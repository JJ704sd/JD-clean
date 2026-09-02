from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.feishu_online_resume_publisher import OnlineFeishuImporter, PublisherConfig, run_cycle


class OnlineResumePublisherTests(unittest.TestCase):
    def _config(self, root: Path, *, dry_run: bool) -> PublisherConfig:
        return PublisherConfig(
            source_directory=root / "downloads",
            output_directory=root / "outputs",
            state_path=root / "state.json",
            report_path=root / "report.json",
            history_path=root / "history.jsonl",
            index_path=root / "resume-index.md",
            dry_run=dry_run,
        )

    @staticmethod
    def _fake_prepare(path: Path, *, candidate_id: str, candidate_name: str, output_directory: Path):
        output = output_directory / candidate_id
        output.mkdir(parents=True, exist_ok=True)
        markdown = output / "resume.feishu.md"
        cleaned = output / "resume.cleaned.md"
        markdown.write_text("# 简历\n\n## 基本信息\n\n## 个人简介\n\n## 教育经历\n\n## 工作经历\n\n## 项目经历\n\n## 技能\n\n## 证书与语言能力\n\n## 其他信息\n", encoding="utf-8")
        cleaned.write_text("cleaned", encoding="utf-8")
        return {
            "markdown_path": str(markdown),
            "cleaned_markdown_path": str(cleaned),
            "markdown_chars": 100,
            "page_count": 1,
            "used_ocr": False,
        }

    def _source(self, root: Path) -> None:
        source = root / "downloads"
        source.mkdir()
        (source / "【全栈工程师_深圳 15-25K】张三 5年.pdf").write_bytes(b"pdf")

    def test_dry_run_only_prepares_local_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source(root)
            config = self._config(root, dry_run=True)
            with patch(
                "scripts.feishu_online_resume_publisher.prepare_markdown",
                self._fake_prepare,
            ):
                report = run_cycle(config)

            self.assertEqual(report["summary"]["prepared"], 1)
            self.assertEqual(report["feishu_imports"], 0)
            self.assertFalse(report["external_writes"])
            self.assertEqual(report["base_writebacks"], 0)
            self.assertEqual(report["model_calls"], 0)
            self.assertNotIn("https://", (root / "resume-index.md").read_text(encoding="utf-8"))

    def test_apply_imports_once_and_writes_online_link_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source(root)
            dry_config = self._config(root, dry_run=True)
            with patch(
                "scripts.feishu_online_resume_publisher.prepare_markdown",
                self._fake_prepare,
            ):
                run_cycle(dry_config)

            importer = Mock()
            importer.import_and_readback.return_value = {
                "status": "success",
                "doc_url": "https://example.feishu.cn/docx/abc",
                "readback_nonempty": True,
                "readback_chars": 500,
            }
            apply_config = replace(dry_config, dry_run=False)
            with patch(
                "scripts.feishu_online_resume_publisher.prepare_markdown",
                self._fake_prepare,
            ):
                report = run_cycle(apply_config, importer=importer)

            self.assertEqual(report["summary"]["success"], 1)
            self.assertEqual(report["feishu_imports"], 1)
            importer.import_and_readback.assert_called_once()
            self.assertIn("https://example.feishu.cn/docx/abc", (root / "resume-index.md").read_text(encoding="utf-8"))

            second_importer = Mock()
            second = run_cycle(apply_config, importer=second_importer)
            self.assertEqual(second["summary"]["already_published"], 1)
            second_importer.import_and_readback.assert_not_called()

    def test_import_pending_is_retained_without_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source(root)
            dry_config = self._config(root, dry_run=True)
            with patch(
                "scripts.feishu_online_resume_publisher.prepare_markdown",
                self._fake_prepare,
            ):
                run_cycle(dry_config)

            first_importer = Mock()
            first_importer.import_and_readback.return_value = {
                "status": "import_pending",
                "import_outcome_uncertain": True,
                "error": "confirm target folder manually",
            }
            apply_config = replace(dry_config, dry_run=False)
            with patch(
                "scripts.feishu_online_resume_publisher.prepare_markdown",
                self._fake_prepare,
            ):
                first = run_cycle(apply_config, importer=first_importer)

            self.assertEqual(first["summary"]["import_pending"], 1)
            self.assertTrue(first["manual_confirmation_required"])

            second_importer = Mock()
            second_importer.import_and_readback.return_value = {
                "status": "success",
                "doc_url": "https://example.feishu.cn/docx/should-not-be-called",
                "readback_nonempty": True,
            }
            second = run_cycle(apply_config, importer=second_importer)
            self.assertEqual(second["summary"]["import_pending"], 1)
            second_importer.import_and_readback.assert_not_called()

    def test_cli_import_uses_relative_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root, dry_run=False)
            markdown = root / "outputs" / "resume.feishu.md"
            markdown.parent.mkdir(parents=True)
            markdown.write_text("# resume", encoding="utf-8")
            content = "\n".join(
                [
                    "## 基本信息",
                    "## 个人简介",
                    "## 教育经历",
                    "## 工作经历",
                    "## 项目经历",
                    "## 技能",
                    "## 证书与语言能力",
                    "## 其他信息",
                ]
            )
            cli = Mock()
            cli.run.side_effect = [
                SimpleNamespace(
                    ok=True,
                    payload={"ok": True, "url": "https://example.feishu.cn/docx/abc"},
                    diagnostic="",
                ),
                SimpleNamespace(
                    ok=True,
                    payload={"ok": True, "data": {"document": {"content": content}}},
                    diagnostic="",
                ),
            ]
            importer = OnlineFeishuImporter(config, cli=cli)
            with patch(
                "scripts.feishu_online_resume_publisher.relative_to_root",
                return_value="output/resume.feishu.md",
            ) as relative:
                result = importer.import_and_readback(markdown, "张三-简历")

            self.assertEqual(result["status"], "success")
            relative.assert_called_once_with(markdown)
            import_args = cli.run.call_args_list[0].args[0]
            self.assertEqual(import_args[3], "output/resume.feishu.md")


if __name__ == "__main__":
    unittest.main()

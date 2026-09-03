from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.feishu_online_resume_publisher import (
    OnlineFeishuImporter,
    PublisherConfig,
    _config_from_args,
    _parser,
    run_cycle,
)
from resume_screening.queue import TaskStore


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
            self.assertEqual(
                (root / "resume-index.md").read_text(encoding="utf-8"),
                "候选人文档 · 在线简历\n\n• 张三  简历：https://example.feishu.cn/docx/abc\n",
            )

            second_importer = Mock()
            second = run_cycle(apply_config, importer=second_importer)
            self.assertEqual(second["summary"]["already_published"], 1)
            second_importer.import_and_readback.assert_not_called()

    def test_apply_handoffs_readback_to_screening_queue_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source(root)
            screening_database = root / "screening.sqlite3"
            screening_output = root / "screening-output"
            dry_config = replace(
                self._config(root, dry_run=True),
                screening_enabled=True,
                screening_database=screening_database,
                screening_output_directory=screening_output,
            )

            with patch(
                "scripts.feishu_online_resume_publisher.prepare_markdown",
                self._fake_prepare,
            ):
                dry_report = run_cycle(dry_config)

            self.assertEqual(dry_report["screening_queue_handoffs"], 0)
            self.assertEqual(dry_report["model_calls"], 0)
            self.assertEqual(dry_report["items"][0]["screening"]["status"], "blocked")
            self.assertFalse(screening_database.exists())

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

            item = report["items"][0]
            self.assertEqual(report["summary"]["success"], 1)
            self.assertEqual(report["screening_queue_handoffs"], 1)
            self.assertEqual(report["screening_queue_failures"], 0)
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(item["screening"]["status"], "queued")
            self.assertEqual(item["screening"]["model"], "MiniMax-M3")
            self.assertEqual(item["screening"]["role"], "senior-fullstack-engineer")
            self.assertEqual(TaskStore(screening_database).status_counts(), {"queued": 1})
            self.assertEqual(
                (root / "resume-index.md").read_text(encoding="utf-8"),
                "候选人文档 · 在线简历\n",
            )

            second_importer = Mock()
            second = run_cycle(apply_config, importer=second_importer)
            self.assertEqual(second["summary"]["already_published"], 1)
            self.assertEqual(second["items"][0]["screening"]["status"], "queued")
            second_importer.import_and_readback.assert_not_called()
            self.assertEqual(TaskStore(screening_database).status_counts(), {"queued": 1})

    def test_screening_index_requires_score_threshold_and_positive_recommendation(
        self,
    ) -> None:
        cases = (
            (69, "advance_pending_human", "shortlist", False),
            (70, "advance_pending_human", "shortlist", True),
            (95, "do_not_advance_pending_human", "shortlist", False),
            (59, "do_not_advance_pending_human", "all-scored", True),
        )
        for score, review_status, index_mode, should_show in cases:
            with (
                self.subTest(score=score, review_status=review_status),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                self._source(root)
                screening_database = root / "screening.sqlite3"
                dry_config = replace(
                    self._config(root, dry_run=True),
                    screening_enabled=True,
                    screening_database=screening_database,
                    screening_output_directory=root / "screening-output",
                    screening_index_mode=index_mode,
                )
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
                    run_cycle(apply_config, importer=importer)

                task = TaskStore(screening_database).successful_results()
                self.assertEqual(task, [])
                queued = TaskStore(screening_database).claim_next()
                self.assertIsNotNone(queued)
                assert queued is not None
                store = TaskStore(screening_database)
                store.mark_succeeded(
                    queued.task_id,
                    result={
                        "scorecard": {
                            "score": score,
                            "review_status": review_status,
                        }
                    },
                    api_response_id=None,
                    usage={},
                )

                second = run_cycle(apply_config, importer=Mock())
                index = (root / "resume-index.md").read_text(encoding="utf-8")
                self.assertEqual(
                    "https://example.feishu.cn/docx/abc" in index, should_show
                )
                self.assertEqual(second["screening"]["min_score"], 70)
                self.assertEqual(second["screening"]["index_mode"], index_mode)

    def test_empty_online_resume_index_contains_only_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root, dry_run=True)

            with patch(
                "scripts.feishu_online_resume_publisher._iter_pdf_files",
                return_value=[],
            ):
                run_cycle(config)

            self.assertEqual(
                (root / "resume-index.md").read_text(encoding="utf-8"),
                "候选人文档 · 在线简历\n",
            )

    def test_screening_cli_defaults_to_domestic_minimax_m3_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _parser().parse_args(
                [
                    "--screening",
                    "--source-dir",
                    str(root / "downloads"),
                    "--screening-database",
                    str(root / "screening.sqlite3"),
                    "--screening-output",
                    str(root / "screening-output"),
                ]
            )

            config = _config_from_args(args)

            self.assertTrue(config.screening_enabled)
            self.assertEqual(config.screening_model, "MiniMax-M3")
            self.assertEqual(config.screening_role, "senior-fullstack-engineer")
            self.assertEqual(config.screening_min_score, 70)
            self.assertEqual(config.screening_database, (root / "screening.sqlite3").resolve())
            self.assertEqual(config.screening_output_directory, (root / "screening-output").resolve())

    def test_screening_min_score_is_configurable_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = _parser().parse_args(
                [
                    "--screening",
                    "--screening-min-score",
                    "80",
                    "--screening-index-mode",
                    "all-scored",
                    "--source-dir",
                    str(root / "downloads"),
                ]
            )
            config = _config_from_args(args)
            self.assertEqual(config.screening_min_score, 80)
            self.assertEqual(config.screening_index_mode, "all-scored")

            invalid_args = _parser().parse_args(
                [
                    "--screening",
                    "--screening-min-score",
                    "101",
                    "--source-dir",
                    str(root / "downloads"),
                ]
            )
            with self.assertRaisesRegex(ValueError, "0 to 100"):
                _config_from_args(invalid_args)

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

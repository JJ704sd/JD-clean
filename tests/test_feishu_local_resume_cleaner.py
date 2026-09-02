from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.feishu_local_resume_cleaner import LocalCleanerConfig, run_cycle


class LocalResumeCleanerTests(unittest.TestCase):
    def _config(self, root: Path, *, watch: bool = False) -> LocalCleanerConfig:
        return LocalCleanerConfig(
            source_directory=root / "downloads",
            output_directory=root / "outputs",
            state_path=root / "state.json",
            report_path=root / "report.json",
            history_path=root / "history.jsonl",
            watch=watch,
        )

    def test_only_today_fullstack_pdf_is_processed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "downloads"
            source.mkdir()
            matching = source / "【全栈工程师_深圳 15-25K】张三 5年.pdf"
            matching.write_bytes(b"pdf-a")
            nonmatching = source / "【产品经理_深圳 15-25K】李四 5年.pdf"
            nonmatching.write_bytes(b"pdf-b")
            old = source / "【全栈工程师_深圳 15-25K】旧简历 5年.pdf"
            old.write_bytes(b"pdf-c")
            old_timestamp = matching.stat().st_mtime - 3 * 24 * 60 * 60
            os.utime(old, (old_timestamp, old_timestamp))

            calls: list[Path] = []

            def fake_prepare(path: Path, *, candidate_id: str, candidate_name: str, output_directory: Path):
                calls.append(path)
                output_directory.mkdir(parents=True, exist_ok=True)
                structured = output_directory / candidate_id / "resume.feishu.md"
                structured.parent.mkdir(parents=True, exist_ok=True)
                structured.write_text("# 简历\n", encoding="utf-8")
                return {
                    "output_directory": str(structured.parent),
                    "structured_markdown": str(structured),
                    "cleaned_markdown": "",
                }

            with patch("scripts.feishu_local_resume_cleaner.prepare_markdown", fake_prepare):
                report = run_cycle(self._config(root))

            self.assertEqual(report["pdf_total"], 2)
            self.assertEqual(report["relevant_pdf_total"], 1)
            self.assertEqual(report["summary"], {"success": 1})
            self.assertEqual(calls, [matching])
            self.assertEqual(report["feishu_imports"], 0)
            self.assertEqual(report["base_writebacks"], 0)
            self.assertEqual(report["model_calls"], 0)

    def test_success_is_idempotent_by_pdf_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "downloads"
            source.mkdir()
            matching = source / "【全栈工程师_深圳 15-25K】张三 5年.pdf"
            matching.write_bytes(b"same-pdf")
            calls = 0

            def fake_prepare(path: Path, *, candidate_id: str, candidate_name: str, output_directory: Path):
                nonlocal calls
                calls += 1
                output_directory.mkdir(parents=True, exist_ok=True)
                structured = output_directory / candidate_id / "resume.feishu.md"
                structured.parent.mkdir(parents=True, exist_ok=True)
                structured.write_text("# 简历\n", encoding="utf-8")
                return {
                    "output_directory": str(structured.parent),
                    "structured_markdown": str(structured),
                    "cleaned_markdown": "",
                }

            with patch("scripts.feishu_local_resume_cleaner.prepare_markdown", fake_prepare):
                first = run_cycle(self._config(root))
                second = run_cycle(self._config(root))

            self.assertEqual(first["summary"], {"success": 1})
            self.assertEqual(second["summary"], {"already_processed": 1})
            self.assertEqual(calls, 1)

    def test_watch_waits_for_two_stable_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "downloads"
            source.mkdir()
            matching = source / "【全栈工程师_深圳 15-25K】张三 5年.pdf"
            matching.write_bytes(b"stable-pdf")
            calls = 0

            def fake_prepare(path: Path, *, candidate_id: str, candidate_name: str, output_directory: Path):
                nonlocal calls
                calls += 1
                output_directory.mkdir(parents=True, exist_ok=True)
                structured = output_directory / candidate_id / "resume.feishu.md"
                structured.parent.mkdir(parents=True, exist_ok=True)
                structured.write_text("# 简历\n", encoding="utf-8")
                return {
                    "output_directory": str(structured.parent),
                    "structured_markdown": str(structured),
                    "cleaned_markdown": "",
                }

            with patch("scripts.feishu_local_resume_cleaner.prepare_markdown", fake_prepare):
                first = run_cycle(self._config(root, watch=True))
                second = run_cycle(self._config(root, watch=True))

            self.assertEqual(first["summary"], {"waiting_for_stable_file": 1})
            self.assertEqual(second["summary"], {"success": 1})
            self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()

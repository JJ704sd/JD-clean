"""Public UI workflows on a real Tk interpreter using synthetic local data."""

import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from resume_screening.desktop import DesktopApp, InstanceLock
from resume_screening.desktop_model import AnalysisResult
from resume_screening.desktop_smoke import TEXT


class DesktopUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        try:
            self.window = tk.Tk()
        except tk.TclError:
            self.skipTest("No graphical session; packaged UI smoke remains required")
        self.window.withdraw()
        self.addCleanup(self.window.destroy)
        self.app = DesktopApp(self.window, self.directory)
        self.window.update()

    def test_review_switch_and_save(self):
        source = self.directory / "synthetic.txt"
        source.write_text(TEXT, encoding="utf-8")
        record = self.app.store.prepare(source, "senior-fullstack-engineer")
        self.app.refresh()
        self.app.documents.selection_set(record["id"])
        self.window.update()
        self.assertGreaterEqual(len(self.app.evidence), 9)
        self.app.decision.set("信息不足")
        self.app.note.insert("1.0", "确认任职日期")
        self.app.criteria.selection_set("1")
        self.window.update()
        self.app.decision.set("有证据支持")
        self.app.note.insert("1.0", "原文写明 Python 和 SQL 服务")
        self.app.reviewer.set("合成审阅者")
        self.app.save_review()
        review = self.app.store.latest_review(record["id"])
        self.assertIn("确认任职日期", review["evidence"])
        self.assertIn("Python", review["evidence"])
        self.assertEqual(self.app.store.get(record["id"])["status"], "待人工审阅")
        self.assertFalse(self.app.dirty)
        with (
            patch("tkinter.messagebox.askokcancel", return_value=False),
            patch.object(self.app, "start_job") as job,
        ):
            self.app.test_model()
            job.assert_not_called()

    def test_review_draft_autosaves(self):
        source = self.directory / "synthetic.txt"
        source.write_text(TEXT, encoding="utf-8")
        record = self.app.store.prepare(source, "senior-fullstack-engineer")
        self.app.refresh()
        self.app.documents.selection_set(record["id"])
        self.window.update()
        self.app.reviewer.set("草稿审阅者")
        self.app.note.insert("1.0", "稍后核对任职日期")
        self.app.autosave_draft()
        draft = self.app.store.get_draft(record["id"])
        self.assertIsNotNone(draft)
        self.assertEqual(draft["reviewer"], "草稿审阅者")
        self.assertIn("稍后核对", draft["evidence"])

    def test_analyze_selected_batch_and_reports_each_run(self):
        first = self.directory / "first.txt"
        second = self.directory / "second.txt"
        first.write_text(TEXT, encoding="utf-8")
        second.write_text(TEXT + "\nAdditional project evidence.", encoding="utf-8")
        records = [
            self.app.store.prepare(path, "senior-fullstack-engineer")
            for path in (first, second)
        ]
        self.app.refresh()
        self.app.documents.selection_set(records[0]["id"])
        self.app.documents.selection_add(records[1]["id"])
        self.app.provider.set("openai-compatible")
        self.app.base.set("https://example.invalid/v1")
        self.app.model.set("fixture")
        client = Mock()
        statuses = iter(("manual_review", "succeeded"))
        with (
            patch("tkinter.messagebox.askokcancel", return_value=True),
            patch("resume_screening.desktop.configured_client", return_value=client),
            patch(
                "resume_screening.desktop.run_analysis",
                side_effect=lambda *args, **kwargs: (next(statuses), Path("fixture")),
            ) as run,
            patch.object(self.app, "start_job") as job,
        ):
            self.app.analyze()
            job.assert_called_once()
            result = job.call_args.args[0]()
        self.assertIn("完成 2/2 份", result)
        self.assertEqual(run.call_count, 2)

    def test_analyze_pauses_after_provider_auth_failure(self):
        first = self.directory / "first.txt"
        second = self.directory / "second.txt"
        first.write_text(TEXT, encoding="utf-8")
        second.write_text(TEXT + "\nAdditional project evidence.", encoding="utf-8")
        records = [
            self.app.store.prepare(path, "senior-fullstack-engineer")
            for path in (first, second)
        ]
        self.app.refresh()
        self.app.documents.selection_set(records[0]["id"])
        self.app.documents.selection_add(records[1]["id"])
        self.app.provider.set("openai-compatible")
        self.app.base.set("https://example.invalid/v1")
        self.app.model.set("fixture")
        client = Mock()
        outcomes = iter(
            (
                AnalysisResult(
                    "retryable_failed", Path("first-run"), "PROVIDER_AUTH_FAILED"
                ),
                AnalysisResult("succeeded", Path("second-run"), None),
            )
        )
        with (
            patch("tkinter.messagebox.askokcancel", return_value=True),
            patch("resume_screening.desktop.configured_client", return_value=client),
            patch(
                "resume_screening.desktop.run_analysis",
                side_effect=lambda *args, **kwargs: next(outcomes),
            ) as run,
            patch.object(self.app, "start_job") as job,
        ):
            self.app.analyze()
            result = job.call_args.args[0]()
        self.assertEqual(run.call_count, 1)
        self.assertIn("已暂停", result)
        self.assertIn("PROVIDER_AUTH_FAILED", result)

    def test_analyze_warns_before_repeating_same_model_run(self):
        source = self.directory / "synthetic.txt"
        source.write_text(TEXT, encoding="utf-8")
        record = self.app.store.prepare(source, "senior-fullstack-engineer")
        self.app.refresh()
        self.app.documents.selection_set(record["id"])
        self.app.provider.set("openai-compatible")
        self.app.base.set("https://example.invalid/v1")
        self.app.model.set("fixture")
        ask = Mock(return_value=False)
        with (
            patch("tkinter.messagebox.askokcancel", ask),
            patch.object(self.app.store, "find_ai_runs", return_value=[{"id": "prior"}]),
        ):
            self.app.analyze()
        self.assertIn("再次计费", ask.call_args.args[1])

    def test_duplicate_instance_and_release(self):
        lock = InstanceLock(self.directory)
        try:
            with self.assertRaises(ValueError):
                InstanceLock(self.directory)
        finally:
            lock.close()
        other = InstanceLock(self.directory)
        other.close()

    def test_batch_continues_after_file_read_error(self):
        blocked = self.directory / "无法读取.txt"
        normal = self.directory / "正常简历.txt"
        blocked.write_text(TEXT, encoding="utf-8")
        normal.write_text(TEXT, encoding="utf-8")
        self.app.role.set("资深全栈工程师")
        original_read_bytes = Path.read_bytes
        blocked_resolved = blocked.resolve()

        def read_bytes(path):
            if path == blocked_resolved:
                raise PermissionError("sensitive-provider-token")
            return original_read_bytes(path)

        with (
            patch("tkinter.messagebox.askokcancel", return_value=True),
            patch.object(Path, "read_bytes", read_bytes),
        ):
            self.app.prepare_files([blocked, normal])
            deadline = time.monotonic() + 5
            while self.app.busy and time.monotonic() < deadline:
                self.window.update()
                time.sleep(0.01)

        self.assertFalse(self.app.busy, "导入应正常结束")
        documents = self.app.store.list_documents()
        self.assertEqual([row["name"] for row in documents], [normal.name])
        self.assertEqual(documents[0]["status"], "待人工审阅")
        report = (self.directory / "last-import-report.txt").read_text(encoding="utf-8")
        self.assertIn(blocked.name, report)
        self.assertNotIn("sensitive-provider-token", report)
        self.assertIn("1/2", self.app.status.get())

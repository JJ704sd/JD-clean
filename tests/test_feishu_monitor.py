from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from resume_screening.feishu_monitor import (
    CliResponse,
    DEFAULT_ERROR_COLUMN,
    DEFAULT_JOB_PREFIX,
    DEFAULT_LINK_COLUMN,
    DEFAULT_PROCESSED_AT_COLUMN,
    DEFAULT_SOURCE_HASH_COLUMN,
    DEFAULT_STATUS_COLUMN,
    FeishuResumeMonitor,
    MonitorConfig,
    PreflightContext,
    base_row_duplicate,
    build_writeback_updates,
    compare_writeback,
    markdown_quality_issues,
    pdf_key,
    structured_markdown,
    writeback_diagnostics,
)
from resume_screening.feishu_screening import (
    DEFAULT_SCREENING_ROLE,
    READBACK_REQUIRED_CODE,
    ScreeningHandoffError,
    ScreeningQueueBridge,
)
from resume_screening.queue import TaskStore


class FeishuMonitorTests(unittest.TestCase):
    def _config(self) -> MonitorConfig:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            return MonitorConfig(
                base_token="test-base-token",
                table_id="tbl-test",
                view_id="vew-test",
                pdf_directory=root,
                output_directory=root / "outputs",
                state_path=root / "state.json",
                report_path=root / "report.json",
                history_path=root / "history.ndjson",
                records_path=root / "records.ndjson",
                lock_path=root / "lock",
            )

    def test_pdf_key_requires_explicit_filename_rule(self):
        self.assertEqual(pdf_key(Path("【全栈工程师_深圳 15-25K】张三 4年.pdf")), "张三")
        self.assertEqual(pdf_key(Path("【全栈工程师】张三 10年以上.pdf")), "")
        self.assertEqual(
            pdf_key(Path("【目标岗位】张三 10年以上.pdf"), "目标岗位"),
            "张三",
        )
        self.assertEqual(pdf_key(Path("张三.pdf")), "")
        self.assertEqual(pdf_key(Path("【全栈工程师】张三 4年.docx")), "")
        self.assertEqual(DEFAULT_JOB_PREFIX, "全栈工程师_深圳 15-25K")

    def test_enumeration_ignores_non_target_job_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "【全栈工程师_深圳 15-25K】张三 4年.pdf"
            other_job = root / "【后端工程师】李四 4年.pdf"
            target.touch()
            other_job.touch()
            config = MonitorConfig(
                base_token="test-base-token",
                table_id="tbl-test",
                view_id="vew-test",
                pdf_directory=root,
                output_directory=root / "outputs",
                state_path=root / "state.json",
                report_path=root / "report.json",
                history_path=root / "history.ndjson",
                records_path=root / "records.ndjson",
                lock_path=root / "lock",
            )
            self.assertEqual(FeishuResumeMonitor(config).enumerate_pdfs(), [target])

    def test_screening_handoff_requires_readback_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.pdf"
            source.write_bytes(b"pdf source")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            bridge = ScreeningQueueBridge(
                database=root / "screening.sqlite3",
                output_directory=root / "screening-output",
                role=DEFAULT_SCREENING_ROLE,
            )
            blocked = bridge.enqueue_after_readback(
                source_path=source,
                source_sha256=source_hash,
                candidate_id="feishu-test",
                candidate_name="张三",
                document_url=None,
                readback_nonempty=None,
                readback_chars=None,
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["error_code"], READBACK_REQUIRED_CODE)

            first = bridge.enqueue_after_readback(
                source_path=source,
                source_sha256=source_hash,
                candidate_id="feishu-test",
                candidate_name="张三",
                document_url="https://example.feishu.cn/docx/test",
                readback_nonempty=True,
                readback_chars=240,
            )
            second = bridge.enqueue_after_readback(
                source_path=source,
                source_sha256=source_hash,
                candidate_id="feishu-test",
                candidate_name="张三",
                document_url="https://example.feishu.cn/docx/test",
                readback_nonempty=True,
                readback_chars=240,
            )
            self.assertEqual(first["status"], "queued")
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertEqual(bridge.store.status_counts(), {"queued": 1})
            self.assertEqual(bridge.task_status(first["task_id"])["status"], "queued")

            with patch.object(
                bridge.store,
                "enqueue",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                with self.assertRaises(ScreeningHandoffError):
                    bridge.enqueue_after_readback(
                        source_path=source,
                        source_sha256=source_hash,
                        candidate_id="feishu-test",
                        candidate_name="张三",
                        document_url="https://example.feishu.cn/docx/test",
                        readback_nonempty=True,
                        readback_chars=240,
                    )

    def test_monitor_handoffs_only_after_successful_document_readback(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            pdf_directory = root / "pdfs"
            pdf_directory.mkdir()
            source = pdf_directory / "【全栈工程师_深圳 15-25K】张三 4年.pdf"
            source.write_bytes(b"pdf source")

            class FakeBaseCLI:
                def run(self, args, *, timeout=90):
                    command = args[1]
                    if command == "+record-list":
                        output = Path.cwd() / args[args.index("--output") + 1]
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(
                            json.dumps({"record_id": "rec-1", "姓名": "张三"}, ensure_ascii=False)
                            + "\n",
                            encoding="utf-8",
                        )
                        return CliResponse(0, {}, stdout="")
                    if command == "+field-list":
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"fields": [{"name": "姓名", "type": "text"}]}},
                        )
                    if command in {"+table-get", "+view-get"}:
                        return CliResponse(0, {"ok": True, "data": {}})
                    raise AssertionError(f"unexpected CLI call: {command}")

            def config(dry_run):
                return MonitorConfig(
                    base_token="test-base-token",
                    table_id="tbl-test",
                    view_id="vew-test",
                    pdf_directory=pdf_directory,
                    output_directory=root / "monitor-output",
                    state_path=root / "monitor-state.json",
                    report_path=root / "monitor-report.json",
                    history_path=root / "monitor-history.ndjson",
                    records_path=root / "records.ndjson",
                    lock_path=root / "import.lock",
                    dry_run=dry_run,
                    screening_enabled=True,
                    screening_database=root / "screening.sqlite3",
                    screening_output_directory=root / "screening-output",
                )

            def fake_prepare(path, candidate_id, candidate_name, output_directory):
                destination = output_directory / candidate_id
                destination.mkdir(parents=True, exist_ok=True)
                markdown = destination / "resume.feishu.md"
                cleaned = destination / "resume.cleaned.md"
                markdown.write_text("# 简历\n\n## 基本信息\n\n测试\n", encoding="utf-8")
                cleaned.write_text("测试\n", encoding="utf-8")
                return {
                    "candidate_id": candidate_id,
                    "markdown_path": str(markdown),
                    "cleaned_markdown_path": str(cleaned),
                    "markdown_chars": 240,
                    "page_count": 1,
                    "used_ocr": False,
                    "parser_version": "test",
                }

            cli = FakeBaseCLI()
            with patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare):
                dry_report = FeishuResumeMonitor(config(True), cli).run_cycle()
            self.assertEqual(dry_report["execution"]["screening_handoffs"], 0)
            self.assertEqual(TaskStore(config(True).screening_database).status_counts(), {})

            import_calls = []

            def fake_import(self, markdown_path, display_name):
                import_calls.append((markdown_path, display_name))
                return {
                    "status": "success",
                    "attempts": 1,
                    "url": "https://example.feishu.cn/docx/test",
                    "readback_nonempty": True,
                    "readback_chars": 240,
                }

            with (
                patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare),
                patch.object(FeishuResumeMonitor, "import_document", fake_import),
            ):
                apply_report = FeishuResumeMonitor(config(False), cli).run_cycle()

            item = apply_report["items"][0]
            self.assertEqual(item["status"], "success")
            self.assertEqual(item["screening"]["status"], "queued")
            self.assertEqual(apply_report["execution"]["screening_handoffs"], 1)
            self.assertEqual(apply_report["execution"]["remote_writes"], 0)
            self.assertEqual(TaskStore(config(False).screening_database).status_counts(), {"queued": 1})

            with (
                patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare),
                patch.object(FeishuResumeMonitor, "import_document", fake_import),
            ):
                second_report = FeishuResumeMonitor(config(False), cli).run_cycle()

            self.assertEqual(len(import_calls), 1)
            self.assertEqual(second_report["items"][0]["status"], "already_processed")
            self.assertEqual(
                second_report["items"][0]["imported_doc_url"],
                "https://example.feishu.cn/docx/test",
            )

    def test_two_monitor_instances_recheck_after_same_folder_import_lock(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            pdf_directory = root / "pdfs"
            pdf_directory.mkdir()
            source = pdf_directory / "【全栈工程师_深圳 15-25K】张三 4年.pdf"
            source.write_bytes(b"pdf source")
            content = "\n\n".join(
                [
                    "# 简历",
                    "## 基本信息\n未提及",
                    "## 个人简介\n未提及",
                    "## 教育经历\n未提及",
                    "## 工作经历\n未提及",
                    "## 项目经历\n未提及",
                    "## 技能\n未提及",
                    "## 证书与语言能力\n未提及",
                    "## 其他信息\n未提及",
                ]
            )

            class CoordinatedCLI:
                def __init__(self):
                    self.import_entered = threading.Event()
                    self.release_import = threading.Event()
                    self.second_preflight_started = threading.Event()
                    self._table_calls = 0
                    self._lock = threading.Lock()
                    self.import_calls = 0

                def run(self, args, *, timeout=90):
                    command = args[1]
                    if command == "+table-get":
                        with self._lock:
                            self._table_calls += 1
                            if self._table_calls >= 3:
                                self.second_preflight_started.set()
                        return CliResponse(0, {"ok": True, "data": {}})
                    if command == "+view-get":
                        return CliResponse(0, {"ok": True, "data": {}})
                    if command == "+field-list":
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"fields": [{"name": "姓名", "type": "text"}]}},
                        )
                    if command == "+record-list":
                        output = Path.cwd() / args[args.index("--output") + 1]
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(
                            json.dumps({"record_id": "rec-1", "姓名": "张三"}, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                        return CliResponse(0, {}, stdout="")
                    if command == "+import":
                        with self._lock:
                            self.import_calls += 1
                            import_number = self.import_calls
                        if import_number == 1:
                            self.import_entered.set()
                            if not self.release_import.wait(5):
                                raise AssertionError("test import release timed out")
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"url": "https://example.feishu.cn/docx/once"}},
                        )
                    if command == "+fetch":
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"document": {"content": content}}},
                        )
                    raise AssertionError(f"unexpected CLI call: {command}")

            def config(*, dry_run, retry_failed=False):
                return MonitorConfig(
                    base_token="test-base-token",
                    table_id="tbl-test",
                    view_id="vew-test",
                    pdf_directory=pdf_directory,
                    output_directory=root / "monitor-output",
                    state_path=root / "monitor-state.json",
                    report_path=root / "monitor-report.json",
                    history_path=root / "monitor-history.ndjson",
                    records_path=root / "records.ndjson",
                    lock_path=root / "import.lock",
                    dry_run=dry_run,
                    retry_failed=retry_failed,
                )

            def fake_prepare(path, candidate_id, candidate_name, output_directory):
                destination = output_directory / candidate_id
                destination.mkdir(parents=True, exist_ok=True)
                markdown = destination / "resume.feishu.md"
                cleaned = destination / "resume.cleaned.md"
                markdown.write_text("# 简历\n", encoding="utf-8")
                cleaned.write_text("测试\n", encoding="utf-8")
                return {
                    "candidate_id": candidate_id,
                    "markdown_path": str(markdown),
                    "cleaned_markdown_path": str(cleaned),
                    "markdown_chars": 240,
                    "page_count": 1,
                    "used_ocr": False,
                    "parser_version": "test",
                }

            cli = CoordinatedCLI()
            with patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare):
                FeishuResumeMonitor(config(dry_run=True), cli).run_cycle()

            first_reports = []
            second_reports = []
            errors = []

            def run(monitor, destination):
                try:
                    destination.append(monitor.run_cycle())
                except BaseException as exc:
                    errors.append(exc)

            with patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare):
                first_thread = threading.Thread(target=run, args=(FeishuResumeMonitor(config(dry_run=False), cli), first_reports))
                first_thread.start()
                self.assertTrue(cli.import_entered.wait(2))

                second_thread = threading.Thread(
                    target=run,
                    args=(FeishuResumeMonitor(config(dry_run=False, retry_failed=True), cli), second_reports),
                )
                second_thread.start()
                cli.second_preflight_started.wait(1)
                time.sleep(0.05)
                cli.release_import.set()
                first_thread.join(5)
                second_thread.join(5)

            self.assertFalse(errors)
            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(cli.import_calls, 1)
            self.assertEqual(first_reports[0]["items"][0]["status"], "success")
            self.assertEqual(second_reports[0]["items"][0]["status"], "already_processed")
            self.assertEqual(
                second_reports[0]["items"][0]["imported_doc_url"],
                "https://example.feishu.cn/docx/once",
            )

    def test_import_retries_transient_document_readback_without_reimporting(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            markdown = root / "resume.feishu.md"
            markdown.write_text("# 简历\n", encoding="utf-8")
            content = "\n\n".join(
                [
                    "# 简历",
                    "## 基本信息\n未提及",
                    "## 个人简介\n后端与前端开发",
                    "## 教育经历\n未提及",
                    "## 工作经历\n未提及",
                    "## 项目经历\n未提及",
                    "## 技能\n未提及",
                    "## 证书与语言能力\n未提及",
                    "## 其他信息\n未提及",
                ]
            )

            class RetryReadbackCLI:
                def __init__(self):
                    self.import_calls = 0
                    self.fetch_calls = 0

                def run(self, args, *, timeout=90):
                    command = args[1]
                    if command == "+import":
                        self.import_calls += 1
                        return CliResponse(
                            0,
                            {
                                "ok": True,
                                "data": {"url": "https://example.feishu.cn/docx/retry"},
                            },
                        )
                    if command == "+fetch":
                        self.fetch_calls += 1
                        if self.fetch_calls < 3:
                            return CliResponse(
                                1,
                                {"ok": False, "error": {"message": "temporary timeout"}},
                            )
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"document": {"content": content}}},
                        )
                    raise AssertionError(f"unexpected CLI call: {command}")

            config = MonitorConfig(
                base_token="test-base-token",
                table_id="tbl-test",
                view_id="vew-test",
                pdf_directory=root,
                output_directory=root / "outputs",
                state_path=root / "state.json",
                report_path=root / "report.json",
                history_path=root / "history.ndjson",
                records_path=root / "records.ndjson",
                lock_path=root / "import.lock",
                dry_run=False,
            )
            cli = RetryReadbackCLI()
            with patch("resume_screening.feishu_monitor.time.sleep"):
                result = FeishuResumeMonitor(config, cli).import_document(markdown, "张三-简历")

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["url"], "https://example.feishu.cn/docx/retry")
            self.assertEqual(result["transient_attempts"], 2)
            self.assertEqual(cli.import_calls, 1)
            self.assertEqual(cli.fetch_calls, 3)

    def test_uncertain_import_without_result_is_pending_without_replay(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            markdown = root / "resume.feishu.md"
            markdown.write_text("# 简历\n", encoding="utf-8")

            class UncertainImportCLI:
                def __init__(self):
                    self.import_calls = 0

                def run(self, args, *, timeout=90):
                    if args[1] != "+import":
                        raise AssertionError(f"unexpected CLI call: {args[1]}")
                    self.import_calls += 1
                    return CliResponse(
                        1,
                        {"ok": False, "error": {"message": "temporary timeout"}},
                    )

            config = MonitorConfig(
                base_token="test-base-token",
                table_id="tbl-test",
                view_id="vew-test",
                pdf_directory=root,
                output_directory=root / "outputs",
                state_path=root / "state.json",
                report_path=root / "report.json",
                history_path=root / "history.ndjson",
                records_path=root / "records.ndjson",
                lock_path=root / "import.lock",
                dry_run=False,
            )
            cli = UncertainImportCLI()
            with patch("resume_screening.feishu_monitor.time.sleep"):
                result = FeishuResumeMonitor(config, cli).import_document(markdown, "张三-简历")

            self.assertEqual(result["status"], "import_pending")
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(cli.import_calls, 1)
            self.assertTrue(result["import_outcome_uncertain"])
            self.assertIn("temporary timeout", result["error"])

    def test_uncertain_import_is_persisted_and_retry_failed_does_not_reimport(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            pdf_directory = root / "pdfs"
            pdf_directory.mkdir()
            source = pdf_directory / "【全栈工程师_深圳 15-25K】张三 4年.pdf"
            source.write_bytes(b"pdf source")

            class UncertainMonitorCLI:
                def __init__(self):
                    self.import_calls = 0

                def run(self, args, *, timeout=90):
                    command = args[1]
                    if command in {"+table-get", "+view-get"}:
                        return CliResponse(0, {"ok": True, "data": {}})
                    if command == "+field-list":
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"fields": [{"name": "姓名", "type": "text"}]}},
                        )
                    if command == "+record-list":
                        output = Path.cwd() / args[args.index("--output") + 1]
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(
                            json.dumps({"record_id": "rec-1", "姓名": "张三"}, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                        return CliResponse(0, {}, stdout="")
                    if command == "+import":
                        self.import_calls += 1
                        return CliResponse(
                            1,
                            {"ok": False, "error": {"message": "temporary timeout"}},
                        )
                    raise AssertionError(f"unexpected CLI call: {command}")

            def config(*, dry_run, retry_failed=False):
                return MonitorConfig(
                    base_token="test-base-token",
                    table_id="tbl-test",
                    view_id="vew-test",
                    pdf_directory=pdf_directory,
                    output_directory=root / "monitor-output",
                    state_path=root / "monitor-state.json",
                    report_path=root / "monitor-report.json",
                    history_path=root / "monitor-history.ndjson",
                    records_path=root / "records.ndjson",
                    lock_path=root / "import.lock",
                    dry_run=dry_run,
                    retry_failed=retry_failed,
                )

            def fake_prepare(path, candidate_id, candidate_name, output_directory):
                destination = output_directory / candidate_id
                destination.mkdir(parents=True, exist_ok=True)
                markdown = destination / "resume.feishu.md"
                cleaned = destination / "resume.cleaned.md"
                markdown.write_text("# 简历\n", encoding="utf-8")
                cleaned.write_text("测试\n", encoding="utf-8")
                return {
                    "candidate_id": candidate_id,
                    "markdown_path": str(markdown),
                    "cleaned_markdown_path": str(cleaned),
                    "markdown_chars": 240,
                    "page_count": 1,
                    "used_ocr": False,
                    "parser_version": "test",
                }

            cli = UncertainMonitorCLI()
            with patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare):
                FeishuResumeMonitor(config(dry_run=True), cli).run_cycle()
                first = FeishuResumeMonitor(config(dry_run=False), cli).run_cycle()

            self.assertEqual(first["items"][0]["status"], "import_pending")
            self.assertIn("temporary timeout", first["items"][0]["error"])
            self.assertEqual(first["summary"]["import_pending"], 1)
            self.assertEqual(cli.import_calls, 1)
            state = json.loads((root / "monitor-state.json").read_text(encoding="utf-8"))
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(state["entries"][source_hash]["status"], "import_pending")
            self.assertTrue(state["entries"][source_hash]["import_outcome_uncertain"])
            self.assertIn("temporary timeout", state["entries"][source_hash]["error"])

            def unexpected_prepare(*args, **kwargs):
                raise AssertionError("unresolved import must not regenerate Markdown")

            with patch("resume_screening.feishu_monitor.prepare_markdown", unexpected_prepare):
                next_cycle = FeishuResumeMonitor(config(dry_run=False), cli).run_cycle()
                retry_cycle = FeishuResumeMonitor(
                    config(dry_run=False, retry_failed=True), cli
                ).run_cycle()

            self.assertEqual(next_cycle["items"][0]["status"], "import_pending")
            self.assertEqual(retry_cycle["items"][0]["status"], "import_pending")
            self.assertIn("manually", retry_cycle["items"][0]["error"])
            self.assertEqual(cli.import_calls, 1)

    def test_retry_failed_readback_reuses_existing_document(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            pdf_directory = root / "pdfs"
            pdf_directory.mkdir()
            source = pdf_directory / "【全栈工程师_深圳 15-25K】张三 4年.pdf"
            source.write_bytes(b"pdf source")
            content = "\n\n".join(
                [
                    "# 简历",
                    "## 基本信息\n未提及",
                    "## 个人简介\n未提及",
                    "## 教育经历\n未提及",
                    "## 工作经历\n未提及",
                    "## 项目经历\n未提及",
                    "## 技能\n未提及",
                    "## 证书与语言能力\n未提及",
                    "## 其他信息\n未提及",
                ]
            )

            class RetryReadbackMonitorCLI:
                def __init__(self):
                    self.fetch_calls = 0

                def run(self, args, *, timeout=90):
                    command = args[1]
                    if command in {"+table-get", "+view-get"}:
                        return CliResponse(0, {"ok": True, "data": {}})
                    if command == "+field-list":
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"fields": [{"name": "姓名", "type": "text"}]}},
                        )
                    if command == "+record-list":
                        output = Path.cwd() / args[args.index("--output") + 1]
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(
                            json.dumps({"record_id": "rec-1", "姓名": "张三"}, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                        return CliResponse(0, {}, stdout="")
                    if command == "+fetch":
                        self.fetch_calls += 1
                        return CliResponse(
                            0,
                            {"ok": True, "data": {"document": {"content": content}}},
                        )
                    raise AssertionError(f"unexpected CLI call: {command}")

            def config(*, dry_run, retry_failed=False):
                return MonitorConfig(
                    base_token="test-base-token",
                    table_id="tbl-test",
                    view_id="vew-test",
                    pdf_directory=pdf_directory,
                    output_directory=root / "monitor-output",
                    state_path=root / "monitor-state.json",
                    report_path=root / "monitor-report.json",
                    history_path=root / "monitor-history.ndjson",
                    records_path=root / "records.ndjson",
                    lock_path=root / "import.lock",
                    dry_run=dry_run,
                    retry_failed=retry_failed,
                )

            def fake_prepare(path, candidate_id, candidate_name, output_directory):
                destination = output_directory / candidate_id
                destination.mkdir(parents=True, exist_ok=True)
                markdown = destination / "resume.feishu.md"
                cleaned = destination / "resume.cleaned.md"
                markdown.write_text("# 简历\n", encoding="utf-8")
                cleaned.write_text("测试\n", encoding="utf-8")
                return {
                    "candidate_id": candidate_id,
                    "markdown_path": str(markdown),
                    "cleaned_markdown_path": str(cleaned),
                    "markdown_chars": 240,
                    "page_count": 1,
                    "used_ocr": False,
                    "parser_version": "test",
                }

            def failed_import(self, markdown_path, display_name):
                return {
                    "status": "import_failed",
                    "attempts": 1,
                    "url": "https://example.feishu.cn/docx/existing",
                    "error": "document_readback_failed: temporary timeout",
                }

            cli = RetryReadbackMonitorCLI()
            with patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare):
                FeishuResumeMonitor(config(dry_run=True), cli).run_cycle()
            with (
                patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare),
                patch.object(FeishuResumeMonitor, "import_document", failed_import),
            ):
                first_apply = FeishuResumeMonitor(config(dry_run=False), cli).run_cycle()

            self.assertEqual(first_apply["items"][0]["status"], "import_failed")
            self.assertEqual(first_apply["summary"]["import_failed"], 1)
            self.assertIn("document_readback_failed", first_apply["items"][0]["error"])

            def unexpected_prepare(*args, **kwargs):
                raise AssertionError("readback retry must not regenerate Markdown")

            def unexpected_import(self, markdown_path, display_name):
                raise AssertionError("readback retry must not import a second document")

            with (
                patch("resume_screening.feishu_monitor.prepare_markdown", unexpected_prepare),
                patch.object(FeishuResumeMonitor, "import_document", unexpected_import),
            ):
                recovered = FeishuResumeMonitor(
                    config(dry_run=False, retry_failed=True), cli
                ).run_cycle()

            self.assertEqual(recovered["items"][0]["status"], "success")
            self.assertEqual(recovered["items"][0]["imported_doc_url"], "https://example.feishu.cn/docx/existing")
            self.assertEqual(cli.fetch_calls, 1)

    def test_writeback_retries_transient_update_and_verification(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            config = MonitorConfig(
                base_token="test-base-token",
                table_id="tbl-test",
                view_id="vew-test",
                pdf_directory=root,
                output_directory=root / "outputs",
                state_path=root / "state.json",
                report_path=root / "report.json",
                history_path=root / "history.ndjson",
                records_path=root / "records.ndjson",
                lock_path=root / "import.lock",
                dry_run=False,
            )
            fields = {
                DEFAULT_LINK_COLUMN: {"id": "link", "name": DEFAULT_LINK_COLUMN, "type": "text"},
                DEFAULT_STATUS_COLUMN: {"id": "status", "name": DEFAULT_STATUS_COLUMN, "type": "select", "options": [{"name": "success"}]},
                DEFAULT_ERROR_COLUMN: {"id": "error", "name": DEFAULT_ERROR_COLUMN, "type": "text"},
                DEFAULT_PROCESSED_AT_COLUMN: {"id": "processed", "name": DEFAULT_PROCESSED_AT_COLUMN, "type": "datetime"},
                DEFAULT_SOURCE_HASH_COLUMN: {"id": "hash", "name": DEFAULT_SOURCE_HASH_COLUMN, "type": "text"},
            }
            preflight = PreflightContext(
                True,
                {"writeback_allowed": True},
                [],
                {},
                fields,
                "fingerprint",
            )
            url = "https://example.feishu.cn/docx/writeback"
            source_hash = "a" * 64

            class RetryWritebackCLI:
                def __init__(self):
                    self.update_calls = 0
                    self.get_calls = 0

                def run(self, args, *, timeout=90):
                    command = args[1]
                    if command == "+record-batch-update":
                        self.update_calls += 1
                        if self.update_calls < 3:
                            return CliResponse(
                                1,
                                {"ok": False, "error": {"message": "429 rate limit"}},
                            )
                        return CliResponse(0, {"ok": True, "data": {}})
                    if command == "+record-get":
                        self.get_calls += 1
                        if self.get_calls < 3:
                            return CliResponse(
                                1,
                                {"ok": False, "error": {"message": "temporary timeout"}},
                            )
                        return CliResponse(
                            0,
                            {
                                "ok": True,
                                "data": {
                                    "data": [
                                        [url, ["success"], "", "2026-09-02 12:00", source_hash]
                                    ]
                                },
                            },
                        )
                    raise AssertionError(f"unexpected CLI call: {command}")

            item = {"record_id": "rec-1", "source_sha256": source_hash}
            cli = RetryWritebackCLI()
            with (
                patch("resume_screening.feishu_monitor.time.sleep"),
                patch("resume_screening.feishu_monitor.now_local", return_value="2026-09-02 12:00"),
            ):
                result = FeishuResumeMonitor(config, cli).writeback(preflight, item, url)

            self.assertEqual(result["status"], "verified")
            self.assertTrue(result["written"])
            self.assertEqual(cli.update_calls, 3)
            self.assertEqual(cli.get_calls, 3)

    def test_retry_failed_writeback_reuses_existing_document(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            pdf_directory = root / "pdfs"
            pdf_directory.mkdir()
            source = pdf_directory / "【全栈工程师_深圳 15-25K】张三 4年.pdf"
            source.write_bytes(b"pdf source")

            class WritebackRecoveryCLI:
                def __init__(self):
                    self.update_calls = 0
                    self.get_calls = 0

                def run(self, args, *, timeout=90):
                    command = args[1]
                    if command == "+table-get" or command == "+view-get":
                        return CliResponse(0, {"ok": True, "data": {}})
                    if command == "+field-list":
                        return CliResponse(
                            0,
                            {
                                "ok": True,
                                "data": {
                                    "fields": [
                                        {"name": "姓名", "type": "text"},
                                        {"name": DEFAULT_LINK_COLUMN, "type": "text"},
                                        {"name": DEFAULT_STATUS_COLUMN, "type": "select", "options": [{"name": "success"}]},
                                        {"name": DEFAULT_ERROR_COLUMN, "type": "text"},
                                        {"name": DEFAULT_PROCESSED_AT_COLUMN, "type": "datetime"},
                                        {"name": DEFAULT_SOURCE_HASH_COLUMN, "type": "text"},
                                    ]
                                },
                            },
                        )
                    if command == "+record-list":
                        output = Path.cwd() / args[args.index("--output") + 1]
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(
                            json.dumps({"record_id": "rec-1", "姓名": "张三"}, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                        return CliResponse(0, {}, stdout="")
                    if command == "+record-batch-update":
                        self.update_calls += 1
                        if self.update_calls == 1:
                            return CliResponse(
                                1,
                                {"ok": False, "error": {"message": "permission denied"}},
                            )
                        return CliResponse(0, {"ok": True, "data": {}})
                    if command == "+record-get":
                        self.get_calls += 1
                        return CliResponse(
                            0,
                            {
                                "ok": True,
                                "data": {
                                    "data": [
                                        [
                                            "https://example.feishu.cn/docx/recovered",
                                            ["success"],
                                            "",
                                            "2026-09-02 12:00",
                                            hashlib.sha256(source.read_bytes()).hexdigest(),
                                        ]
                                    ]
                                },
                            },
                        )
                    raise AssertionError(f"unexpected CLI call: {command}")

            def config(*, dry_run, retry_failed=False):
                return MonitorConfig(
                    base_token="test-base-token",
                    table_id="tbl-test",
                    view_id="vew-test",
                    pdf_directory=pdf_directory,
                    output_directory=root / "monitor-output",
                    state_path=root / "monitor-state.json",
                    report_path=root / "monitor-report.json",
                    history_path=root / "monitor-history.ndjson",
                    records_path=root / "records.ndjson",
                    lock_path=root / "import.lock",
                    dry_run=dry_run,
                    retry_failed=retry_failed,
                )

            def fake_prepare(path, candidate_id, candidate_name, output_directory):
                destination = output_directory / candidate_id
                destination.mkdir(parents=True, exist_ok=True)
                markdown = destination / "resume.feishu.md"
                cleaned = destination / "resume.cleaned.md"
                markdown.write_text("# 简历\n", encoding="utf-8")
                cleaned.write_text("测试\n", encoding="utf-8")
                return {
                    "candidate_id": candidate_id,
                    "markdown_path": str(markdown),
                    "cleaned_markdown_path": str(cleaned),
                    "markdown_chars": 240,
                    "page_count": 1,
                    "used_ocr": False,
                    "parser_version": "test",
                }

            import_calls = 0

            def fake_import(self, markdown_path, display_name):
                nonlocal import_calls
                import_calls += 1
                return {
                    "status": "success",
                    "attempts": 1,
                    "url": "https://example.feishu.cn/docx/recovered",
                    "readback_nonempty": True,
                    "readback_chars": 240,
                }

            cli = WritebackRecoveryCLI()
            with (
                patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare),
                patch("resume_screening.feishu_monitor.now_local", return_value="2026-09-02 12:00"),
            ):
                FeishuResumeMonitor(config(dry_run=True), cli).run_cycle()
            with (
                patch("resume_screening.feishu_monitor.prepare_markdown", fake_prepare),
                patch.object(FeishuResumeMonitor, "import_document", fake_import),
                patch("resume_screening.feishu_monitor.now_local", return_value="2026-09-02 12:00"),
            ):
                first_apply = FeishuResumeMonitor(config(dry_run=False), cli).run_cycle()

            self.assertEqual(first_apply["items"][0]["status"], "sheet_write_failed")
            self.assertEqual(import_calls, 1)

            def unexpected_import(self, markdown_path, display_name):
                raise AssertionError("writeback retry must not import a second document")

            with (
                patch.object(FeishuResumeMonitor, "import_document", unexpected_import),
                patch("resume_screening.feishu_monitor.now_local", return_value="2026-09-02 12:00"),
            ):
                recovered = FeishuResumeMonitor(
                    config(dry_run=False, retry_failed=True), cli
                ).run_cycle()

            self.assertEqual(recovered["items"][0]["status"], "already_processed")
            self.assertEqual(recovered["items"][0]["writeback"]["status"], "verified")
            self.assertEqual(import_calls, 1)
            self.assertEqual(cli.update_calls, 2)
            self.assertEqual(cli.get_calls, 1)

    def test_writeback_schema_is_strict_and_updates_only_configured_columns(self):
        config = self._config()
        fields = {
            DEFAULT_LINK_COLUMN: {"id": "link", "name": DEFAULT_LINK_COLUMN, "type": "text"},
            DEFAULT_STATUS_COLUMN: {"id": "status", "name": DEFAULT_STATUS_COLUMN, "type": "select", "options": [{"name": "success"}]},
            DEFAULT_ERROR_COLUMN: {"id": "error", "name": DEFAULT_ERROR_COLUMN, "type": "text"},
            DEFAULT_PROCESSED_AT_COLUMN: {"id": "processed", "name": DEFAULT_PROCESSED_AT_COLUMN, "type": "datetime"},
            DEFAULT_SOURCE_HASH_COLUMN: {"id": "hash", "name": DEFAULT_SOURCE_HASH_COLUMN, "type": "text"},
        }
        self.assertEqual(writeback_diagnostics(config, fields), [])
        updates = build_writeback_updates(
            config,
            fields,
            document_url="https://example.feishu.cn/docx/doc-token",
            source_hash="a" * 64,
            processed_at="2026-09-02 12:00",
        )
        self.assertEqual(set(updates), set(config.required_columns))
        self.assertEqual(updates[DEFAULT_STATUS_COLUMN], ["success"])
        self.assertTrue(compare_writeback(config, list(updates.values()), updates))

    def test_writeback_is_blocked_when_success_option_is_not_available(self):
        config = self._config()
        fields = {
            name: {"name": name, "type": "text"}
            for name in config.required_columns
        }
        fields[DEFAULT_STATUS_COLUMN] = {
            "name": DEFAULT_STATUS_COLUMN,
            "type": "select",
            "options": [{"name": "已处理"}],
        }
        diagnostics = writeback_diagnostics(config, fields)
        self.assertIn("status select does not contain success", diagnostics)

    def test_existing_link_or_same_hash_is_not_overwritten(self):
        config = self._config()
        self.assertEqual(
            base_row_duplicate(config, {DEFAULT_LINK_COLUMN: "https://example/doc"}, "a" * 64),
            (True, "existing_link", "https://example/doc"),
        )
        self.assertEqual(
            base_row_duplicate(config, {DEFAULT_SOURCE_HASH_COLUMN: "A" * 64}, "a" * 64),
            (True, "existing_source_hash", None),
        )
        self.assertEqual(
            base_row_duplicate(config, {DEFAULT_SOURCE_HASH_COLUMN: "b" * 64}, "a" * 64),
            (False, "", None),
        )

    def test_structured_markdown_has_required_sections_and_privacy_markers(self):
        cleaned = SimpleNamespace(
            candidate_id="feishu-test",
            source_sha256="a" * 64,
            parser_version="test",
            used_ocr=False,
            page_count=1,
            markdown="---\n\n张三\n电话：13812345678\n个人优势\n负责 Go 订单服务。\n教育经历\n本科。\n",
        )
        document = structured_markdown(cleaned, "张三")
        self.assertTrue(all(heading in document for heading in (
            "## 基本信息",
            "## 个人简介",
            "## 教育经历",
            "## 工作经历",
            "## 项目经历",
            "## 技能",
            "## 证书与语言能力",
            "## 其他信息",
        )))
        self.assertNotIn("13812345678", document)
        self.assertNotIn("张三", document)
        self.assertEqual(markdown_quality_issues(document), [])


if __name__ == "__main__":
    unittest.main()

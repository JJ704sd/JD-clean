from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from resume_screening.cli import main
from resume_screening.queue import TaskStore


class CliTests(unittest.TestCase):
    def test_export_review_queue_includes_task_id_for_calibration_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "【ai产品经理】候选人.md"
            source.write_text("AI 产品需求、评测、上线和复盘 " * 12, encoding="utf-8")
            database = root / "state.sqlite3"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "--database",
                        str(database),
                        "enqueue",
                        str(source),
                        "--auto-route",
                    ]
                )
            store = TaskStore(database)
            task = store.claim_next()
            store.mark_succeeded(
                task.task_id,
                result={
                    "screening_record": {
                        "role": "ai-product-manager",
                        "model_recommendation": "advance",
                    },
                    "scorecard": {"score": 80, "grade": "B"},
                },
                api_response_id=None,
                usage={},
            )

            with redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "--database",
                        str(database),
                        "export",
                        "--directory",
                        str(root / "exports"),
                    ]
                )

            with (root / "exports" / "review_queue.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(code, 0)
            self.assertEqual(row["task_id"], str(task.task_id))

    def test_enqueue_today_filters_older_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            incoming.mkdir()
            current = incoming / "【ai产品经理】今日候选人 4年.md"
            older = incoming / "【ai产品经理】历史候选人 4年.md"
            for path in (current, older):
                path.write_text(
                    "负责 AI 产品需求、评测、上线和复盘。" * 12, encoding="utf-8"
                )
            os.utime(current, (datetime(2026, 9, 1, 12).timestamp(),) * 2)
            os.utime(older, (datetime(2026, 8, 31, 12).timestamp(),) * 2)
            database = root / "state.sqlite3"

            with (
                patch(
                    "resume_screening.cli._local_today", return_value=date(2026, 9, 1)
                ),
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "--database",
                        str(database),
                        "enqueue",
                        str(incoming),
                        "--auto-route",
                        "--today",
                    ]
                )

            store = TaskStore(database)
            task = store.claim_next()
            self.assertEqual(code, 0)
            self.assertIsNotNone(task)
            self.assertEqual(Path(task.source_path).name, current.name)
            self.assertIsNone(store.claim_next())

    def test_auto_route_distribution_counts_unique_tasks_after_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            incoming.mkdir()
            content = "负责 AI 产品需求、评测、上线和复盘。" * 12
            (incoming / "【ai产品经理】候选人甲 4年.md").write_text(
                content, encoding="utf-8"
            )
            (incoming / "【ai产品经理】候选人甲副本 4年.md").write_text(
                content, encoding="utf-8"
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = main(
                    [
                        "--database",
                        str(root / "state.sqlite3"),
                        "enqueue",
                        str(incoming),
                        "--auto-route",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("登记 1 份简历", stdout.getvalue())
            self.assertIn("ai-product-manager=1", stdout.getvalue())

    def test_auto_route_enqueues_known_roles_with_names_and_skips_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            incoming.mkdir()
            for name in (
                "【全栈工程师_深圳 15-25K】唐先生 4年.md",
                "【ai产品经理_深圳 15-25K】陈熙纯 4年.md",
                "电子发票.md",
            ):
                (incoming / name).write_text(
                    "项目经历和工作内容。" * 12, encoding="utf-8"
                )
            database = root / "state.sqlite3"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = main(
                    [
                        "--database",
                        str(database),
                        "enqueue",
                        str(incoming),
                        "--auto-route",
                    ]
                )

            store = TaskStore(database)
            first = store.claim_next()
            second = store.claim_next()
            self.assertEqual(code, 0)
            self.assertEqual(
                {first.role, second.role},
                {
                    "senior-fullstack-engineer",
                    "ai-product-manager",
                },
            )
            self.assertEqual(
                {first.candidate_name, second.candidate_name},
                {
                    "唐先生",
                    "陈熙纯",
                },
            )
            self.assertIsNone(store.claim_next())
            self.assertIn("跳过 1 个无法识别岗位的文件", stdout.getvalue())

    def test_enqueue_directory_and_show_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            incoming.mkdir()
            (incoming / "candidate.md").write_text(
                "负责 Go 物流项目开发与上线。" * 12, encoding="utf-8"
            )
            database = root / "state.sqlite3"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "--database",
                        str(database),
                        "enqueue",
                        str(incoming),
                        "--role",
                        "senior-fullstack-engineer",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("登记 1 份", stdout.getvalue())
            self.assertEqual(TaskStore(database).status_counts(), {"queued": 1})

    def test_fixed_role_rejects_files_with_a_conflicting_explicit_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "【ai产品经理_深圳】候选人 5年.md"
            incoming.write_text(
                "负责 AI 产品需求、评测、上线和复盘。" * 12, encoding="utf-8"
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "--database",
                        str(root / "state.sqlite3"),
                        "enqueue",
                        str(incoming),
                        "--role",
                        "senior-fullstack-engineer",
                    ]
                )

            self.assertEqual(code, 2)
            self.assertRegex(stderr.getvalue(), "文件名岗位.*指定岗位冲突")
            self.assertEqual(TaskStore(root / "state.sqlite3").status_counts(), {})

    def test_worker_without_key_does_not_claim_or_count_an_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "candidate.md"
            source.write_text("负责 Go 物流项目开发与上线。" * 12, encoding="utf-8")
            database = root / "state.sqlite3"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "--database",
                        str(database),
                        "enqueue",
                        str(source),
                        "--role",
                        "senior-fullstack-engineer",
                    ]
                )
            stderr = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
                code = main(["--database", str(database), "worker", "--once"])

            self.assertEqual(code, 2)
            store = TaskStore(database)
            task = store.claim_next()
            self.assertIsNotNone(task)
            self.assertEqual(task.attempt_count, 0)
            self.assertIn("MINIMAX_API_KEY", stderr.getvalue())

    def test_worker_stops_batch_after_provider_level_rejection(self):
        first = SimpleNamespace(
            task_id=1,
            candidate_id="candidate-one",
            status="manual_review",
            error_code="MODEL_CALL_REJECTED",
        )
        second = SimpleNamespace(
            task_id=2,
            candidate_id="candidate-two",
            status="succeeded",
            error_code=None,
        )

        class FakePipeline:
            def __init__(self, **_: object):
                self.tasks = iter((first, second, None))
                self.calls = 0

            def process_next(self):
                self.calls += 1
                return next(self.tasks)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=True),
            patch("resume_screening.cli.ScreeningPipeline", FakePipeline),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(
                ["--database", str(Path(tmp) / "state.sqlite3"), "worker", "--once"]
            )

        self.assertEqual(code, 2)
        self.assertIn("MODEL_CALL_REJECTED", stderr.getvalue())
        self.assertNotIn("candidate-two", stdout.getvalue())

    def test_worker_max_tasks_bounds_a_smoke_batch(self):
        first = SimpleNamespace(
            task_id=1,
            candidate_id="candidate-one",
            status="succeeded",
            error_code=None,
        )
        second = SimpleNamespace(
            task_id=2,
            candidate_id="candidate-two",
            status="succeeded",
            error_code=None,
        )

        class FakePipeline:
            def __init__(self, **_: object):
                self.tasks = iter((first, second, None))

            def process_next(self):
                return next(self.tasks)

        stdout = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=True),
            patch("resume_screening.cli.ScreeningPipeline", FakePipeline),
            redirect_stdout(stdout),
        ):
            code = main(
                [
                    "--database",
                    str(Path(tmp) / "state.sqlite3"),
                    "worker",
                    "--once",
                    "--max-tasks",
                    "1",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn("candidate-one", stdout.getvalue())
        self.assertNotIn("candidate-two", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

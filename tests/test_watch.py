from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from resume_screening.cli import main
from resume_screening.minimax import ModelResponse
from resume_screening.queue import (
    ACTIVE_CONTRACTS,
    TaskSpec,
    TaskStore,
    WorkerAlreadyRunningError,
    WorkerLeaseLostError,
)
from resume_screening.watch import WatchScanner

ROOT = Path(__file__).resolve().parents[1]


def _v8_payload() -> dict:
    criteria = (
        "SEN-EXP-01",
        "SEN-BE-01",
        "SEN-ARCH-01",
        "SEN-FE-01",
        "SEN-DATA-01",
        "SEN-AI-01",
        "SEN-DOMAIN-01",
        "SEN-LEVEL-01",
        "SEN-ADM-01",
    )
    evidence = []
    for criterion in criteria:
        is_admin = criterion == "SEN-ADM-01"
        excerpt = (
            "计算机相关本科，行政信息可核对"
            if is_admin
            else "在生产 Go 物流项目中负责个人交付，比较方案并完成重构上线"
        )
        evidence.append(
            {
                "criterion_id": criterion,
                "state": "supported",
                "excerpt": excerpt,
                "location": "项目经历",
                "rationale": "有可定位的项目背景、个人动作与结果",
                "confidence": "high",
                "evidence_factors": {
                    "project_context": "生产项目",
                    "personal_action": "候选人负责实现",
                    "method_or_tradeoff": "比较方案后落地",
                    "result_scope": "上线后按周期统计",
                    "verifiable_impact": "监控结果可核验",
                },
            }
        )
    return {"evidence": evidence, "uncertainties": [], "interview_probes": []}


class WatchCliTests(unittest.TestCase):
    def test_auto_route_watch_accepts_mixed_downloads_and_keeps_unknown_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "Downloads"
            incoming.mkdir()
            for filename in (
                "【ai产品经理】候选人甲.md",
                "【全栈开发实习生】候选人乙.md",
                "【资深全栈】候选人丙.md",
                "电子发票.md",
            ):
                (incoming / filename).write_text("有效简历内容 " * 30, encoding="utf-8")

            class FakePipeline:
                def __init__(self, **_: object):
                    self.tasks = iter(
                        SimpleNamespace(
                            task_id=index,
                            candidate_id=f"candidate-{index}",
                            status="succeeded",
                            error_code=None,
                        )
                        for index in range(1, 4)
                    )

                def process_next(self):
                    return next(self.tasks)

            with (
                patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=True),
                patch("resume_screening.cli.ScreeningPipeline", FakePipeline),
                patch("resume_screening.cli.time.sleep"),
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "--database",
                        str(root / "state.sqlite3"),
                        "worker",
                        "--watch",
                        "--input",
                        str(incoming),
                        "--auto-route",
                        "--max-tasks",
                        "3",
                    ]
                )

            self.assertEqual(code, 0)
            store = TaskStore(root / "state.sqlite3")
            tasks = []
            while (task := store.claim_next()) is not None:
                tasks.append(task)
            self.assertEqual(
                {task.role for task in tasks},
                {
                    "ai-product-manager",
                    "fullstack-development-intern",
                    "senior-fullstack-engineer",
                },
            )
            self.assertEqual(len(tasks), 3)

    def test_fixed_role_watch_skips_other_role_and_keeps_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "Downloads"
            incoming.mkdir()
            (incoming / "【资深全栈】候选人甲.md").write_text(
                "有效简历内容 " * 30, encoding="utf-8"
            )
            (incoming / "【ai产品经理】候选人乙.md").write_text(
                "有效简历内容 " * 30, encoding="utf-8"
            )

            class FakePipeline:
                def __init__(self, **_: object):
                    self.calls = 0

                def process_next(self):
                    self.calls += 1
                    if self.calls == 1:
                        return None
                    if self.calls > 2:
                        return None
                    return SimpleNamespace(
                        task_id=1,
                        candidate_id="candidate-one",
                        status="succeeded",
                        error_code=None,
                    )

            with (
                patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=True),
                patch("resume_screening.cli.ScreeningPipeline", FakePipeline),
                patch("resume_screening.cli.time.sleep"),
            ):
                code = main(
                    [
                        "--database",
                        str(root / "state.sqlite3"),
                        "worker",
                        "--watch",
                        "--input",
                        str(incoming),
                        "--role",
                        "senior-fullstack-engineer",
                        "--max-tasks",
                        "1",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                TaskStore(root / "state.sqlite3").watch_event_counts().get(
                    "ROLE_MISMATCH_SKIPPED"
                ),
                1,
            )

    def test_fixed_role_watch_requires_explicit_opt_in_for_unlabeled_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            incoming = Path(tmp) / "Downloads"
            incoming.mkdir()
            source = incoming / "candidate-without-role.md"
            source.write_text("有效简历内容 " * 30, encoding="utf-8")

            scanner = WatchScanner(
                incoming, role="senior-fullstack-engineer", auto_route=False
            )
            self.assertEqual(scanner.scan(), [])
            self.assertEqual(scanner.scan(), [])
            self.assertEqual(scanner.event_counts["UNLABELED_FILE_SKIPPED"], 1)

            accepting_scanner = WatchScanner(
                incoming,
                role="senior-fullstack-engineer",
                auto_route=False,
                accept_unlabeled=True,
            )
            self.assertEqual(accepting_scanner.scan(), [])
            accepted = accepting_scanner.scan()
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0].role, "senior-fullstack-engineer")

    def test_stability_requires_two_unchanged_polls_and_hashes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "【资深全栈】候选人.md"
            source.write_text("有效简历内容 " * 30, encoding="utf-8")
            scanner = WatchScanner(root, auto_route=True)
            with patch("resume_screening.watch._sha256", wraps=None) as digest:
                digest.side_effect = lambda path: __import__(
                    "hashlib"
                ).sha256(path.read_bytes()).hexdigest()
                self.assertEqual(scanner.scan(), [])
                candidates = scanner.scan()
                self.assertEqual(len(candidates), 1)
                self.assertEqual(scanner.scan(), [])
                self.assertEqual(digest.call_count, 1)

            source.write_text("文件仍在写入，内容发生变化。" * 30, encoding="utf-8")
            self.assertEqual(scanner.scan(), [])
            self.assertEqual(len(scanner.scan()), 1)

    def test_old_contract_is_marked_stale_without_blocking_v8(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text("有效简历内容 " * 30, encoding="utf-8")
            store = TaskStore(root / "state.sqlite3")
            old = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="old",
                    role="senior-fullstack-engineer",
                    jd_version="senior-fullstack-2026-08-14-v1",
                    rubric_version="senior-fullstack-2026-09-01-v6",
                    parser_version="resume-cleaner-2026-09-01-v1",
                    scoring_version="evidence-score-2026-09-01-v1",
                    prompt_version="resume-screening-prompt-2026-09-01-v1",
                )
            )
            current = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="current",
                    role="senior-fullstack-engineer",
                    jd_version="senior-fullstack-2026-08-14-v1",
                    rubric_version="senior-fullstack-2026-09-01-v8",
                )
            )

            claimed = store.claim_next()

            self.assertEqual(claimed.task_id, current.task_id)
            self.assertEqual(store.get(old.task_id).status, "queued")
            store.mark_stale_contracts(ACTIVE_CONTRACTS)
            self.assertEqual(store.get(old.task_id).error_code, "STALE_CONTRACT_VERSION")

    def test_health_reports_stale_heartbeat_and_single_worker_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.sqlite3")
            worker_id = store.acquire_worker(
                active_contracts=ACTIVE_CONTRACTS,
                model_configured=True,
                lease_seconds=5,
            )
            with self.assertRaises(WorkerAlreadyRunningError):
                store.acquire_worker(active_contracts=ACTIVE_CONTRACTS, model_configured=True)

            with patch.dict(os.environ, {}, clear=True):
                active_health = store.health_snapshot(active_contracts=ACTIVE_CONTRACTS)
            self.assertTrue(active_health["model_configured"])

            future = datetime.now(UTC) + timedelta(seconds=6)
            health = store.health_snapshot(
                active_contracts=ACTIVE_CONTRACTS,
                model_configured=True,
                now=future,
            )
            self.assertFalse(health["worker_holds_heartbeat"])
            self.assertEqual(health["worker_status"], "stale")
            store.release_worker(worker_id)

    def test_worker_lease_fences_old_owner_from_writing_after_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "【资深全栈】candidate.md"
            source.write_text("Go 物流项目交付证据 " * 20, encoding="utf-8")
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="candidate-fenced",
                    role="senior-fullstack-engineer",
                    jd_version="senior-fullstack-2026-08-14-v1",
                    rubric_version="senior-fullstack-2026-09-01-v8",
                )
            )
            first = store.acquire_worker(
                active_contracts=ACTIVE_CONTRACTS,
                model_configured=True,
                worker_id="first-worker",
            )
            claimed = store.claim_next(lease_id=first)
            self.assertEqual(claimed.task_id, task.task_id)
            store.release_worker(first)
            second = store.acquire_worker(
                active_contracts=ACTIVE_CONTRACTS,
                model_configured=True,
                worker_id="second-worker",
            )

            with self.assertRaises(WorkerLeaseLostError):
                store.mark_manual_review(
                    task.task_id,
                    code="AFTER_LOSS",
                    message="old owner must not write",
                    lease_id=first,
                )
            self.assertEqual(store.get(task.task_id).status, "processing")
            store.mark_manual_review(
                task.task_id,
                code="CURRENT_OWNER",
                message="current owner may write",
                lease_id=second,
            )
            store.release_worker(second)

    def test_v8_watch_runs_clean_assemble_score_validate_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "Downloads"
            incoming.mkdir()
            (incoming / "【资深全栈】匿名候选人.md").write_text(
                "Go 生产物流订单项目，负责后端、架构、数据、前端和重构上线。" * 8,
                encoding="utf-8",
            )
            payload = json.dumps(_v8_payload(), ensure_ascii=False)

            class FakeModel:
                calls = 0

                def __init__(self, **_: object):
                    pass

                def analyze(self, **_: object) -> ModelResponse:
                    FakeModel.calls += 1
                    return ModelResponse(
                        content=payload, response_id="anonymous-response", usage={}
                    )

            with (
                patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=True),
                patch("resume_screening.cli.MiniMaxClient", FakeModel),
                patch("resume_screening.cli.time.sleep"),
            ):
                code = main(
                    [
                        "--database",
                        str(root / "state.sqlite3"),
                        "--output",
                        str(root / "outputs"),
                        "worker",
                        "--watch",
                        "--input",
                        str(incoming),
                        "--auto-route",
                        "--max-tasks",
                        "1",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(FakeModel.calls, 1)
            store = TaskStore(root / "state.sqlite3")
            self.assertEqual(store.status_counts(), {"succeeded": 1})
            result = json.loads(
                next((root / "outputs").glob("*/screening.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                result["screening_record"]["rubric_version"],
                "senior-fullstack-2026-09-01-v8",
            )
            self.assertIn("scorecard", result)

    def test_worker_and_health_output_do_not_echo_resume_pii_or_secrets(self):
        phone = "13812345678"
        email = "candidate@example.com"
        secret = "sk-live-very-secret-token"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "candidate.md"
            source.write_text(f"电话 {phone} 邮箱 {email} 密钥 {secret} " * 10, encoding="utf-8")
            database = root / "state.sqlite3"
            output = io.StringIO()
            with redirect_stdout(output):
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
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                code = main(["--database", str(database), "worker", "--once"])
            with redirect_stdout(output):
                main(["--database", str(database), "health"])

            emitted = output.getvalue() + stderr.getvalue()
            self.assertEqual(code, 2)
            self.assertNotIn(phone, emitted)
            self.assertNotIn(email, emitted)
            self.assertNotIn(secret, emitted)
            self.assertIn("NOT_CONFIGURED", emitted)

    def test_one_bad_resume_is_manual_review_and_does_not_stop_the_watch_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "Downloads"
            incoming.mkdir()
            (incoming / "【资深全栈】a-broken.md").write_text("��", encoding="utf-8")
            (incoming / "【资深全栈】b-good.md").write_text(
                "Go 生产物流项目负责后端交付、架构、数据和重构上线。" * 10,
                encoding="utf-8",
            )

            class FakeModel:
                calls = 0

                def analyze(self, **_: object) -> ModelResponse:
                    FakeModel.calls += 1
                    return ModelResponse(
                        content=json.dumps(_v8_payload(), ensure_ascii=False),
                        response_id="good-response",
                        usage={},
                    )

            with (
                patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=True),
                patch("resume_screening.cli.MiniMaxClient", FakeModel),
                patch("resume_screening.cli.time.sleep"),
            ):
                code = main(
                    [
                        "--database",
                        str(root / "state.sqlite3"),
                        "--output",
                        str(root / "outputs"),
                        "worker",
                        "--watch",
                        "--input",
                        str(incoming),
                        "--auto-route",
                        "--max-tasks",
                        "2",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(FakeModel.calls, 1)
            self.assertEqual(
                TaskStore(root / "state.sqlite3").status_counts(),
                {"manual_review": 1, "succeeded": 1},
            )


if __name__ == "__main__":
    unittest.main()

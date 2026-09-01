from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from resume_screening.minimax import ModelCallError, ModelResponse, RetryableModelError
from resume_screening.pipeline import ScreeningPipeline
from resume_screening.queue import TaskSpec, TaskStore

ROOT = Path(__file__).resolve().parents[1]


def current_senior_record() -> dict:
    contract = (
        ROOT
        / "skills"
        / "screen-senior-fullstack-resumes"
        / "references"
        / "output-contract.md"
    )
    match = re.search(
        r"```json\s*(.*?)\s*```", contract.read_text(encoding="utf-8"), re.DOTALL
    )
    assert match
    record = json.loads(match.group(1))
    record["rubric_version"] = "senior-fullstack-2026-09-01-v8"
    record["priority_profile"]["target_stack"] = "go_present"
    for item in record["evidence"]:
        if item.get("state") == "supported":
            item["evidence_factors"] = {
                "project_context": "生产项目",
                "personal_action": "候选人负责实现",
                "method_or_tradeoff": "比较方案后落地",
                "result_scope": "上线后按周期统计",
                "verifiable_impact": "监控结果可核验",
            }
        else:
            item["evidence_factors"] = {
                "project_context": None,
                "personal_action": None,
                "method_or_tradeoff": None,
                "result_scope": None,
                "verifiable_impact": None,
            }
    return record


def documented_record(skill_dir: str) -> dict:
    if skill_dir == "screen-ai-product-manager-resumes":
        return json.loads(
            (
                ROOT / "skills" / skill_dir / "references" / "example-record.json"
            ).read_text(encoding="utf-8")
        )
    contract = ROOT / "skills" / skill_dir / "references" / "output-contract.md"
    match = re.search(
        r"```json\s*(.*?)\s*```", contract.read_text(encoding="utf-8"), re.DOTALL
    )
    assert match
    return json.loads(match.group(1))


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def analyze(self, **_: object) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content=self.content,
            response_id="response-001",
            usage={"total_tokens": 123},
        )


class SequencedClient(FakeClient):
    def __init__(self, content: str, failures: int):
        super().__init__(content)
        self.failures = failures

    def analyze(self, **_: object) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise RetryableModelError("provider rejected request before generation")
        return ModelResponse(
            content=self.content, response_id="response-final", usage={}
        )


class RejectedClient(FakeClient):
    def analyze(self, **_: object) -> ModelResponse:
        self.calls += 1
        raise ModelCallError("MiniMax API 1004: invalid token")


class ScreeningPipelineTests(unittest.TestCase):
    def _spec(self, source: Path, candidate_id: str = "candidate-001") -> TaskSpec:
        return TaskSpec(
            source_path=source,
            candidate_id=candidate_id,
            role="senior-fullstack-engineer",
            jd_version="senior-fullstack-2026-08-14-v1",
            rubric_version="senior-fullstack-2026-09-01-v8",
        )

    def test_successful_task_calls_model_once_and_writes_structured_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 跨境物流订单服务的开发、测试、上线与重构。" * 12,
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(self._spec(source))
            client = FakeClient(json.dumps(current_senior_record(), ensure_ascii=False))
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            completed = pipeline.process_next()
            nothing_left = pipeline.process_next()

            self.assertEqual(completed.task_id, task.task_id)
            self.assertIsNone(nothing_left)
            self.assertEqual(client.calls, 1)
            self.assertEqual(store.get(task.task_id).status, "succeeded")
            envelope = json.loads(
                (root / "outputs" / "candidate-001" / "screening.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(envelope["scorecard"]["grade"], "A")
            self.assertEqual(envelope["scorecard"]["review_status"], "second_review")
            self.assertTrue(
                (root / "outputs" / "candidate-001" / "resume.cleaned.md").is_file()
            )
            self.assertTrue(
                (root / "outputs" / "candidate-001" / "conclusion.md").is_file()
            )

    def test_senior_evidence_only_payload_is_assembled_by_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 跨境物流订单服务的开发、测试、上线与重构。" * 12,
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            store.enqueue(self._spec(source))
            documented = current_senior_record()
            payload = {
                "evidence": documented["evidence"],
                "uncertainties": documented["uncertainties"] * 2,
                "interview_probes": documented["interview_probes"],
            }
            client = FakeClient(json.dumps(payload, ensure_ascii=False))
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            completed = pipeline.process_next()

            self.assertEqual(completed.status, "succeeded")
            self.assertEqual(client.calls, 1)
            envelope = json.loads(
                (root / "outputs" / "candidate-001" / "screening.json").read_text(
                    encoding="utf-8"
                )
            )
            record = envelope["screening_record"]
            self.assertEqual(record["model_recommendation"], "second_review")
            self.assertEqual(len(record["uncertainties"]), 1)
            self.assertEqual(
                record["human_review"]["level_2_mode"],
                "same_owner_separate_pass",
            )
            self.assertLessEqual(len(record["recommendation_rationale"]), 200)

    def test_parse_failure_never_calls_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "broken.md"
            source.write_text("��", encoding="utf-8")
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(self._spec(source, "candidate-broken"))
            client = FakeClient(json.dumps(current_senior_record(), ensure_ascii=False))
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            pipeline.process_next()

            self.assertEqual(client.calls, 0)
            failed = store.get(task.task_id)
            self.assertEqual(failed.status, "manual_review")
            self.assertEqual(failed.error_code, "U01_PARSE_QUALITY")

    def test_stale_contract_task_requires_versioned_reenqueue_without_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 物流订单系统的接口开发、测试、上线和故障排查。" * 8,
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            spec = self._spec(source)
            store.enqueue(
                TaskSpec(
                    source_path=spec.source_path,
                    candidate_id=spec.candidate_id,
                    role=spec.role,
                    jd_version=spec.jd_version,
                    rubric_version=spec.rubric_version,
                    parser_version="resume-cleaner-2026-09-01-v1",
                    scoring_version="evidence-score-2026-09-01-v1",
                    prompt_version="resume-screening-prompt-2026-09-01-v3",
                )
            )
            client = FakeClient("{}")
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            completed = pipeline.process_next()

            self.assertEqual(completed.status, "manual_review")
            self.assertEqual(completed.error_code, "STALE_CONTRACT_VERSION")
            self.assertEqual(client.calls, 0)

    def test_encrypted_pdf_becomes_manual_review_without_leaving_processing(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "encrypted.pdf"
            document = pymupdf.open()
            document.new_page().insert_text((72, 72), "Confidential resume")
            document.save(
                source,
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner-password",
                user_pw="user-password",
            )
            document.close()
            store = TaskStore(root / "state.sqlite3")
            store.enqueue(self._spec(source, "candidate-encrypted"))
            client = FakeClient(json.dumps(current_senior_record(), ensure_ascii=False))
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            completed = pipeline.process_next()

            self.assertEqual(client.calls, 0)
            self.assertEqual(completed.status, "manual_review")
            self.assertEqual(completed.error_code, "U01_PARSE_QUALITY")

    def test_source_change_after_enqueue_never_analyzes_the_wrong_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 物流订单项目开发、测试与上线。" * 12, encoding="utf-8"
            )
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(self._spec(source))
            source.write_text(
                "文件登记后已被替换为另一份简历内容。" * 12, encoding="utf-8"
            )
            client = FakeClient(json.dumps(current_senior_record(), ensure_ascii=False))
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            pipeline.process_next()

            stopped = store.get(task.task_id)
            self.assertEqual(client.calls, 0)
            self.assertEqual(stopped.status, "manual_review")
            self.assertEqual(stopped.error_code, "SOURCE_CHANGED")

    def test_completed_invalid_model_output_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 跨境物流订单服务的开发、测试、上线与重构。" * 12,
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(self._spec(source))
            client = FakeClient("这不是 JSON")
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            pipeline.process_next()
            self.assertIsNone(pipeline.process_next())

            failed = store.get(task.task_id)
            self.assertEqual(client.calls, 1)
            self.assertEqual(failed.status, "manual_review")
            self.assertTrue(failed.model_completed)
            self.assertEqual(failed.error_code, "INVALID_MODEL_OUTPUT")

    def test_provider_business_rejection_is_diagnostic_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 跨境物流订单服务的开发、测试、上线与重构。" * 12,
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(self._spec(source))
            client = RejectedClient("")
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            pipeline.process_next()
            self.assertIsNone(pipeline.process_next())

            failed = store.get(task.task_id)
            self.assertEqual(client.calls, 1)
            self.assertEqual(failed.status, "manual_review")
            self.assertEqual(failed.error_code, "MODEL_CALL_REJECTED")
            self.assertEqual(failed.error_message, "MiniMax API 1004: invalid token")

    def test_retryable_failures_are_bounded_and_preserve_one_completed_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 跨境物流订单服务的开发、测试、上线与重构。" * 12,
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(self._spec(source))
            client = SequencedClient(
                json.dumps(current_senior_record(), ensure_ascii=False), failures=2
            )
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            pipeline.process_next()
            pipeline.process_next()
            pipeline.process_next()

            completed = store.get(task.task_id)
            self.assertEqual(client.calls, 3)
            self.assertEqual(completed.attempt_count, 3)
            self.assertTrue(completed.model_completed)
            self.assertEqual(completed.status, "succeeded")

    def test_three_uncompleted_provider_failures_stop_for_manual_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text(
                "负责 Go 跨境物流订单服务的开发、测试、上线与重构。" * 12,
                encoding="utf-8",
            )
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(self._spec(source))
            client = SequencedClient(
                json.dumps(current_senior_record(), ensure_ascii=False), failures=99
            )
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )

            for _ in range(4):
                pipeline.process_next()

            stopped = store.get(task.task_id)
            self.assertEqual(client.calls, 3)
            self.assertEqual(stopped.attempt_count, 3)
            self.assertEqual(stopped.status, "manual_review")
            self.assertFalse(stopped.model_completed)

    def test_pipeline_accepts_each_role_owned_output_contract(self):
        cases = (
            (
                "ai-product-manager",
                "ai-pm-2026-08-v2",
                "ai-pm-rubric-2026-08-18-v3",
                "screen-ai-product-manager-resumes",
            ),
            (
                "fullstack-development-intern",
                "fullstack-intern-2026-08-14-v1",
                "fullstack-intern-2026-08-24-v4",
                "screen-fullstack-intern-resumes",
            ),
        )
        for role, jd_version, rubric_version, skill_dir in cases:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "resume.md"
                source.write_text(
                    "负责产品或工程项目的需求、实现、测试、上线与复盘。" * 12,
                    encoding="utf-8",
                )
                store = TaskStore(root / "state.sqlite3")
                task = store.enqueue(
                    TaskSpec(
                        source_path=source,
                        candidate_id="candidate-role-test",
                        role=role,
                        jd_version=jd_version,
                        rubric_version=rubric_version,
                    )
                )
                client = FakeClient(
                    json.dumps(documented_record(skill_dir), ensure_ascii=False)
                )
                pipeline = ScreeningPipeline(
                    store=store,
                    client=client,
                    output_root=root / "outputs",
                    project_root=ROOT,
                )

                pipeline.process_next()

                self.assertEqual(store.get(task.task_id).status, "succeeded")
                self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()

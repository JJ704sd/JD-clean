from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from resume_screening.business_operations_assembly import assemble_business_operations_record
from resume_screening.metadata import infer_role
from resume_screening.minimax import ModelResponse
from resume_screening.pipeline import ScreeningPipeline
from resume_screening.prompts import build_system_prompt
from resume_screening.queue import TaskSpec, TaskStore
from resume_screening.scoring import score_record
from resume_screening.versions import contract_for_role

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "skills" / "screen-business-system-operations-resumes" / "references" / "example-record.json"


def payload() -> dict:
    record = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for item in record["evidence"]:
        if item["state"] == "supported":
            item["evidence_factors"] = {
                "project_context": "企业业务系统支持项目",
                "personal_action": "候选人本人排查并跟进",
                "method_or_tradeoff": "按影响范围选择处理路径",
                "result_scope": "用户确认后关闭",
                "verifiable_impact": "工单记录可核验",
            }
        else:
            item["evidence_factors"] = {
                "project_context": None,
                "personal_action": None,
                "method_or_tradeoff": None,
                "result_scope": None,
                "verifiable_impact": None,
            }
    return {
        key: record[key]
        for key in ("evidence", "uncertainties", "interview_probes")
    }


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def analyze(self, **_: object) -> ModelResponse:
        self.calls += 1
        return ModelResponse(content=self.content, response_id="bso-test", usage={})


class BusinessSystemOperationsIntegrationTests(unittest.TestCase):
    def test_record_and_score_use_role_owned_contract(self):
        jd_version, rubric_version, _, scoring_version, prompt_version = contract_for_role(
            "business-system-operations-engineer"
        )
        record = assemble_business_operations_record(
            payload(),
            screening_record_id="sr-bso-test",
            candidate_id="candidate-bso-test",
            candidate_name="张三",
            jd_version=jd_version,
            rubric_version=rubric_version,
        )
        from resume_screening.contracts import validate_record

        self.assertEqual(validate_record(ROOT, record["role"], record), [])
        self.assertEqual(len(record["evidence"]), 16)
        self.assertEqual(score_record(record).scoring_version, scoring_version)
        self.assertIn("BSO-TROUBLE-01", build_system_prompt(
            ROOT,
            role=record["role"],
            candidate_id=record["candidate_id"],
            jd_version=jd_version,
            rubric_version=rubric_version,
            prompt_version=prompt_version,
        ))

    def test_pipeline_accepts_business_system_operations_payload(self):
        jd_version, rubric_version, *_ = contract_for_role(
            "business-system-operations-engineer"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text("企业业务系统支持、问题排查、用户培训与工单跟进。" * 10, encoding="utf-8")
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="candidate-bso-test",
                    candidate_name="张三",
                    role="business-system-operations-engineer",
                    jd_version=jd_version,
                    rubric_version=rubric_version,
                )
            )
            client = FakeClient(json.dumps(payload(), ensure_ascii=False))
            pipeline = ScreeningPipeline(
                store=store,
                client=client,
                output_root=root / "outputs",
                project_root=ROOT,
            )
            pipeline.process_next()
            self.assertEqual(store.get(task.task_id).status, "succeeded")
            output = json.loads(
                (root / "outputs" / "candidate-bso-test" / "screening.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                output["screening_record"]["role"],
                "business-system-operations-engineer",
            )
            self.assertEqual(len(output["screening_record"]["evidence"]), 16)

    def test_filename_routing_uses_explicit_business_system_marker(self):
        self.assertEqual(
            infer_role(Path("业务系统运维工程师_候选人.pdf")),
            "business-system-operations-engineer",
        )


if __name__ == "__main__":
    unittest.main()

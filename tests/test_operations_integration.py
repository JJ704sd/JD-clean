import json
import tempfile
import unittest
from pathlib import Path

from resume_screening.contracts import validate_record
from resume_screening.minimax import ModelResponse
from resume_screening.operations_assembly import assemble_operations_record
from resume_screening.pipeline import ScreeningPipeline
from resume_screening.queue import TaskSpec, TaskStore
from resume_screening.scoring import score_record
from resume_screening.versions import contract_for_role


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "skills" / "screen-operations-devops-resumes" / "references" / "example-record-v6.json"


def example_payload() -> dict:
    source = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return {key: source[key] for key in ("evidence", "uncertainties", "interview_probes")}


class OperationsIntegrationTests(unittest.TestCase):
    def test_example_payload_assembles_into_current_role_contract(self):
        jd_version, rubric_version, *_ = contract_for_role("operations-devops-engineer")
        record = assemble_operations_record(
            example_payload(),
            screening_record_id="sr-ops-test",
            candidate_id="candidate-ops-test",
            candidate_name="张三",
            jd_version=jd_version,
            rubric_version=rubric_version,
        )

        self.assertEqual(validate_record(ROOT, record["role"], record), [])
        self.assertEqual(record["schema_version"], "1.4")
        self.assertEqual(len(record["evidence"]), 20)
        self.assertEqual(len(record["interview_probes"]), 4)
        self.assertEqual(score_record(record).scoring_version, "operations-devops-score-2026-09-17-v1")

    def test_pipeline_accepts_operations_task_with_role_specific_contract(self):
        content = json.dumps(example_payload(), ensure_ascii=False)
        contract = contract_for_role("operations-devops-engineer")

        class FakeClient:
            def analyze(self, **_: object) -> ModelResponse:
                return ModelResponse(content=content, response_id="response-ops-test", usage={})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "运维开发工程师-张三.txt"
            source.write_text(
                "张三 本科 运维开发工程师 Linux Docker PostgreSQL Redis Kafka Prometheus RAG AI 项目经历。" * 5,
                encoding="utf-8",
            )
            store = TaskStore(root / "screening.sqlite3")
            task = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="candidate-ops-test",
                    candidate_name="张三",
                    role="operations-devops-engineer",
                    jd_version=contract[0],
                    rubric_version=contract[1],
                )
            )
            self.assertEqual(task.scoring_version, contract[3])
            self.assertEqual(task.prompt_version, contract[4])
            result = ScreeningPipeline(
                store=store,
                client=FakeClient(),
                output_root=root / "output",
                project_root=ROOT,
                result_metadata={"provider": "test", "model": "MiniMax-M3"},
            ).process_next()

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "succeeded")
            output = json.loads(
                (root / "output" / "candidate-ops-test" / "screening.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(output["screening_record"]["role"], "operations-devops-engineer")
            self.assertEqual(output["screening_record"]["schema_version"], "1.4")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from resume_screening.queue import TaskSpec, TaskStore


class TaskQueueTests(unittest.TestCase):
    def test_interrupted_processing_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text("Go 物流项目经历" * 30, encoding="utf-8")
            store = TaskStore(root / "screening.sqlite3")
            queued = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="candidate-001",
                    role="senior-fullstack-engineer",
                    jd_version="senior-fullstack-2026-08-14-v1",
                    rubric_version="senior-fullstack-2026-09-01-v6",
                )
            )
            claimed = store.claim_next()
            self.assertEqual(claimed.task_id, queued.task_id)

            store.requeue_stale(older_than_seconds=-1)

            interrupted = store.get(queued.task_id)
            self.assertEqual(interrupted.status, "manual_review")
            self.assertEqual(interrupted.error_code, "WORKER_INTERRUPTED_AMBIGUOUS")
            self.assertEqual(store.retry_failed(queued.task_id), 0)

    def test_manual_review_retry_requires_invalid_model_output_and_explicit_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text("业务系统支持与用户培训" * 30, encoding="utf-8")
            store = TaskStore(root / "screening.sqlite3")
            task = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="candidate-001",
                    role="business-system-operations-engineer",
                    jd_version="business-system-operations-engineer-2026-09-18-draft-v1",
                    rubric_version="business-system-operations-rubric-2026-09-18-v1",
                )
            )
            store.mark_manual_review(
                task.task_id,
                code="INVALID_MODEL_OUTPUT",
                message="invalid evidence state",
                model_completed=True,
            )

            self.assertEqual(store.status_counts(), {"manual_review": 1})
            self.assertEqual(store.retry_manual_review(task.task_id), 1)
            self.assertEqual(store.get(task.task_id).status, "queued")
            self.assertEqual(store.get(task.task_id).model_completed, False)
            self.assertEqual(store.retry_manual_review(task.task_id), 0)

    def test_same_resume_and_contract_is_enqueued_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text("Go 物流项目经历" * 30, encoding="utf-8")
            store = TaskStore(root / "screening.sqlite3")
            spec = TaskSpec(
                source_path=source,
                candidate_id="candidate-001",
                role="senior-fullstack-engineer",
                jd_version="senior-fullstack-2026-08-14-v1",
                rubric_version="senior-fullstack-2026-09-01-v6",
            )

            first = store.enqueue(spec)
            second = store.enqueue(spec)

            self.assertEqual(first.task_id, second.task_id)
            self.assertEqual(store.status_counts(), {"queued": 1})

    def test_contract_change_creates_a_new_task_for_rescreening(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text("Go 物流项目经历" * 30, encoding="utf-8")
            store = TaskStore(root / "screening.sqlite3")
            common = {
                "source_path": source,
                "candidate_id": "candidate-001",
                "role": "senior-fullstack-engineer",
                "jd_version": "senior-fullstack-2026-08-14-v1",
            }

            legacy = store.enqueue(
                TaskSpec(**common, rubric_version="senior-fullstack-2026-08-25-v5")
            )
            current = store.enqueue(
                TaskSpec(**common, rubric_version="senior-fullstack-2026-09-01-v6")
            )

            self.assertNotEqual(legacy.task_id, current.task_id)
            self.assertEqual(store.status_counts(), {"queued": 2})


if __name__ == "__main__":
    unittest.main()

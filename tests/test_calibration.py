from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from resume_screening.queue import TaskSpec, TaskStore


class CalibrationTests(unittest.TestCase):
    def test_non_capability_reasons_are_excluded_from_calibration_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "resume.md"
            source.write_text("Go 物流项目交付证据 " * 20, encoding="utf-8")
            store = TaskStore(root / "state.sqlite3")
            task = store.enqueue(
                TaskSpec(
                    source_path=source,
                    candidate_id="candidate-calibration",
                    role="senior-fullstack-engineer",
                    jd_version="senior-fullstack-2026-08-14-v1",
                    rubric_version="senior-fullstack-2026-09-01-v8",
                )
            )
            result = {
                "screening_record": {
                    "role": "senior-fullstack-engineer",
                    "evidence": [
                        {
                            "criterion_id": "SEN-BE-01",
                            "state": "supported",
                            "strength": "E2",
                        }
                    ],
                },
                "scorecard": {"score": 82},
            }
            store.claim_next()
            store.mark_succeeded(
                task.task_id, result=result, api_response_id=None, usage={}
            )
            reviews = root / "human-results.csv"
            with reviews.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=(
                        "task_id",
                        "human_conclusion",
                        "reason_category",
                        "criterion_id",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "task_id": task.task_id,
                        "human_conclusion": "do_not_advance",
                        "reason_category": "capability",
                        "criterion_id": "SEN-BE-01",
                    }
                )
                writer.writerow(
                    {
                        "task_id": task.task_id,
                        "human_conclusion": "advance",
                        "reason_category": "process_or_commercial",
                        "criterion_id": "SEN-BE-01",
                    }
                )

            self.assertEqual(store.import_human_reviews(reviews), 2)
            self.assertEqual(store.import_human_reviews(reviews), 0)
            report = store.calibration_report()

            self.assertEqual(report["sample_size"], 1)
            self.assertEqual(report["excluded_non_capability_sample_size"], 1)
            self.assertEqual(
                report["non_capability_reason_counts"]["process_or_commercial"], 1
            )
            self.assertEqual(
                report["score_distribution_by_status"]["do_not_advance"]["count"], 1
            )
            self.assertNotIn("advance", report["score_distribution_by_status"])
            self.assertEqual(
                report["recommendation_vs_human_capability_confusion_matrix"],
                {"unknown": {"do_not_advance": 1}},
            )
            self.assertFalse(report["sample_sufficient"])
            self.assertIn("不生成调权建议", report["sample_warning"])
            self.assertIsNone(report["weight_changes"])


if __name__ == "__main__":
    unittest.main()

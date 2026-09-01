from __future__ import annotations

import unittest

from resume_screening.scoring import score_record


def senior_record(*, recommendation: str = "advance_pending_human") -> dict:
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
    return {
        "role": "senior-fullstack-engineer",
        "rubric_version": "senior-fullstack-2026-09-01-v6",
        "model_recommendation": recommendation,
        "priority_profile": {
            "target_stack": "go_present",
            "refactoring_experience": "supported",
            "logistics_experience": "supported",
        },
        "evidence": [
            {"criterion_id": criterion, "state": "supported", "strength": "E3"}
            for criterion in criteria
        ],
    }


class DeterministicScoringTests(unittest.TestCase):
    def test_senior_weights_sum_to_100_and_logistics_is_15_points(self):
        record = senior_record()
        perfect = score_record(record)
        self.assertEqual(perfect.score, 100)
        self.assertEqual(perfect.grade, "A")

        domain = next(
            item
            for item in record["evidence"]
            if item["criterion_id"] == "SEN-DOMAIN-01"
        )
        domain.update(state="not_evidenced", strength="E0")
        without_logistics = score_record(record)
        self.assertEqual(without_logistics.score, 85)
        self.assertEqual(without_logistics.grade, "A")

    def test_evidence_grade_is_independent_from_review_status(self):
        second_review = senior_record(recommendation="second_review")
        reviewed = score_record(second_review)
        self.assertEqual(reviewed.grade, "A")
        self.assertEqual(reviewed.review_status, "second_review")

        missing_go = senior_record(recommendation="do_not_advance_pending_human")
        missing_go["priority_profile"]["target_stack"] = "no_qualifying_go"
        backend = next(
            item
            for item in missing_go["evidence"]
            if item["criterion_id"] == "SEN-BE-01"
        )
        backend.update(state="not_evidenced", strength="E0")
        gated = score_record(missing_go)
        self.assertEqual(gated.score, 80)
        self.assertEqual(gated.grade, "B")
        self.assertEqual(gated.review_status, "do_not_advance_pending_human")

        backend.update(state="directly_not_met", strength="E1")
        direct = score_record(missing_go)
        self.assertEqual(direct.score, 80)
        self.assertEqual(direct.grade, "B")
        self.assertEqual(direct.review_status, "do_not_advance_pending_human")

    def test_strength_conversion_is_fixed_and_not_model_supplied(self):
        record = senior_record()
        experience = next(
            item for item in record["evidence"] if item["criterion_id"] == "SEN-EXP-01"
        )
        experience["strength"] = "E1"
        result = score_record(record)
        self.assertEqual(result.score, 94)
        self.assertEqual(result.components["SEN-EXP-01"], 4)


if __name__ == "__main__":
    unittest.main()

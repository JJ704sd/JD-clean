#!/usr/bin/env python3
"""Tests for the operations/DevOps evidence-coverage scorecard."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from scoring import RUBRIC_VERSION, SCORING_VERSION, WEIGHTS, score_record
from render_conclusion import render
from validate_screening_output import (
    LEGACY_RUBRIC_VERSIONS,
    PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS,
    validate_record,
)
from validate_jd_profile import LEGACY_CRITERIA, V3_V4_CRITERIA


def load_example() -> dict:
    return json.loads(
        (SKILL_DIR / "references" / "example-record-v6.json").read_text(encoding="utf-8")
    )


def evidence_for(record: dict, criterion_id: str) -> dict:
    return next(item for item in record["evidence"] if item["criterion_id"] == criterion_id)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.record = load_example()

    def test_weights_cover_all_criteria_and_total_one_hundred(self):
        criterion_ids = {item["criterion_id"] for item in self.record["evidence"]}
        self.assertEqual(set(WEIGHTS), criterion_ids)
        self.assertEqual(sum(WEIGHTS.values()), 100)

    def test_example_scorecard_is_deterministic(self):
        result = score_record(self.record)
        example_path = SKILL_DIR / "references" / "example-scorecard-v6.json"
        expected = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(result, expected)
        self.assertEqual(result["score"], 62)
        self.assertEqual(result["grade"], "C")
        self.assertEqual(result["review_status"], "second_review")
        self.assertEqual(result["scoring_version"], SCORING_VERSION)
        self.assertEqual(result["rubric_version"], RUBRIC_VERSION)
        self.assertEqual(result["screening_record_id"], "sr-ops-v6-001")
        self.assertEqual(result["components"]["OPS-ENV-01"], 5.25)
        self.assertEqual(result["components"]["OPS-OBS-01"], 2.8)
        self.assertEqual(result["weights"]["OPS-EXP-01"], 10)
        self.assertEqual(result["weights"]["OPS-DELIVERY-01"], 8)
        self.assertEqual(result["weights"]["OPS-OWN-01"], 9)
        self.assertEqual(result["weights"]["OPS-SCALE-01"], 3)
        self.assertEqual(result["components"]["OPS-OWN-01"], 6.75)
        self.assertEqual(result["components"]["OPS-SCALE-01"], 0)

    def test_model_supplied_score_is_ignored(self):
        record = copy.deepcopy(self.record)
        record["scorecard"] = {"score": 100, "grade": "A"}
        self.assertEqual(score_record(record)["score"], 62)

    def test_conflicting_or_direct_contradiction_earns_zero(self):
        for state in ("conflicting", "directly_not_met"):
            with self.subTest(state=state):
                record = copy.deepcopy(self.record)
                evidence_for(record, "OPS-LINUX-01").update(
                    {"state": state, "strength": "E3"}
                )
                self.assertEqual(score_record(record)["components"]["OPS-LINUX-01"], 0)

    def test_omitted_evidence_is_not_redistributed(self):
        record = copy.deepcopy(self.record)
        linux = evidence_for(record, "OPS-LINUX-01")
        linux.update({"state": "not_evidenced", "strength": "E0"})
        self.assertEqual(score_record(record)["components"]["OPS-LINUX-01"], 0)
        self.assertEqual(score_record(record)["weights"]["OPS-LINUX-01"], 8)

    def test_rounding_is_half_up(self):
        record = copy.deepcopy(self.record)
        for item in record["evidence"]:
            item.update({"state": "not_evidenced", "strength": "E0"})
        # The score helper applies half-up rounding independently of profile validation.
        evidence_for(record, "OPS-EDU-01").update(
            {"state": "supported", "strength": "E2"}
        )
        result = score_record(record)
        self.assertEqual(result["components"]["OPS-EDU-01"], 1.5)
        self.assertEqual(result["score"], 2)

    def test_experience_fit_is_scored_without_changing_recommendation(self):
        record = copy.deepcopy(self.record)
        record["recommendation"] = "advance_pending_human"
        experience = evidence_for(record, "OPS-EXP-01")
        experience.update({"state": "directly_not_met", "strength": "E3"})
        result = score_record(record)
        self.assertEqual(result["components"]["OPS-EXP-01"], 0)
        self.assertEqual(result["review_status"], "advance_pending_human")

    def test_explicit_outsource_signal_has_no_score_effect(self):
        baseline = score_record(self.record)
        record = copy.deepcopy(self.record)
        evidence_for(record, "OPS-OUTSOURCE-01").update(
            {"state": "supported", "strength": "E3"}
        )
        result = score_record(record)
        self.assertEqual(result["components"]["OPS-OUTSOURCE-01"], 0)
        self.assertEqual(result["score"], baseline["score"])
        self.assertEqual(result["review_status"], baseline["review_status"])

    def test_scale_bonus_changes_score_without_changing_recommendation(self):
        baseline = score_record(self.record)
        record = copy.deepcopy(self.record)
        scale = evidence_for(record, "OPS-SCALE-01")
        scale.update({"state": "supported", "strength": "E3", "excerpt": "有明确峰值和容量保障", "location": "项目经历/容量治理"})
        baseline = score_record(record)
        scale = evidence_for(record, "OPS-SCALE-01")
        scale.update({"state": "not_evidenced", "strength": "E0", "excerpt": None, "location": None})
        result = score_record(record)
        self.assertEqual(baseline["components"]["OPS-SCALE-01"], 3)
        self.assertEqual(result["components"]["OPS-SCALE-01"], 0)
        self.assertEqual(baseline["score"] - result["score"], 3)
        self.assertEqual(result["review_status"], baseline["review_status"])

    def test_ai_product_bonus_and_project_ownership_are_separate_scored_dimensions(self):
        result = score_record(self.record)
        self.assertEqual(result["components"]["OPS-AI-BONUS-01"], 2.25)
        self.assertEqual(result["components"]["OPS-OWN-01"], 6.75)
        self.assertEqual(result["weights"]["OPS-AI-BONUS-01"], 3)
        self.assertEqual(result["weights"]["OPS-OWN-01"], 9)

    def test_historical_records_stay_readable_but_are_not_scored(self):
        historical = {
            **{version: LEGACY_CRITERIA for version in LEGACY_RUBRIC_VERSIONS},
            **{version: V3_V4_CRITERIA for version in PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS},
        }
        for legacy_version, criteria in historical.items():
            with self.subTest(version=legacy_version):
                record = copy.deepcopy(self.record)
                record["rubric_version"] = legacy_version
                record["schema_version"] = "1.2"
                record["jd_version"] = "operations-devops-engineer-2026-09-14-draft-v1"
                record["evidence"] = [
                    item for item in record["evidence"]
                    if item["criterion_id"] in criteria
                ]
                record["interview_probes"] = [
                    item for item in record["interview_probes"]
                    if item["criterion_id"] in criteria
                ]
                while len(record["interview_probes"]) < 3:
                    criterion_id = next(
                        criterion for criterion in criteria
                        if not any(item["criterion_id"] == criterion for item in record["interview_probes"])
                    )
                    record["interview_probes"].append({
                        "criterion_id": criterion_id,
                        "question": f"请补充说明 {criterion_id} 的个人动作和结果。",
                        "expected_signal": "能够给出可核对的个人动作、技术依据和结果。",
                    })
                record["summary"]["top_strengths"] = [
                    item for item in record["summary"]["top_strengths"]
                    if item["criterion_id"] in criteria
                ]
                self.assertEqual(validate_record(record), [])
                with self.assertRaisesRegex(ValueError, "not scored retroactively"):
                    score_record(record)

    def test_conclusion_renders_internal_dimension_scorecard(self):
        rendered = render(self.record)
        self.assertIn("内部证据覆盖分（非决策指标）", rendered)
        self.assertIn("62/100，等级 C", rendered)
        self.assertIn("Linux 与网络诊断", rendered)

    def test_legacy_conclusion_marks_score_unavailable(self):
        record = copy.deepcopy(self.record)
        record["rubric_version"] = next(iter(PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS))
        record["schema_version"] = "1.2"
        record["evidence"] = [
            item for item in record["evidence"] if item["criterion_id"] in V3_V4_CRITERIA
        ]
        rendered = render(record)
        self.assertIn("追溯计算分数", rendered)

    def test_unknown_rubric_is_rejected(self):
        record = copy.deepcopy(self.record)
        record["rubric_version"] = "operations-devops-rubric-unknown"
        self.assertIn("rubric_version", " ".join(validate_record(record)))


if __name__ == "__main__":
    unittest.main()

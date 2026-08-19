#!/usr/bin/env python3
"""Behavioral regression tests for screening conclusion and human-review consistency."""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
VALIDATOR_PATH = SKILL_DIR / "scripts" / "validate_screening_output.py"
EXAMPLE_PATH = SKILL_DIR / "references" / "example-record.json"

spec = importlib.util.spec_from_file_location("screening_validator", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


class ScreeningConclusionConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))

    def assert_error_contains(self, record: dict, expected: str) -> None:
        errors = validator.validate(record)
        self.assertTrue(
            any(expected in error for error in errors),
            msg=f"expected error containing {expected!r}, got {errors!r}",
        )

    def finalized_record(self) -> dict:
        record = copy.deepcopy(self.record)
        record["screening_status"] = "human_finalized"
        record["human_review"].update(
            {
                "level_1_reviewer": "reviewer-a",
                "level_1_decision": "advance",
                "level_1_reviewed_at": "2026-08-18T10:00:00+08:00",
                "level_2_reviewer": "reviewer-b",
                "level_2_decision": "advance",
                "level_2_reviewed_at": "2026-08-18T15:00:00+08:00",
                "reviewers_agree": True,
                "resolution_owner": "hiring-manager",
                "resolution": "进入结构化面试",
            }
        )
        return record

    def advance_record(self) -> dict:
        record = copy.deepcopy(self.record)
        record["recommendation"] = "advance_pending_human"
        record["summary"]["conclusion_label"] = "建议推进（待人工确认）"
        record["summary"]["human_review_requirement"] = "仅人工一审"
        record["summary"]["next_step"] = "由招聘责任人核对核心证据并完成人工一审"
        record["uncertainties"] = []
        record["human_review"].update(
            {
                "level_2_required": False,
                "level_2_mode": "not_required",
                "level_2_reason_codes": [],
                "prior_recommendations_hidden_during_recheck": False,
            }
        )
        return record

    def test_example_record_remains_valid(self) -> None:
        self.assertEqual(validator.validate(self.record), [])

    def test_top_strength_requires_supported_evidence(self) -> None:
        self.record["summary"]["top_strengths"] = [
            {"criterion_id": "AIPM-RISK-01", "finding": "风险治理能力很强"}
        ]
        self.assert_error_contains(self.record, "top_strengths requires supported evidence")

    def test_evidence_rejects_an_unapproved_extra_criterion(self) -> None:
        self.record["evidence"].append(
            {
                "criterion_id": "AIPM-PRESTIGE-01",
                "criterion_name": "公司与学校声望",
                "state": "supported",
                "strength": "E3",
                "excerpt": "来自知名公司",
                "location": "工作经历",
                "rationale": "不应进入岗位能力矩阵",
                "confidence": "high",
            }
        )
        self.assert_error_contains(self.record, "criterion_id is not allowed")

    def test_e1_weak_evidence_requires_excerpt_and_location(self) -> None:
        item = next(
            evidence for evidence in self.record["evidence"]
            if evidence["criterion_id"] == "AIPM-DATA-01"
        )
        self.assertEqual(item["strength"], "E1")
        item["excerpt"] = None
        item["location"] = None
        self.assert_error_contains(self.record, "excerpt is required for E1")
        item["excerpt"] = "搭建企业知识库"
        item["location"] = "项目经历/项目 A"
        self.assertEqual(validator.validate(self.record), [])

    def test_low_confidence_core_evidence_blocks_advance(self) -> None:
        record = self.advance_record()
        item = next(
            evidence for evidence in record["evidence"]
            if evidence["criterion_id"] == "AIPM-AI-01"
        )
        item["confidence"] = "low"
        self.assert_error_contains(record, "low-confidence decision evidence requires second review")

    def test_rubric_transition_accepts_legacy_and_current_but_rejects_unknown(self) -> None:
        legacy = copy.deepcopy(self.record)
        legacy["rubric_version"] = "ai-pm-rubric-2026-08-18-v2"
        self.assertEqual(validator.validate(legacy), [])

        current = copy.deepcopy(self.record)
        current["rubric_version"] = "ai-pm-rubric-2026-08-18-v3"
        self.assertEqual(validator.validate(current), [])

        unknown = copy.deepcopy(self.record)
        unknown["rubric_version"] = "ai-pm-rubric-custom"
        self.assert_error_contains(unknown, "rubric_version is not supported")

    def test_review_summary_must_match_human_review_gate(self) -> None:
        self.record["summary"]["human_review_requirement"] = "无需任何人工复核"
        self.assert_error_contains(self.record, "human_review_requirement does not match")

    def test_human_decisions_use_allowed_values(self) -> None:
        record = self.finalized_record()
        record["human_review"]["level_1_decision"] = "banana"
        record["human_review"]["level_2_decision"] = "banana"
        self.assert_error_contains(record, "level_1_decision is invalid")
        self.assert_error_contains(record, "level_2_decision is invalid")

    def test_reviewer_agreement_matches_decisions(self) -> None:
        record = self.finalized_record()
        record["human_review"]["level_1_decision"] = "do_not_advance"
        record["human_review"]["level_2_decision"] = "advance"
        record["human_review"]["reviewers_agree"] = True
        self.assert_error_contains(record, "reviewers_agree does not match reviewer decisions")

    def test_second_review_occurs_after_first_review(self) -> None:
        record = self.finalized_record()
        record["human_review"]["level_2_reviewed_at"] = "2026-08-18T09:00:00+08:00"
        self.assert_error_contains(record, "level 2 review must occur after level 1")

    def test_human_finalized_requires_explicit_validation_mode(self) -> None:
        record = self.finalized_record()
        self.assert_error_contains(record, "explicit human-finalized validation mode")
        self.assertEqual(validator.validate(record, allow_human_finalized=True), [])

    def test_non_final_record_cannot_claim_completed_human_review(self) -> None:
        self.record["human_review"].update(
            {
                "level_1_reviewer": "reviewer-a",
                "level_1_decision": "advance",
                "level_1_reviewed_at": "2026-08-18T10:00:00+08:00",
            }
        )
        self.assert_error_contains(self.record, "non_final record cannot contain completed human review")


if __name__ == "__main__":
    unittest.main()

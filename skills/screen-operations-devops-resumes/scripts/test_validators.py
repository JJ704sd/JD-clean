#!/usr/bin/env python3
"""Focused tests for the operations/DevOps skill validation gates."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from validate_jd_profile import LEGACY_CRITERIA, V3_V4_CRITERIA, validate_profile
from validate_screening_output import (
    LEGACY_RUBRIC_VERSIONS,
    PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS,
    validate_record,
)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ScreeningRecordTests(unittest.TestCase):
    def setUp(self):
        self.record = load_json(SKILL_DIR / "references" / "example-record-v6.json")

    def approved_profile(self):
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        profile["jd_version"] = "operations-devops-engineer-2026-09-14-approved-v1"
        profile["jd_hard_gates_approved"] = True
        profile["approved_by"] = [
            {"reviewer_id": "recruiter-01", "role": "recruiter"},
            {"reviewer_id": "manager-01", "role": "hiring_manager"},
        ]
        for criterion in profile["criteria"]:
            criterion["category"] = "preferred"
            criterion["approved"] = True
            criterion["proxy_risk"] = "none"
        return profile

    def approved_record_base(self, profile):
        record = copy.deepcopy(self.record)
        record["summary"]["key_gaps"] = record["summary"]["key_gaps"][:1]
        record["screening_basis"] = "approved_jd"
        record["jd_hard_gates_approved"] = True
        record["jd_version"] = profile["jd_version"]
        record["recommendation"] = "advance_pending_human"
        record["summary"]["conclusion_label"] = "建议推进（待人工一审）"
        record["uncertainties"] = []
        record["human_review"]["level_2_required"] = False
        record["human_review"]["level_2_status"] = "not_required"
        record["human_review"]["level_2_mode"] = "not_required"
        record["human_review"]["level_2_reason_codes"] = []
        record["human_review"]["blind_review_required"] = False
        return record

    def historical_record(self, version, criteria):
        record = copy.deepcopy(self.record)
        record["schema_version"] = "1.2"
        record["rubric_version"] = version
        record["jd_version"] = "operations-devops-engineer-2026-09-14-draft-v1"
        record["evidence"] = [
            item for item in record["evidence"] if item["criterion_id"] in criteria
        ]
        probes = [
            item for item in record["interview_probes"]
            if item["criterion_id"] in criteria
        ]
        for criterion_id in criteria:
            if len(probes) >= 3:
                break
            if not any(item["criterion_id"] == criterion_id for item in probes):
                probes.append({
                    "criterion_id": criterion_id,
                    "question": f"请补充说明 {criterion_id} 的个人动作和结果。",
                    "expected_signal": "能够给出可核对的个人动作、技术依据和结果。",
                })
        record["interview_probes"] = probes[:3]
        record["summary"]["top_strengths"] = [
            item for item in record["summary"]["top_strengths"]
            if item["criterion_id"] in criteria
        ]
        return record

    def test_draft_profile_and_provisional_example_are_valid(self):
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        self.assertEqual(profile["schema_version"], "1.3")
        self.assertEqual(len(profile["criteria"]), 20)
        self.assertEqual(self.record["schema_version"], "1.4")
        self.assertEqual(validate_profile(profile), [])
        self.assertEqual(validate_record(self.record), [])

    def test_rubric_v3_v4_profile_and_record_remain_readable(self):
        self.assertEqual(len(V3_V4_CRITERIA), 16)
        for version in PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS:
            with self.subTest(rubric_version=version):
                profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
                profile["schema_version"] = "1.1"
                profile["rubric_version"] = version
                profile["criteria"] = [
                    item for item in profile["criteria"] if item["criterion_id"] in V3_V4_CRITERIA
                ]
                record = self.historical_record(version, V3_V4_CRITERIA)
                self.assertEqual(validate_profile(profile), [])
                self.assertEqual(validate_record(record, profile), [])

                # Approved v3/v4 profiles remain pairable with their historical records.
                approved_profile = self.approved_profile()
                approved_profile["schema_version"] = "1.1"
                approved_profile["rubric_version"] = version
                approved_profile["criteria"] = [
                    item for item in approved_profile["criteria"]
                    if item["criterion_id"] in V3_V4_CRITERIA
                ]
                approved_record = self.historical_record(version, V3_V4_CRITERIA)
                approved_record["screening_basis"] = "approved_jd"
                approved_record["jd_hard_gates_approved"] = True
                approved_record["jd_version"] = approved_profile["jd_version"]
                approved_record["recommendation"] = "advance_pending_human"
                approved_record["summary"]["conclusion_label"] = "建议推进（待人工一审）"
                approved_record["uncertainties"] = []
                approved_record["human_review"]["level_2_required"] = False
                approved_record["human_review"]["level_2_status"] = "not_required"
                approved_record["human_review"]["level_2_mode"] = "not_required"
                approved_record["human_review"]["level_2_reason_codes"] = []
                approved_record["human_review"]["blind_review_required"] = False
                approved_record["jd_version"] = approved_profile["jd_version"]
                self.assertEqual(validate_profile(approved_profile), [])
                self.assertEqual(validate_record(approved_record, approved_profile), [])

    def test_legacy_schema_1_0_profile_and_rubric_v2_record_remain_readable(self):
        self.assertEqual(len(LEGACY_CRITERIA), 13)
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        profile["schema_version"] = "1.0"
        profile.pop("rubric_version")
        profile["criteria"] = [
            item for item in profile["criteria"] if item["criterion_id"] in LEGACY_CRITERIA
        ]
        self.assertEqual(validate_profile(profile), [])

        for version in LEGACY_RUBRIC_VERSIONS:
            with self.subTest(rubric_version=version):
                record = self.historical_record(version, LEGACY_CRITERIA)
                self.assertEqual(validate_record(record), [])

        profile["jd_version"] = "operations-devops-engineer-2026-09-14-approved-legacy"
        profile["jd_hard_gates_approved"] = True
        profile["approved_by"] = [
            {"reviewer_id": "recruiter-legacy", "role": "recruiter"},
            {"reviewer_id": "manager-legacy", "role": "hiring_manager"},
        ]
        for criterion in profile["criteria"]:
            criterion["category"] = "preferred"
            criterion["approved"] = True
            criterion["proxy_risk"] = "none"
        self.assertEqual(validate_profile(profile), [])

        approved_record = self.historical_record("operations-devops-rubric-2026-09-14-v2", LEGACY_CRITERIA)
        approved_record["screening_basis"] = "approved_jd"
        approved_record["jd_hard_gates_approved"] = True
        approved_record["jd_version"] = profile["jd_version"]
        approved_record["rubric_version"] = "operations-devops-rubric-2026-09-14-v2"
        self.assertEqual(validate_record(approved_record, profile), [])

    def test_schema_versions_require_their_matching_rubric_and_criterion_sets(self):
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        profile["criteria"] = [
            item for item in profile["criteria"] if item["criterion_id"] != "OPS-SCALE-01"
        ]
        errors = validate_profile(profile)
        self.assertTrue(any("criteria IDs must exactly match schema '1.3'" in error for error in errors))

        record = copy.deepcopy(self.record)
        record["schema_version"] = "1.3"
        errors = validate_record(record)
        self.assertTrue(any("schema_version must be '1.4'" in error for error in errors))

        historical = copy.deepcopy(self.record)
        historical["rubric_version"] = next(iter(PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS))
        historical["schema_version"] = "1.3"
        historical["evidence"] = [
            item for item in historical["evidence"] if item["criterion_id"] in V3_V4_CRITERIA
        ]
        errors = validate_record(historical)
        self.assertTrue(any("schema_version must be '1.2'" in error for error in errors))

    def test_approved_record_rubric_must_match_profile_rubric(self):
        profile = self.approved_profile()
        record = self.approved_record_base(profile)
        record["rubric_version"] = next(iter(PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS))
        record["schema_version"] = "1.2"
        record["evidence"] = [
            item for item in record["evidence"] if item["criterion_id"] in V3_V4_CRITERIA
        ]
        errors = validate_record(record, profile)
        self.assertTrue(any("record rubric_version does not match approved profile" in error for error in errors))

    def test_draft_profile_cannot_declare_active_hard_gate(self):
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        profile["criteria"][0]["category"] = "must_have"
        profile["criteria"][0]["approved"] = True
        errors = validate_profile(profile)
        self.assertTrue(any("draft profile cannot declare an active must_have" in error for error in errors))

    def test_approved_profile_requires_a_new_non_draft_version(self):
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        profile["jd_hard_gates_approved"] = True
        profile["approved_by"] = [
            {"reviewer_id": "recruiter-01", "role": "recruiter"},
            {"reviewer_id": "manager-01", "role": "hiring_manager"},
        ]
        for criterion in profile["criteria"]:
            criterion["category"] = "preferred"
            criterion["approved"] = True
            criterion["proxy_risk"] = "none"
        errors = validate_profile(profile)
        self.assertTrue(any("new non-draft jd_version" in error for error in errors))

    def test_current_experience_ownership_ai_scale_and_domain_preferences_cannot_be_hard_gates(self):
        preference_only = {"OPS-EXP-01", "OPS-OWN-01", "OPS-AI-BONUS-01", "OPS-SCALE-01", "OPS-DOMAIN-01"}
        for criterion_id in preference_only:
            for category in ("must_have", "exclude_if_evidenced"):
                with self.subTest(criterion_id=criterion_id, category=category):
                    profile = self.approved_profile()
                    criterion = next(
                        item for item in profile["criteria"]
                        if item["criterion_id"] == criterion_id
                    )
                    criterion["category"] = category
                    criterion["resume_observable"] = True
                    criterion["missing_information_action"] = (
                        "second_review" if category == "must_have" else "no_effect"
                    )
                    errors = validate_profile(profile)
                    self.assertTrue(
                        any(f"{criterion_id} is preference-only" in error for error in errors),
                        errors,
                    )

    def test_missing_or_low_scale_evidence_is_not_a_gap_or_hard_gate(self):
        profile = self.approved_profile()
        scale_requirement = next(
            item for item in profile["criteria"] if item["criterion_id"] == "OPS-SCALE-01"
        )
        self.assertEqual(scale_requirement["category"], "preferred")

        for strength, excerpt, location in (
            ("E0", None, None),
            ("E1", "参与某内部业务平台运维", "项目经历/业务平台"),
        ):
            with self.subTest(strength=strength):
                record = self.approved_record_base(profile)
                scale = next(
                    item for item in record["evidence"]
                    if item["criterion_id"] == "OPS-SCALE-01"
                )
                scale.update({
                    "state": "not_evidenced",
                    "strength": strength,
                    "excerpt": excerpt,
                    "location": location,
                    "rationale": "简历未提供可核验的业务量、用户量或容量复杂度证据。",
                })
                self.assertNotIn("OPS-SCALE-01", {
                    item["criterion_id"] for item in record["summary"]["key_gaps"]
                })
                self.assertNotIn("OPS-SCALE-01", {
                    item["criterion_id"] for item in record["hard_gate_conflicts"]
                })
                self.assertEqual(validate_record(record, profile), [])

        record = self.approved_record_base(profile)
        scale = next(
            item for item in record["evidence"] if item["criterion_id"] == "OPS-SCALE-01"
        )
        scale.update({
            "state": "not_evidenced",
            "strength": "E0",
            "excerpt": None,
            "location": None,
            "rationale": "简历未提供可核验的业务量、用户量或容量复杂度证据。",
        })
        record["summary"]["key_gaps"].append({
            "criterion_id": "OPS-SCALE-01",
            "gap_type": "evidence_gap",
            "finding": "简历未提供业务规模证据。",
        })
        errors = validate_record(record, profile)
        self.assertTrue(any("cannot treat missing bonus-only evidence as a key gap" in error for error in errors))

    def test_scale_evidence_cannot_be_directly_not_met(self):
        record = copy.deepcopy(self.record)
        scale = next(item for item in record["evidence"] if item["criterion_id"] == "OPS-SCALE-01")
        scale.update({
            "state": "directly_not_met",
            "strength": "E2",
            "excerpt": "项目业务量未达到预期规模。",
            "location": "项目经历/业务平台",
            "rationale": "偏好项只能记录规模证据是否充分，不得形成反向淘汰结论。",
        })
        errors = validate_record(record)
        self.assertTrue(any("OPS-SCALE-01 is preference-only and cannot be directly_not_met" in error for error in errors))

    def test_distinct_scoring_dimensions_cannot_reuse_the_same_cited_fact(self):
        for left_id, right_id in (
            ("OPS-OWN-01", "OPS-SCALE-01"),
            ("OPS-AI-BONUS-01", "OPS-MODEL-01"),
            ("OPS-AI-BONUS-01", "OPS-RAG-01"),
            ("OPS-AI-BONUS-01", "OPS-AI-GOV-01"),
        ):
            with self.subTest(left_id=left_id, right_id=right_id):
                record = copy.deepcopy(self.record)
                left = next(item for item in record["evidence"] if item["criterion_id"] == left_id)
                right = next(item for item in record["evidence"] if item["criterion_id"] == right_id)
                right["excerpt"] = left["excerpt"]
                right["location"] = left["location"]
                right["state"] = "supported"
                right["strength"] = "E2"
                errors = validate_record(record)
                self.assertTrue(
                    any(f"{left_id} and {right_id} reuse the same cited fact" in error for error in errors),
                    errors,
                )

    def test_provisional_record_cannot_be_negative(self):
        record = copy.deepcopy(self.record)
        record["recommendation"] = "do_not_advance_pending_human"
        record["summary"]["conclusion_label"] = "暂不推进（待人工一审与二次复核）"
        errors = validate_record(record)
        self.assertTrue(any("provisional_baseline" in error for error in errors))
        self.assertTrue(any("direct approved hard-gate conflict" in error for error in errors))

    def test_technical_keyword_is_not_supported_evidence(self):
        record = copy.deepcopy(self.record)
        item = next(entry for entry in record["evidence"] if entry["criterion_id"] == "OPS-LINUX-01")
        item["strength"] = "E1"
        errors = validate_record(record)
        self.assertTrue(any("supported requires E2/E3" in error for error in errors))

    def test_uncertainty_requires_second_review(self):
        record = copy.deepcopy(self.record)
        record["recommendation"] = "advance_pending_human"
        record["summary"]["conclusion_label"] = "建议推进（待人工一审）"
        errors = validate_record(record)
        self.assertTrue(any("provisional_baseline must use second_review" in error for error in errors))
        self.assertTrue(any("decision-relevant uncertainties require second_review" in error for error in errors))

    def test_uncertainty_requires_impact_and_human_action(self):
        for field in ("description", "decision_impact", "required_human_action"):
            with self.subTest(field=field):
                record = copy.deepcopy(self.record)
                record["uncertainties"][0][field] = "x"
                errors = validate_record(record)
                self.assertTrue(any(f"uncertainties[0].{field}" in error for error in errors))

    def test_summary_strengths_and_gaps_must_match_evidence_state(self):
        record = copy.deepcopy(self.record)
        record["summary"]["top_strengths"][0]["criterion_id"] = "OPS-OBS-01"
        errors = validate_record(record)
        self.assertTrue(any("must reference E2/E3 supported capability evidence" in error for error in errors))

    def test_key_gap_type_must_match_evidence_state(self):
        record = copy.deepcopy(self.record)
        record["summary"]["key_gaps"][0]["criterion_id"] = "OPS-LINUX-01"
        errors = validate_record(record)
        self.assertTrue(any("gap_type does not match evidence state" in error for error in errors))

    def test_partial_gap_type_is_not_allowed_for_supported_evidence(self):
        record = copy.deepcopy(self.record)
        gap = record["summary"]["key_gaps"][0]
        gap["criterion_id"] = "OPS-LINUX-01"
        gap["gap_type"] = "partial_evidence"
        gap["finding"] = "简历完全没有 Linux 运维证据"
        errors = validate_record(record)
        self.assertTrue(any("gap_type is invalid" in error for error in errors))

    def test_conflict_gap_matches_conflicting_evidence(self):
        record = copy.deepcopy(self.record)
        item = next(entry for entry in record["evidence"] if entry["criterion_id"] == "OPS-OBS-01")
        item.update(
            {
                "state": "conflicting",
                "strength": "E2",
                "excerpt": "一处写告警由我负责，另一处将其列为团队统一职责。",
                "location": "项目经历/监控建设",
                "rationale": "同一维度的职责描述互相矛盾。",
            }
        )
        record["summary"]["key_gaps"][0]["gap_type"] = "conflicting_facts"
        record["uncertainties"].append(
            {
                "code": "U03_CONFLICTING_FACTS",
                "description": "监控告警责任在两处描述中不一致。",
                "decision_impact": "该职责归属可能改变稳定性维度的证据强度。",
                "required_human_action": "请复核原始简历两处项目描述并确认责任边界。",
                "requires_second_review": True,
            }
        )
        record["human_review"]["level_2_reason_codes"].append("U03_CONFLICTING_FACTS")
        self.assertEqual(validate_record(record), [])

    def test_administrative_fact_cannot_be_a_strength(self):
        record = copy.deepcopy(self.record)
        record["summary"]["top_strengths"][0]["criterion_id"] = "OPS-EDU-01"
        errors = validate_record(record)
        self.assertTrue(any("cannot use an administrative fact as a strength" in error for error in errors))

    def test_outsource_exclusion_signal_cannot_be_a_top_strength(self):
        record = copy.deepcopy(self.record)
        outsource = next(
            item for item in record["evidence"] if item["criterion_id"] == "OPS-OUTSOURCE-01"
        )
        outsource.update({
            "state": "supported",
            "strength": "E2",
            "excerpt": "候选人以第三方外包人员身份驻场运维。",
            "location": "工作经历/平台运维",
            "rationale": "外包经历只用于已校准的排除判断，不属于可宣传的岗位能力优势。",
        })
        record["summary"]["top_strengths"] = record["summary"]["top_strengths"][:2]
        record["summary"]["top_strengths"].append({
            "criterion_id": "OPS-OUTSOURCE-01",
            "finding": "有外包驻场经验。",
        })
        errors = validate_record(record)
        self.assertTrue(any("cannot use an administrative fact as a strength" in error for error in errors))

    def test_approved_hard_gate_conflict_requires_independent_second_review(self):
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        profile["jd_version"] = "operations-devops-engineer-2026-09-14-v1"
        profile["jd_hard_gates_approved"] = True
        profile["approved_by"] = [
            {"reviewer_id": "recruiter-01", "role": "recruiter"},
            {"reviewer_id": "manager-01", "role": "hiring_manager"},
        ]
        for criterion in profile["criteria"]:
            criterion["category"] = "preferred"
            criterion["approved"] = True
            criterion["proxy_risk"] = "none"
            if criterion["criterion_id"] == "OPS-EDU-01":
                criterion["category"] = "must_have"

        record = copy.deepcopy(self.record)
        record["screening_basis"] = "approved_jd"
        record["jd_hard_gates_approved"] = True
        record["jd_version"] = profile["jd_version"]
        record["recommendation"] = "do_not_advance_pending_human"
        record["summary"]["conclusion_label"] = "暂不推进（待人工一审与二次复核）"
        record["summary"]["human_review_requirement"] = "待独立审核人核对批准的学历门槛及原文。"
        record["summary"]["key_gaps"] = record["summary"]["key_gaps"][:2]
        record["summary"]["key_gaps"].append(
            {
                "criterion_id": "OPS-EDU-01",
                "gap_type": "direct_contradiction",
                "finding": "简历明确列出的学历与批准的门槛冲突。",
            }
        )
        record["uncertainties"] = []
        record["hard_gate_conflicts"] = [
            {
                "criterion_id": "OPS-EDU-01",
                "excerpt": "大专",
                "location": "教育经历",
            }
        ]
        education = next(entry for entry in record["evidence"] if entry["criterion_id"] == "OPS-EDU-01")
        education.update(
            {
                "state": "directly_not_met",
                "strength": "E1",
                "excerpt": "大专",
                "location": "教育经历",
                "rationale": "简历明确列出学历层次，且 approved profile 将本科列为 hard gate。",
            }
        )
        experience = next(entry for entry in record["evidence"] if entry["criterion_id"] == "OPS-EXP-01")
        experience.update(
            {
                "state": "supported",
                "strength": "E2",
                "excerpt": "2019-2024 年负责平台运维与发布系统",
                "location": "工作经历/平台运维",
                "rationale": "可定位的日期与职责显示相关经验满足当前 profile 的年限要求。",
            }
        )
        record["human_review"]["level_2_reason_codes"] = ["H02_NEGATIVE_RECOMMENDATION"]
        errors = validate_record(record, profile)
        self.assertEqual(errors, [])

        record["human_review"]["level_2_mode"] = "same_owner_separate_pass"
        errors = validate_record(record, profile)
        self.assertTrue(any("negative recommendation requires independent_reviewer" in error for error in errors))

    def test_approved_record_requires_approved_profile(self):
        record = copy.deepcopy(self.record)
        record["screening_basis"] = "approved_jd"
        record["jd_hard_gates_approved"] = True
        errors = validate_record(record)
        self.assertTrue(any("--jd-profile" in error for error in errors))

    def test_advance_requires_production_and_ai_runtime_evidence(self):
        profile = load_json(SKILL_DIR / "references" / "jd-profile-template.json")
        profile["jd_version"] = "operations-devops-engineer-2026-09-14-v1"
        profile["jd_hard_gates_approved"] = True
        profile["approved_by"] = [
            {"reviewer_id": "recruiter-01", "role": "recruiter"},
            {"reviewer_id": "manager-01", "role": "hiring_manager"},
        ]
        for criterion in profile["criteria"]:
            criterion["category"] = "preferred"
            criterion["approved"] = True
            criterion["proxy_risk"] = "none"

        record = copy.deepcopy(self.record)
        record["screening_basis"] = "approved_jd"
        record["jd_hard_gates_approved"] = True
        record["jd_version"] = profile["jd_version"]
        record["recommendation"] = "advance_pending_human"
        record["summary"]["conclusion_label"] = "建议推进（待人工一审）"
        record["uncertainties"] = []
        record["human_review"]["level_2_required"] = False
        record["human_review"]["level_2_status"] = "not_required"
        record["human_review"]["level_2_mode"] = "not_required"
        record["human_review"]["level_2_reason_codes"] = []
        record["human_review"]["blind_review_required"] = False
        self.assertEqual(validate_record(record, profile), [])

        model_evidence = next(entry for entry in record["evidence"] if entry["criterion_id"] == "OPS-MODEL-01")
        model_evidence.update(
            {
                "state": "not_evidenced",
                "strength": "E0",
                "excerpt": None,
                "location": None,
                "rationale": "没有模型 API 或 RAG/Agent 线上运维证据。",
            }
        )
        errors = validate_record(record, profile)
        self.assertTrue(any("model API or RAG/Agent operations evidence" in error for error in errors))

    def test_approved_education_gate_supports_explicit_direct_conflict(self):
        profile = self.approved_profile()
        education_gate = next(item for item in profile["criteria"] if item["criterion_id"] == "OPS-EDU-01")
        education_gate.update({
            "category": "must_have",
            "requirement_text": "第一学历须本科及以上",
            "missing_information_action": "second_review",
        })
        self.assertEqual(validate_profile(profile), [])

        record = self.approved_record_base(profile)
        record["recommendation"] = "do_not_advance_pending_human"
        record["summary"]["conclusion_label"] = "暂不推进（待人工一审与二次复核）"
        record["summary"]["key_gaps"] = [{
            "criterion_id": "OPS-EDU-01",
            "gap_type": "direct_contradiction",
            "finding": "简历明确第一学历为大专。",
        }]
        record["hard_gate_conflicts"] = [{
            "criterion_id": "OPS-EDU-01",
            "excerpt": "第一学历：大专",
            "location": "教育经历",
        }]
        education = next(item for item in record["evidence"] if item["criterion_id"] == "OPS-EDU-01")
        education.update({
            "state": "directly_not_met",
            "strength": "E1",
            "excerpt": "第一学历：大专",
            "location": "教育经历",
            "rationale": "简历明确列出的第一学历低于已批准的本科门槛。",
        })
        record["human_review"]["level_2_required"] = True
        record["human_review"]["level_2_status"] = "pending"
        record["human_review"]["level_2_mode"] = "independent_reviewer"
        record["human_review"]["level_2_reason_codes"] = ["H02_NEGATIVE_RECOMMENDATION"]
        record["human_review"]["blind_review_required"] = True
        self.assertEqual(validate_record(record, profile), [])

    def test_approved_exclusion_gate_only_triggers_on_explicit_outsource_evidence(self):
        profile = self.approved_profile()
        exclusion = next(item for item in profile["criteria"] if item["criterion_id"] == "OPS-OUTSOURCE-01")
        exclusion.update({
            "category": "exclude_if_evidenced",
            "missing_information_action": "no_effect",
            "proxy_risk": "none",
        })
        self.assertEqual(validate_profile(profile), [])

        record = self.approved_record_base(profile)
        self.assertEqual(record["hard_gate_conflicts"], [])
        self.assertEqual(validate_record(record, profile), [])

        record["recommendation"] = "do_not_advance_pending_human"
        record["summary"]["conclusion_label"] = "暂不推进（待人工一审与二次复核）"
        record["summary"]["key_gaps"].append({
            "criterion_id": "OPS-OUTSOURCE-01",
            "gap_type": "direct_contradiction",
            "finding": "简历明确说明候选人以外包身份任职。",
        })
        outsource = next(item for item in record["evidence"] if item["criterion_id"] == "OPS-OUTSOURCE-01")
        outsource.update({
            "state": "directly_not_met",
            "strength": "E2",
            "excerpt": "以第三方外包人员身份驻场运维。",
            "location": "工作经历/平台运维",
            "rationale": "简历明确说明候选人本人的用工形式，符合已批准的排除条件。",
        })
        record["hard_gate_conflicts"] = [{
            "criterion_id": "OPS-OUTSOURCE-01",
            "excerpt": "以第三方外包人员身份驻场运维。",
            "location": "工作经历/平台运维",
        }]
        record["human_review"]["level_2_required"] = True
        record["human_review"]["level_2_status"] = "pending"
        record["human_review"]["level_2_mode"] = "independent_reviewer"
        record["human_review"]["level_2_reason_codes"] = ["H02_NEGATIVE_RECOMMENDATION"]
        record["human_review"]["blind_review_required"] = True
        self.assertEqual(validate_record(record, profile), [])

    def test_five_year_experience_preference_is_not_a_hard_gate(self):
        profile = self.approved_profile()
        education_gate = next(item for item in profile["criteria"] if item["criterion_id"] == "OPS-EDU-01")
        education_gate.update({
            "category": "must_have",
            "requirement_text": "第一学历须本科及以上",
            "missing_information_action": "second_review",
        })
        experience_preference = next(item for item in profile["criteria"] if item["criterion_id"] == "OPS-EXP-01")
        experience_preference["requirement_text"] = "5 年及以上相关运维经验；当前 profile 下不构成自动淘汰"
        self.assertEqual(validate_profile(profile), [])

        record = self.approved_record_base(profile)
        experience = next(item for item in record["evidence"] if item["criterion_id"] == "OPS-EXP-01")
        experience.update({
            "state": "directly_not_met",
            "strength": "E2",
            "excerpt": "2024 年起负责平台运维，累计相关经验约 2 年。",
            "location": "工作经历/平台运维",
            "rationale": "相关经验低于最新 JD 期望，但当前范围不是已批准硬门槛。",
            "confidence": "high",
        })
        # 偏好区间外仍保留评分证据，但不作为硬门槛缺口。
        self.assertEqual(record["hard_gate_conflicts"], [])
        self.assertEqual(validate_record(record, profile), [])


if __name__ == "__main__":
    unittest.main()

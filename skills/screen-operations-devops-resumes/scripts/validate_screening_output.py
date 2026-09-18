#!/usr/bin/env python3
"""Validate operations/DevOps screening records and human-review gates."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

from validate_jd_profile import CRITERIA as PROFILE_CRITERIA
from validate_jd_profile import LEGACY_CRITERIA
from validate_jd_profile import V5_CRITERIA
from validate_jd_profile import V3_V4_CRITERIA
from validate_jd_profile import validate_profile

ROLE = "operations-devops-engineer"
V5_JD_VERSION = "operations-devops-engineer-2026-09-14-draft-v1"
JD_VERSION = "operations-devops-engineer-2026-09-17-draft-v1"
V5_RUBRIC_VERSION = "operations-devops-rubric-2026-09-14-v5"
RUBRIC_VERSION = "operations-devops-rubric-2026-09-17-v1"
PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS = {
    "operations-devops-rubric-2026-09-14-v3",
    "operations-devops-rubric-2026-09-14-v4",
}
LEGACY_RUBRIC_VERSIONS = {
    "operations-devops-rubric-2026-09-14-v1",
    "operations-devops-rubric-2026-09-14-v2",
}
SUPPORTED_RUBRIC_VERSIONS = LEGACY_RUBRIC_VERSIONS | {
    *PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS,
    V5_RUBRIC_VERSION,
    RUBRIC_VERSION,
}
CRITERIA_BY_RUBRIC = {
    **{version: LEGACY_CRITERIA for version in LEGACY_RUBRIC_VERSIONS},
    **{version: V3_V4_CRITERIA for version in PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS},
    V5_RUBRIC_VERSION: V5_CRITERIA,
    RUBRIC_VERSION: PROFILE_CRITERIA,
}
RECORD_SCHEMA_BY_RUBRIC = {
    **{version: "1.2" for version in LEGACY_RUBRIC_VERSIONS},
    **{version: "1.2" for version in PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS},
    V5_RUBRIC_VERSION: "1.3",
    RUBRIC_VERSION: "1.4",
}
JD_VERSION_BY_RUBRIC = {
    **{version: V5_JD_VERSION for version in LEGACY_RUBRIC_VERSIONS},
    **{version: V5_JD_VERSION for version in PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS},
    V5_RUBRIC_VERSION: V5_JD_VERSION,
    RUBRIC_VERSION: JD_VERSION,
}
RECOMMENDATIONS = {
    "advance_pending_human",
    "second_review",
    "do_not_advance_pending_human",
}
STATES = {"supported", "not_evidenced", "conflicting", "directly_not_met"}
STRENGTHS = {"E0", "E1", "E2", "E3"}
CONFIDENCES = {"high", "medium", "low"}
UNCERTAINTIES = {
    "U01_PARSE_QUALITY",
    "U02_MUST_HAVE_MISSING",
    "U03_CONFLICTING_FACTS",
    "U04_CONTRIBUTION_UNCLEAR",
    "U05_TRANSFERABILITY",
    "U06_BOUNDARY_CASE",
    "U07_BIAS_OR_PROXY",
    "U08_DIMENSION_CONFLICT",
    "U09_ROLE_AMBIGUITY",
    "U10_RUBRIC_AMBIGUITY",
    "U11_UNTRUSTED_CONTENT",
}
L2_REASONS = UNCERTAINTIES | {"H02_NEGATIVE_RECOMMENDATION", "H03_BATCH_AUDIT"}
CRITERION_NAMES = {
    "OPS-EDU-01": "学历与专业",
    "OPS-EXP-01": "相关经验年限",
    "OPS-LINUX-01": "Linux 与网络诊断",
    "OPS-ENV-01": "环境、资源与云平台",
    "OPS-DB-01": "PostgreSQL、Redis 与消息队列",
    "OPS-DELIVERY-01": "容器、CI/CD 与变更",
    "OPS-OBS-01": "可观测、SLO 与事故闭环",
    "OPS-SEC-DR-01": "安全、凭据、审计与灾备",
    "OPS-MODEL-01": "模型 API 与网关运维",
    "OPS-RAG-01": "向量检索、RAG 与 Agent 运行链路",
    "OPS-AI-GOV-01": "AI 成本、效果和版本变更",
    "OPS-AUTO-COST-01": "自动化、IaC 与资源成本",
    "OPS-COLLAB-01": "协作、文档与复盘",
    "OPS-OWN-01": "项目独立承担与核心责任",
    "OPS-AI-BONUS-01": "AI 深度使用与 AI 产品经验",
    "OPS-OUTSOURCE-01": "外包/派遣经历排除信号（非计分）",
    "OPS-SCALE-01": "运维项目复杂度与业务/用户规模",
    "OPS-OFFICE-IT-01": "公司电脑与办公 IT 管理",
    "OPS-OFFICE-NET-01": "办公网络与 VPN 管理",
    "OPS-DOMAIN-01": "物流、供应链或跨境行业经验",
}
FOUNDATION_CRITERIA = {
    "OPS-LINUX-01",
    "OPS-ENV-01",
    "OPS-DB-01",
    "OPS-DELIVERY-01",
    "OPS-OBS-01",
    "OPS-SEC-DR-01",
}
AI_RUNTIME_CRITERIA = {"OPS-MODEL-01", "OPS-RAG-01"}
DISTINCT_EVIDENCE_PAIRS = (
    ("OPS-OWN-01", "OPS-SCALE-01"),
    ("OPS-AI-BONUS-01", "OPS-MODEL-01"),
    ("OPS-AI-BONUS-01", "OPS-RAG-01"),
    ("OPS-AI-BONUS-01", "OPS-AI-GOV-01"),
)
GAP_TYPES = {
    "evidence_gap",
    "conflicting_facts",
    "direct_contradiction",
}
SUMMARY_LABELS = {
    "advance_pending_human": "建议推进（待人工一审）",
    "second_review": "建议二次复核",
    "do_not_advance_pending_human": "暂不推进（待人工一审与二次复核）",
}


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_null(value: Any) -> bool:
    return value is None


def normalized_evidence_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def validate_record(record: Any, jd_profile: Any | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    required = {
        "schema_version",
        "screening_record_id",
        "candidate_id",
        "role",
        "screening_basis",
        "jd_hard_gates_approved",
        "jd_version",
        "rubric_version",
        "screening_status",
        "recommendation",
        "summary",
        "hard_gate_conflicts",
        "evidence",
        "uncertainties",
        "interview_probes",
        "sensitive_attributes_used",
        "human_review",
        "automation_actions",
    }
    missing = sorted(required - record.keys())
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")
        return errors
    for key in ("screening_record_id", "candidate_id", "jd_version", "rubric_version"):
        if not nonempty(record.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if "candidate_name" in record and not nonempty(record.get("candidate_name")):
        errors.append("candidate_name must be a non-empty string when present")
    if record.get("role") != ROLE:
        errors.append(f"role must be {ROLE!r}")
    basis = record.get("screening_basis")
    if basis not in {"approved_jd", "provisional_baseline"}:
        errors.append("screening_basis must be approved_jd or provisional_baseline")
    rubric_version = record.get("rubric_version")
    if rubric_version not in SUPPORTED_RUBRIC_VERSIONS:
        errors.append(
            "rubric_version must be one of "
            f"{sorted(SUPPORTED_RUBRIC_VERSIONS)}"
        )
    expected_schema_version = RECORD_SCHEMA_BY_RUBRIC.get(rubric_version)
    if record.get("schema_version") != expected_schema_version:
        errors.append(f"schema_version must be {expected_schema_version!r} for rubric_version {rubric_version!r}")
    expected_criteria = CRITERIA_BY_RUBRIC.get(rubric_version, PROFILE_CRITERIA)
    if record.get("screening_status") != "non_final":
        errors.append("screening_status must be 'non_final'")
    recommendation = record.get("recommendation")
    if recommendation not in RECOMMENDATIONS:
        errors.append("recommendation is invalid")
    hard_gates_approved = record.get("jd_hard_gates_approved")
    if not isinstance(hard_gates_approved, bool):
        errors.append("jd_hard_gates_approved must be boolean")

    if basis == "provisional_baseline":
        if hard_gates_approved is not False:
            errors.append("provisional_baseline requires jd_hard_gates_approved=false")
        expected_jd_version = JD_VERSION_BY_RUBRIC.get(rubric_version, JD_VERSION)
        if record.get("jd_version") != expected_jd_version:
            errors.append(f"provisional record jd_version must be {expected_jd_version!r}")
        if recommendation != "second_review":
            errors.append("provisional_baseline must use second_review")
    elif basis == "approved_jd":
        if hard_gates_approved is not True:
            errors.append("approved_jd requires jd_hard_gates_approved=true")
        if jd_profile is None:
            errors.append("approved_jd requires --jd-profile")
        else:
            errors.extend(f"jd profile: {item}" for item in validate_profile(jd_profile))
            if isinstance(jd_profile, dict):
                if jd_profile.get("jd_hard_gates_approved") is not True:
                    errors.append("approved_jd requires an approved profile")
                if record.get("jd_version") != jd_profile.get("jd_version"):
                    errors.append("record jd_version does not match approved profile")
                profile_schema = jd_profile.get("schema_version")
                if profile_schema != "1.0" and rubric_version != jd_profile.get("rubric_version"):
                    errors.append("record rubric_version does not match approved profile")
                if profile_schema == "1.0" and rubric_version not in LEGACY_RUBRIC_VERSIONS:
                    errors.append("schema 1.0 profile requires a legacy rubric version (v1 or v2)")
                if profile_schema == "1.1" and rubric_version not in PREVIOUS_16_CRITERIA_RUBRIC_VERSIONS:
                    errors.append("schema 1.1 profile requires rubric v3 or v4")
                if profile_schema == "1.2" and rubric_version != V5_RUBRIC_VERSION:
                    errors.append("schema 1.2 profile requires rubric v5")
                if profile_schema == "1.3" and rubric_version != RUBRIC_VERSION:
                    errors.append("schema 1.3 profile requires the current rubric")

    evidence = record.get("evidence")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(evidence, list):
        errors.append("evidence must be a list")
        evidence = []
    for index, item in enumerate(evidence):
        prefix = f"evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        criterion_id = item.get("criterion_id")
        if criterion_id not in expected_criteria:
            errors.append(f"{prefix}.criterion_id is unknown")
            continue
        if criterion_id in evidence_by_id:
            errors.append(f"duplicate criterion_id: {criterion_id}")
        else:
            evidence_by_id[criterion_id] = item
        if item.get("criterion_name") != CRITERION_NAMES[criterion_id]:
            errors.append(f"{prefix}.criterion_name does not match {criterion_id}")
        state = item.get("state")
        strength = item.get("strength")
        confidence = item.get("confidence")
        if state not in STATES:
            errors.append(f"{prefix}.state is invalid")
        if strength not in STRENGTHS:
            errors.append(f"{prefix}.strength is invalid")
        if confidence not in CONFIDENCES:
            errors.append(f"{prefix}.confidence is invalid")
        if not nonempty(item.get("rationale")):
            errors.append(f"{prefix}.rationale must be non-empty")
        excerpt = item.get("excerpt")
        location = item.get("location")
        if strength == "E0":
            if not is_null(excerpt) or not is_null(location):
                errors.append(f"{prefix}: E0 requires null excerpt and location")
        elif not nonempty(excerpt) or not nonempty(location):
            errors.append(f"{prefix}: E1-E3 require excerpt and location")
        if state == "supported" and strength not in {"E2", "E3"}:
            if not (criterion_id == "OPS-EDU-01" and strength == "E1"):
                errors.append(f"{prefix}: supported requires E2/E3 except explicit education facts at E1")
        if state == "not_evidenced" and strength not in {"E0", "E1"}:
            errors.append(f"{prefix}: not_evidenced requires E0/E1")
        if state == "directly_not_met" and strength not in {"E2", "E3"}:
            if not (criterion_id == "OPS-EDU-01" and strength == "E1"):
                errors.append(f"{prefix}: directly_not_met requires explicit E2/E3 evidence except explicit education facts at E1")
        if (
            rubric_version in {V5_RUBRIC_VERSION, RUBRIC_VERSION}
            and criterion_id == "OPS-SCALE-01"
            and state == "directly_not_met"
        ):
            errors.append(f"{prefix}: OPS-SCALE-01 is preference-only and cannot be directly_not_met")
        if state == "conflicting" and strength not in {"E1", "E2", "E3"}:
            errors.append(f"{prefix}: conflicting requires cited E1-E3 source evidence")
        if state == "directly_not_met" and confidence == "low" and recommendation != "second_review":
            errors.append(f"{prefix}: low-confidence direct contradiction requires second_review")

    if set(evidence_by_id) != expected_criteria:
        errors.append(
            "evidence must contain each criterion exactly once; "
            f"missing={sorted(expected_criteria - set(evidence_by_id))}, "
            f"extra={sorted(set(evidence_by_id) - expected_criteria)}"
        )

    if rubric_version in {V5_RUBRIC_VERSION, RUBRIC_VERSION}:
        for left_id, right_id in DISTINCT_EVIDENCE_PAIRS:
            left = evidence_by_id.get(left_id)
            right = evidence_by_id.get(right_id)
            if not isinstance(left, dict) or not isinstance(right, dict):
                continue
            both_scoreable = all(
                item.get("state") in {"supported", "not_evidenced"}
                and item.get("strength") in {"E1", "E2", "E3"}
                for item in (left, right)
            )
            same_fact = (
                normalized_evidence_text(left.get("excerpt"))
                == normalized_evidence_text(right.get("excerpt"))
                and normalized_evidence_text(left.get("location"))
                == normalized_evidence_text(right.get("location"))
                and normalized_evidence_text(left.get("excerpt"))
                and normalized_evidence_text(left.get("location"))
            )
            if both_scoreable and same_fact:
                errors.append(
                    f"{left_id} and {right_id} reuse the same cited fact; "
                    "use distinct excerpts for separate scoring dimensions"
                )

    conflicts = record.get("hard_gate_conflicts")
    if not isinstance(conflicts, list):
        errors.append("hard_gate_conflicts must be a list")
        conflicts = []
    profile_must_haves: set[str] = set()
    profile_hard_gates: set[str] = set()
    if isinstance(jd_profile, dict):
        profile_criteria = jd_profile.get("criteria", [])
        if not isinstance(profile_criteria, list):
            profile_criteria = []
        profile_must_haves = {
            item.get("criterion_id")
            for item in profile_criteria
            if isinstance(item, dict) and item.get("category") == "must_have"
        }
        profile_hard_gates = {
            item.get("criterion_id")
            for item in profile_criteria
            if isinstance(item, dict)
            and item.get("category") in {"must_have", "exclude_if_evidenced"}
        }
    for index, conflict in enumerate(conflicts):
        prefix = f"hard_gate_conflicts[{index}]"
        if not isinstance(conflict, dict):
            errors.append(f"{prefix} must be an object")
            continue
        criterion_id = conflict.get("criterion_id")
        if basis != "approved_jd" or criterion_id not in profile_hard_gates:
            errors.append(f"{prefix} must reference an approved profile must_have or exclude_if_evidenced criterion")
        item = evidence_by_id.get(criterion_id, {})
        if item.get("state") != "directly_not_met":
            errors.append(f"{prefix} must match directly_not_met evidence")
        if conflict.get("excerpt") != item.get("excerpt") or conflict.get("location") != item.get("location"):
            errors.append(f"{prefix} excerpt and location must match the evidence item")
    if basis == "approved_jd":
        for criterion_id in profile_hard_gates:
            item = evidence_by_id.get(criterion_id, {})
            if item.get("state") == "directly_not_met" and criterion_id not in {
                conflict.get("criterion_id")
                for conflict in conflicts
                if isinstance(conflict, dict)
            }:
                errors.append(f"directly_not_met approved hard-gate evidence must be listed in hard_gate_conflicts: {criterion_id}")
    if recommendation == "do_not_advance_pending_human" and not conflicts:
        errors.append("do_not_advance_pending_human requires a direct approved hard-gate conflict")
    if conflicts and recommendation == "advance_pending_human":
        errors.append("hard-gate conflicts cannot be paired with advance_pending_human")
    if basis == "provisional_baseline" and conflicts:
        errors.append("provisional_baseline cannot emit hard_gate_conflicts")

    uncertainties = record.get("uncertainties")
    uncertainty_codes: list[str] = []
    if not isinstance(uncertainties, list):
        errors.append("uncertainties must be a list")
        uncertainties = []
    for index, item in enumerate(uncertainties):
        prefix = f"uncertainties[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        code = item.get("code")
        if code not in UNCERTAINTIES:
            errors.append(f"{prefix}.code is invalid")
        elif code in uncertainty_codes:
            errors.append(f"duplicate uncertainty code: {code}")
        else:
            uncertainty_codes.append(code)
        for field in ("description", "decision_impact", "required_human_action"):
            value = item.get(field)
            if not nonempty(value) or len(value.strip()) < 8:
                errors.append(f"{prefix}.{field} must contain a specific explanation of at least 8 characters")
        if item.get("requires_second_review") is not True:
            errors.append(f"{prefix}.requires_second_review must be true")
    if any(item.get("state") == "conflicting" for item in evidence_by_id.values()):
        if "U03_CONFLICTING_FACTS" not in uncertainty_codes:
            errors.append("conflicting evidence requires U03_CONFLICTING_FACTS")
    if basis == "provisional_baseline" and "U10_RUBRIC_AMBIGUITY" not in uncertainty_codes:
        errors.append("provisional_baseline requires U10_RUBRIC_AMBIGUITY")
    if uncertainties and recommendation != "second_review":
        errors.append("decision-relevant uncertainties require second_review")
    if recommendation == "second_review" and not uncertainty_codes:
        errors.append("second_review requires at least one uncertainty reason code")
    if basis == "approved_jd" and isinstance(jd_profile, dict):
        profile_criteria = jd_profile.get("criteria", [])
        if not isinstance(profile_criteria, list):
            profile_criteria = []
        for profile_item in profile_criteria:
            if not isinstance(profile_item, dict) or profile_item.get("category") != "must_have":
                continue
            evidence_item = evidence_by_id.get(profile_item.get("criterion_id"), {})
            if evidence_item.get("state") == "not_evidenced" and "U02_MUST_HAVE_MISSING" not in uncertainty_codes:
                errors.append("missing approved must_have evidence requires U02_MUST_HAVE_MISSING")
            if evidence_item.get("state") == "conflicting" and "U03_CONFLICTING_FACTS" not in uncertainty_codes:
                errors.append("conflicting approved must_have evidence requires U03_CONFLICTING_FACTS")
    if recommendation == "advance_pending_human":
        if basis != "approved_jd":
            errors.append("advance_pending_human requires approved_jd")
        if uncertainties or conflicts:
            errors.append("advance_pending_human cannot carry uncertainties or hard-gate conflicts")
        if any(
            item.get("confidence") == "low"
            and item.get("state") in {"supported", "conflicting", "directly_not_met"}
            for item in evidence_by_id.values()
        ):
            errors.append("low-confidence decision evidence requires second_review")
        if not any(
            evidence_by_id.get(criterion_id, {}).get("state") == "supported"
            and evidence_by_id.get(criterion_id, {}).get("strength") in {"E2", "E3"}
            for criterion_id in FOUNDATION_CRITERIA
        ):
            errors.append("advance_pending_human requires E2/E3 production operations evidence")
        if not any(
            evidence_by_id.get(criterion_id, {}).get("state") == "supported"
            and evidence_by_id.get(criterion_id, {}).get("strength") in {"E2", "E3"}
            for criterion_id in AI_RUNTIME_CRITERIA
        ):
            errors.append("advance_pending_human requires E2/E3 model API or RAG/Agent operations evidence")

    probes = record.get("interview_probes")
    if not isinstance(probes, list) or not 3 <= len(probes) <= 6:
        errors.append("interview_probes must contain 3-6 items")
    else:
        for index, item in enumerate(probes):
            prefix = f"interview_probes[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if item.get("criterion_id") not in expected_criteria:
                errors.append(f"{prefix}.criterion_id is unknown")
            if not nonempty(item.get("question")) or not nonempty(item.get("expected_signal")):
                errors.append(f"{prefix} requires question and expected_signal")

    summary = record.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        for key in (
            "conclusion_label",
            "one_line_conclusion",
            "human_review_requirement",
            "human_next_action",
        ):
            if not nonempty(summary.get(key)):
                errors.append(f"summary.{key} must be non-empty")
        if len(summary.get("one_line_conclusion", "")) > 160:
            errors.append("summary.one_line_conclusion exceeds 160 characters")
        if recommendation in SUMMARY_LABELS and summary.get("conclusion_label") != SUMMARY_LABELS[recommendation]:
            errors.append("summary.conclusion_label does not match recommendation")
        for key in ("top_strengths", "key_gaps"):
            items = summary.get(key)
            if not isinstance(items, list) or len(items) > 3:
                errors.append(f"summary.{key} must be a list of at most 3 items")
                continue
            for index, item in enumerate(items):
                if (
                    not isinstance(item, dict)
                    or item.get("criterion_id") not in expected_criteria
                    or not nonempty(item.get("finding"))
                ):
                    errors.append(f"summary.{key}[{index}] requires criterion_id and finding")
                    continue
                evidence_item = evidence_by_id.get(item["criterion_id"], {})
                if key == "top_strengths":
                    if item["criterion_id"] in {"OPS-EDU-01", "OPS-EXP-01", "OPS-OUTSOURCE-01", "OPS-DOMAIN-01"}:
                        errors.append(f"summary.{key}[{index}] cannot use an administrative fact as a strength")
                    if evidence_item.get("state") != "supported" or evidence_item.get("strength") not in {"E2", "E3"}:
                        errors.append(f"summary.{key}[{index}] must reference E2/E3 supported capability evidence")
                else:
                    gap_type = item.get("gap_type")
                    if (
                        rubric_version in {V5_RUBRIC_VERSION, RUBRIC_VERSION}
                        and item["criterion_id"] in {"OPS-SCALE-01", "OPS-DOMAIN-01"}
                        and evidence_item.get("state") == "not_evidenced"
                    ):
                        errors.append(f"summary.{key}[{index}] cannot treat missing bonus-only evidence as a key gap")
                    if gap_type not in GAP_TYPES:
                        errors.append(f"summary.{key}[{index}].gap_type is invalid")
                    expected_state = {
                        "evidence_gap": "not_evidenced",
                        "conflicting_facts": "conflicting",
                        "direct_contradiction": "directly_not_met",
                    }.get(gap_type)
                    if expected_state and evidence_item.get("state") != expected_state:
                        errors.append(f"summary.{key}[{index}].gap_type does not match evidence state")

    if record.get("sensitive_attributes_used") is not False:
        errors.append("sensitive_attributes_used must be false")
    if record.get("automation_actions") != []:
        errors.append("automation_actions must be an empty list")
    errors.extend(_validate_human_review(record.get("human_review"), recommendation, uncertainty_codes))
    return errors


def _validate_human_review(value: Any, recommendation: Any, uncertainty_codes: list[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["human_review must be an object"]
    if value.get("level_1_required") is not True:
        errors.append("human_review.level_1_required must be true")
    if value.get("level_1_status") != "pending":
        errors.append("initial record level_1_status must be pending")
    for key in ("level_1_reviewer", "level_1_decision", "level_1_reviewed_at"):
        if value.get(key) is not None:
            errors.append(f"initial record human_review.{key} must be null")
    needs_l2 = recommendation in {"second_review", "do_not_advance_pending_human"} or bool(uncertainty_codes)
    if value.get("level_2_required") is not needs_l2:
        errors.append(f"human_review.level_2_required must be {str(needs_l2).lower()}")
    if needs_l2:
        if value.get("level_2_status") != "pending":
            errors.append("required level 2 review must be pending")
        if value.get("level_2_mode") not in {"same_owner_separate_pass", "independent_reviewer"}:
            errors.append("required level 2 review needs a supported mode")
        if recommendation == "do_not_advance_pending_human" and value.get("level_2_mode") != "independent_reviewer":
            errors.append("negative recommendation requires independent_reviewer")
    else:
        if value.get("level_2_status") != "not_required" or value.get("level_2_mode") != "not_required":
            errors.append("unneeded level 2 review must be marked not_required")
    expected_reasons = set(uncertainty_codes)
    if recommendation == "do_not_advance_pending_human":
        expected_reasons.add("H02_NEGATIVE_RECOMMENDATION")
    reasons = value.get("level_2_reason_codes")
    if not isinstance(reasons, list) or any(reason not in L2_REASONS for reason in reasons):
        errors.append("human_review.level_2_reason_codes contains invalid codes")
        reasons = []
    if not expected_reasons.issubset(set(reasons)):
        errors.append("human_review.level_2_reason_codes must include every trigger")
    if not needs_l2 and reasons:
        errors.append("human_review.level_2_reason_codes must be empty when no second review is required")
    if needs_l2 and value.get("blind_review_required") is not True:
        errors.append("required second review must hide the first recommendation")
    if value.get("blind_review_confirmed") is not None:
        errors.append("initial record blind_review_confirmed must be null")
    for key in (
        "level_2_reviewer",
        "level_2_decision",
        "level_2_reviewed_at",
        "final_disposition",
        "resolution",
    ):
        if value.get(key) is not None:
            errors.append(f"initial record human_review.{key} must be null")
    return errors


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--jd-profile", type=Path)
    args = parser.parse_args(argv)
    try:
        record = read_json(args.record)
        profile = read_json(args.jd_profile) if args.jd_profile else None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    errors = validate_record(record, profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("valid operations/DevOps screening record; human review remains required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

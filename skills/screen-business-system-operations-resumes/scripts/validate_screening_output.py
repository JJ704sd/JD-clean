#!/usr/bin/env python3
"""Validate business-system operations screening records and review gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EXPECTED_ROLE = "business-system-operations-engineer"
EXPECTED_JD_VERSION = "business-system-operations-engineer-2026-09-18-draft-v1"
EXPECTED_RUBRIC_VERSION = "business-system-operations-rubric-2026-09-18-v1"
SCHEMA_VERSION = "1.0"
CRITERIA = (
    "BSO-EXP-01",
    "BSO-INTAKE-01",
    "BSO-TROUBLE-01",
    "BSO-TRACK-01",
    "BSO-ESCALATE-01",
    "BSO-MAINT-01",
    "BSO-CONTINUITY-01",
    "BSO-KB-01",
    "BSO-IMPROVE-01",
    "BSO-SERVICE-01",
    "BSO-SEC-01",
    "BSO-COLLAB-01",
    "BSO-ENTERPRISE-01",
    "BSO-DATA-01",
    "BSO-TICKET-01",
    "BSO-TRAINING-01",
)
CRITERION_NAMES = {
    "BSO-EXP-01": "相关应用支持/系统运维/实施/服务台经验",
    "BSO-INTAKE-01": "业务问题承接与诉求澄清",
    "BSO-TROUBLE-01": "初步排障与操作指导",
    "BSO-TRACK-01": "问题记录、状态同步与闭环跟进",
    "BSO-ESCALATE-01": "缺陷、需求、数据异常与复杂问题转交",
    "BSO-MAINT-01": "账号、权限、基础数据与配置维护",
    "BSO-CONTINUITY-01": "关键业务流程与运行保障",
    "BSO-KB-01": "问题分析、知识库与持续改进",
    "BSO-IMPROVE-01": "上线验证、反馈分析与体验改进",
    "BSO-SERVICE-01": "沟通、服务意识与优先级判断",
    "BSO-SEC-01": "权限、数据、配置与变更安全",
    "BSO-COLLAB-01": "跨团队协作与技术表达",
    "BSO-ENTERPRISE-01": "企业业务系统经验（加分）",
    "BSO-DATA-01": "SQL、日志检索或数据核查（加分）",
    "BSO-TICKET-01": "工单、服务台分级或知识库运营（加分）",
    "BSO-TRAINING-01": "用户培训、手册或系统上线支持（加分）",
}
BONUS_CRITERIA = {
    "BSO-ENTERPRISE-01",
    "BSO-DATA-01",
    "BSO-TICKET-01",
    "BSO-TRAINING-01",
}
STATES = {"supported", "not_evidenced", "conflicting", "directly_not_met"}
STRENGTHS = {"E0", "E1", "E2", "E3"}
CONFIDENCES = {"high", "medium", "low"}
RECOMMENDATIONS = {
    "advance_pending_human",
    "second_review",
    "do_not_advance_pending_human",
}
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
SUMMARY_LABELS = {
    "advance_pending_human": "建议推进（待人工一审）",
    "second_review": "建议二次复核",
    "do_not_advance_pending_human": "暂不推进（待人工一审与二次复核）",
}
GAP_TYPES = {"evidence_gap", "conflicting_facts", "direct_contradiction"}


def validate_profile(profile: Any) -> list[str]:
    """Keep record validation independent from same-named role modules."""

    if not isinstance(profile, dict):
        return ["profile must be a JSON object"]
    errors: list[str] = []
    if profile.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")
    if profile.get("role") != EXPECTED_ROLE:
        errors.append(f"profile role must be {EXPECTED_ROLE!r}")
    if profile.get("jd_version") != EXPECTED_JD_VERSION:
        errors.append(f"profile jd_version must be {EXPECTED_JD_VERSION!r}")
    if profile.get("rubric_version") != EXPECTED_RUBRIC_VERSION:
        errors.append(f"profile rubric_version must be {EXPECTED_RUBRIC_VERSION!r}")
    if profile.get("jd_hard_gates_approved") is not False:
        errors.append("draft profile must set jd_hard_gates_approved=false")
    criteria = profile.get("criteria")
    if not isinstance(criteria, list):
        return errors + ["profile criteria must be a list"]
    ids = [item.get("criterion_id") for item in criteria if isinstance(item, dict)]
    if len(ids) != len(CRITERIA) or set(ids) != set(CRITERIA):
        errors.append("profile criteria must contain each role criterion exactly once")
    return errors


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _review_errors(value: Any, uncertainty_codes: set[str]) -> list[str]:
    if not isinstance(value, dict):
        return ["human_review must be an object"]
    errors: list[str] = []
    if value.get("level_1_required") is not True:
        errors.append("human_review.level_1_required must be true")
    if value.get("level_1_status") != "pending":
        errors.append("initial level_1_status must be pending")
    for key in ("level_1_reviewer", "level_1_decision", "level_1_reviewed_at"):
        if value.get(key) is not None:
            errors.append(f"initial human_review.{key} must be null")
    if value.get("level_2_required") is not True:
        errors.append("provisional records require level 2 review")
    if value.get("level_2_status") != "pending":
        errors.append("required level 2 review must be pending")
    if value.get("level_2_mode") not in {"independent_reviewer", "same_owner_separate_pass"}:
        errors.append("level 2 review mode is invalid")
    reasons = value.get("level_2_reason_codes")
    if not isinstance(reasons, list) or not uncertainty_codes.issubset(set(reasons)):
        errors.append("level_2_reason_codes must include every uncertainty code")
    if value.get("blind_review_required") is not True:
        errors.append("required second review must set blind_review_required=true")
    if value.get("blind_review_confirmed") is not None:
        errors.append("initial blind_review_confirmed must be null")
    for key in (
        "level_2_reviewer",
        "level_2_decision",
        "level_2_reviewed_at",
        "final_disposition",
        "resolution",
    ):
        if value.get(key) is not None:
            errors.append(f"initial human_review.{key} must be null")
    return errors


def validate_record(record: Any, jd_profile: Any | None = None) -> list[str]:
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    errors: list[str] = []
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
        return [f"missing required fields: {', '.join(missing)}"]
    for key in ("screening_record_id", "candidate_id", "jd_version", "rubric_version"):
        if not nonempty(record.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if record.get("candidate_name") is not None and not nonempty(record.get("candidate_name")):
        errors.append("candidate_name must be non-empty when present")
    if record.get("role") != EXPECTED_ROLE:
        errors.append(f"role must be {EXPECTED_ROLE!r}")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if record.get("jd_version") != EXPECTED_JD_VERSION:
        errors.append(f"jd_version must be {EXPECTED_JD_VERSION!r}")
    if record.get("rubric_version") != EXPECTED_RUBRIC_VERSION:
        errors.append(f"rubric_version must be {EXPECTED_RUBRIC_VERSION!r}")
    if record.get("screening_status") != "non_final":
        errors.append("screening_status must be non_final")
    if record.get("screening_basis") != "provisional_baseline":
        errors.append("current profile requires provisional_baseline")
    if record.get("jd_hard_gates_approved") is not False:
        errors.append("current profile requires jd_hard_gates_approved=false")
    recommendation = record.get("recommendation")
    if recommendation not in RECOMMENDATIONS:
        errors.append("recommendation is invalid")
    if recommendation != "second_review":
        errors.append("current provisional profile requires second_review")

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
        if criterion_id not in CRITERIA:
            errors.append(f"{prefix}.criterion_id is unknown")
            continue
        if criterion_id in evidence_by_id:
            errors.append(f"duplicate evidence: {criterion_id}")
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
            if excerpt is not None or location is not None:
                errors.append(f"{prefix}: E0 must use null excerpt and location")
        elif not nonempty(excerpt) or not nonempty(location):
            errors.append(f"{prefix}: E1-E3 require excerpt and location")
        if state == "supported" and strength not in {"E2", "E3"}:
            errors.append(f"{prefix}: supported requires E2 or E3")
        if state == "not_evidenced" and strength not in {"E0", "E1"}:
            errors.append(f"{prefix}: not_evidenced requires E0 or E1")
        if state in {"conflicting", "directly_not_met"} and strength == "E0":
            errors.append(f"{prefix}: {state} requires cited evidence")
        if len(str(item.get("excerpt") or "")) > 500:
            errors.append(f"{prefix}.excerpt is too long")
    if set(evidence_by_id) != set(CRITERIA):
        errors.append(
            "evidence must contain each criterion exactly once; "
            f"missing={sorted(set(CRITERIA) - set(evidence_by_id))}, "
            f"extra={sorted(set(evidence_by_id) - set(CRITERIA))}"
        )

    conflicts = record.get("hard_gate_conflicts")
    if conflicts != []:
        errors.append("provisional profile must not emit hard_gate_conflicts")

    uncertainties = record.get("uncertainties")
    codes: list[str] = []
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
        elif code in codes:
            errors.append(f"duplicate uncertainty code: {code}")
        else:
            codes.append(code)
        for key in ("description", "decision_impact", "required_human_action"):
            if not nonempty(item.get(key)) or len(item[key].strip()) < 8:
                errors.append(f"{prefix}.{key} must be specific")
        if item.get("requires_second_review") is not True:
            errors.append(f"{prefix}.requires_second_review must be true")
    if "U10_RUBRIC_AMBIGUITY" not in codes:
        errors.append("provisional profile requires U10_RUBRIC_AMBIGUITY")
    if any(item.get("state") == "conflicting" for item in evidence_by_id.values()):
        if "U03_CONFLICTING_FACTS" not in codes:
            errors.append("conflicting evidence requires U03_CONFLICTING_FACTS")

    probes = record.get("interview_probes")
    if not isinstance(probes, list) or not 3 <= len(probes) <= 6:
        errors.append("interview_probes must contain 3 to 6 items")
    else:
        seen_probe_ids: set[str] = set()
        for index, item in enumerate(probes):
            prefix = f"interview_probes[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if item.get("criterion_id") not in CRITERIA:
                errors.append(f"{prefix}.criterion_id is invalid")
            if item.get("criterion_id") in seen_probe_ids:
                errors.append(f"{prefix} duplicates a criterion")
            seen_probe_ids.add(item.get("criterion_id"))
            if not nonempty(item.get("question")) or not nonempty(item.get("expected_signal")):
                errors.append(f"{prefix} requires question and expected_signal")

    summary = record.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        for key in ("conclusion_label", "one_line_conclusion", "human_review_requirement", "human_next_action"):
            if not nonempty(summary.get(key)):
                errors.append(f"summary.{key} must be non-empty")
        if summary.get("conclusion_label") != SUMMARY_LABELS.get(recommendation):
            errors.append("summary.conclusion_label does not match recommendation")
        for key in ("top_strengths", "key_gaps"):
            items = summary.get(key)
            if not isinstance(items, list) or len(items) > 3:
                errors.append(f"summary.{key} must be a list of at most 3 items")
                continue
            for index, item in enumerate(items):
                prefix = f"summary.{key}[{index}]"
                if not isinstance(item, dict) or item.get("criterion_id") not in CRITERIA or not nonempty(item.get("finding")):
                    errors.append(f"{prefix} requires criterion_id and finding")
                    continue
                evidence_item = evidence_by_id.get(item["criterion_id"], {})
                if key == "top_strengths":
                    if evidence_item.get("state") != "supported" or evidence_item.get("strength") not in {"E2", "E3"}:
                        errors.append(f"{prefix} must reference supported E2/E3 evidence")
                else:
                    if item["criterion_id"] in BONUS_CRITERIA and evidence_item.get("state") == "not_evidenced":
                        errors.append(f"{prefix} cannot treat missing bonus evidence as a key gap")
                    if item.get("gap_type") not in GAP_TYPES:
                        errors.append(f"{prefix}.gap_type is invalid")

    if record.get("sensitive_attributes_used") is not False:
        errors.append("sensitive_attributes_used must be false")
    if record.get("automation_actions") != []:
        errors.append("automation_actions must be an empty list")
    errors.extend(_review_errors(record.get("human_review"), set(codes)))
    if jd_profile is not None:
        errors.extend(f"jd profile: {error}" for error in validate_profile(jd_profile))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--jd-profile", type=Path)
    args = parser.parse_args(argv)
    try:
        record = json.loads(args.record.read_text(encoding="utf-8-sig"))
        profile = (
            json.loads(args.jd_profile.read_text(encoding="utf-8-sig"))
            if args.jd_profile
            else None
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    errors = validate_record(record, profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("screening record valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

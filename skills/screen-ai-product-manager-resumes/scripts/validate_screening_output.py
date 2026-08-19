#!/usr/bin/env python3
"""Validate AI product-manager screening evidence and mandatory human gates."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROLE = "ai-product-manager"
CURRENT_RUBRIC_VERSION = "ai-pm-rubric-2026-08-18-v3"
LEGACY_RUBRIC_VERSION = "ai-pm-rubric-2026-08-18-v2"
SUPPORTED_RUBRIC_VERSIONS = {LEGACY_RUBRIC_VERSION, CURRENT_RUBRIC_VERSION}
BASES = {"approved_jd", "provisional_baseline"}
RECOMMENDATIONS = {"advance_pending_human", "second_review", "do_not_advance_pending_human"}
STATES = {"supported", "not_evidenced", "conflicting"}
STRENGTHS = {"E0", "E1", "E2", "E3"}
CONFIDENCES = {"high", "medium", "low"}
L2_MODES = {"not_required", "same_owner_separate_pass", "independent_reviewer"}
CORE_CRITERIA = {
    "AIPM-PROD-01": "产品发现与定义",
    "AIPM-AI-01": "AI 方案理解与边界",
    "AIPM-EVAL-01": "评测与迭代",
    "AIPM-DATA-01": "数据/知识治理",
    "AIPM-DELIV-01": "端到端交付",
    "AIPM-OUT-01": "用户与业务结果",
    "AIPM-RISK-01": "风险、安全与合规",
    "AIPM-COLLAB-01": "协作与所有权",
}
UNCERTAINTIES = {
    "U01_PARSE_QUALITY", "U02_MUST_HAVE_MISSING", "U03_CONFLICTING_FACTS",
    "U04_CONTRIBUTION_UNCLEAR", "U05_TRANSFERABILITY", "U06_BOUNDARY_CASE",
    "U07_BIAS_OR_PROXY", "U08_DIMENSION_CONFLICT", "U09_ROLE_AMBIGUITY",
    "U10_RUBRIC_AMBIGUITY", "U11_UNTRUSTED_CONTENT",
}
HUMAN_REVIEW_REASONS = UNCERTAINTIES | {
    "H02_NEGATIVE_RECOMMENDATION",
    "H03_BATCH_AUDIT",
}
TOP_FIELDS = {
    "schema_version", "screening_record_id", "candidate_id", "candidate_name", "role",
    "screening_basis", "jd_hard_gates_approved", "jd_version", "rubric_version",
    "screening_status", "recommendation", "summary", "hard_gate_conflicts",
    "evidence", "uncertainties", "interview_probes", "sensitive_attributes_used",
    "human_review", "automation_actions",
}
OPTIONAL_TOP_FIELDS = {"candidate_name"}
EVIDENCE_FIELDS = {
    "criterion_id", "criterion_name", "state", "strength", "excerpt", "location",
    "rationale", "confidence",
}
UNCERTAINTY_FIELDS = {"code", "description", "requires_second_review"}
SUMMARY_FIELDS = {
    "conclusion_label", "one_line_conclusion", "top_strengths", "key_gaps",
    "human_review_requirement", "next_step",
}
SUMMARY_ITEM_FIELDS = {"criterion_id", "finding"}
HARD_GATE_FIELDS = {
    "criterion_id", "requirement_text", "excerpt", "location", "rationale",
    "jd_version", "approved_must_have",
}
HUMAN_REVIEW_FIELDS = {
    "level_1_required", "level_1_reviewer", "level_1_decision", "level_1_reviewed_at",
    "level_2_required", "level_2_mode", "level_2_reason_codes", "level_2_reviewer",
    "level_2_decision", "level_2_reviewed_at",
    "prior_recommendations_hidden_during_recheck", "reviewers_agree",
    "disagreement_reason", "resolution_owner", "resolution",
}
CONCLUSION_LABELS = {
    "advance_pending_human": "建议推进（待人工确认）",
    "second_review": "建议二次复核",
    "do_not_advance_pending_human": "建议暂不推进（待双重人工确认）",
}
L2_MODE_LABELS = {
    "independent_reviewer": "独立二审",
    "same_owner_separate_pass": "同人分时二审",
}
HUMAN_DECISIONS = {"advance", "second_review", "do_not_advance"}
PII_PATTERNS = (
    re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
)


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def unknown_fields(value: Any, allowed: set[str]) -> set[str]:
    return set(value) - allowed if isinstance(value, dict) else set()


def parse_timestamp(value: Any) -> datetime | None:
    if not nonempty(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def find_pii(value: Any, path: str = "record") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            errors.extend(find_pii(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(find_pii(item, f"{path}[{index}]"))
    elif isinstance(value, str) and any(pattern.search(value) for pattern in PII_PATTERNS):
        errors.append(f"possible PII found at {path}")
    return errors


def expected_human_review_requirement(review: Any) -> str | None:
    if not isinstance(review, dict) or review.get("level_1_required") is not True:
        return None
    if review.get("level_2_required") is not True:
        return "仅人工一审"
    mode_label = L2_MODE_LABELS.get(review.get("level_2_mode"))
    reasons = review.get("level_2_reason_codes")
    if mode_label is None or not isinstance(reasons, list) or not reasons:
        return None
    return f"人工一审 + {mode_label}（原因：{'、'.join(reasons)}）"


def validate(record: Any, *, allow_human_finalized: bool = False) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["record must be a JSON object"]

    for key in sorted((TOP_FIELDS - OPTIONAL_TOP_FIELDS) - record.keys()):
        errors.append(f"missing required field: {key}")
    for key in sorted(unknown_fields(record, TOP_FIELDS)):
        errors.append(f"unknown top-level field: {key}")

    if record.get("schema_version") != "1.2":
        errors.append("schema_version must be '1.2'")
    if record.get("role") != ROLE:
        errors.append(f"role must be {ROLE!r}")
    for key in ("screening_record_id", "candidate_id", "jd_version", "rubric_version"):
        if not nonempty(record.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if "candidate_name" in record:
        if not nonempty(record.get("candidate_name")) or len(record["candidate_name"].strip()) > 80:
            errors.append("candidate_name must be a non-empty string with at most 80 characters")
    if record.get("rubric_version") not in SUPPORTED_RUBRIC_VERSIONS:
        errors.append("rubric_version is not supported")
    if record.get("screening_status") not in {"non_final", "human_finalized"}:
        errors.append("screening_status must be non_final or human_finalized")
    if record.get("screening_status") == "human_finalized" and not allow_human_finalized:
        errors.append("human_finalized requires explicit human-finalized validation mode")
    recommendation = record.get("recommendation")
    if recommendation not in RECOMMENDATIONS:
        errors.append("recommendation is invalid")

    summary = record.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
    else:
        for key in sorted(SUMMARY_FIELDS - summary.keys()):
            errors.append(f"summary missing field: {key}")
        for key in sorted(unknown_fields(summary, SUMMARY_FIELDS)):
            errors.append(f"summary has unknown field: {key}")
        if recommendation in CONCLUSION_LABELS and summary.get("conclusion_label") != CONCLUSION_LABELS[recommendation]:
            errors.append("summary.conclusion_label does not match recommendation")
        conclusion = summary.get("one_line_conclusion")
        if not nonempty(conclusion) or len(conclusion) > 160:
            errors.append("summary.one_line_conclusion must be 1 to 160 characters")
        item_count = 0
        for list_name in ("top_strengths", "key_gaps"):
            items = summary.get(list_name)
            if not isinstance(items, list) or len(items) > 3:
                errors.append(f"summary.{list_name} must be a list with at most 3 items")
                continue
            item_count += len(items)
            for index, item in enumerate(items):
                prefix = f"summary.{list_name}[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                if unknown_fields(item, SUMMARY_ITEM_FIELDS):
                    errors.append(f"{prefix} has unknown fields")
                if item.get("criterion_id") not in CORE_CRITERIA:
                    errors.append(f"{prefix}.criterion_id is invalid")
                finding = item.get("finding")
                if not nonempty(finding) or len(finding) > 100:
                    errors.append(f"{prefix}.finding must be 1 to 100 characters")
        if item_count == 0:
            errors.append("summary must contain at least one strength or gap")
        for key in ("human_review_requirement", "next_step"):
            if not nonempty(summary.get(key)) or len(summary.get(key, "")) > 160:
                errors.append(f"summary.{key} must be 1 to 160 characters")
    basis = record.get("screening_basis")
    if basis not in BASES:
        errors.append("screening_basis is invalid")
    approved = record.get("jd_hard_gates_approved")
    if not isinstance(approved, bool):
        errors.append("jd_hard_gates_approved must be boolean")
    if basis == "approved_jd" and approved is not True:
        errors.append("approved_jd requires jd_hard_gates_approved=true")
    if basis == "provisional_baseline" and approved is not False:
        errors.append("provisional_baseline requires jd_hard_gates_approved=false")
    if record.get("sensitive_attributes_used") is not False:
        errors.append("sensitive_attributes_used must be false")
    if record.get("automation_actions") != []:
        errors.append("automation_actions must be an empty list")

    hard_gate_conflicts = record.get("hard_gate_conflicts")
    if not isinstance(hard_gate_conflicts, list):
        errors.append("hard_gate_conflicts must be a list")
        hard_gate_conflicts = []
    else:
        for index, item in enumerate(hard_gate_conflicts):
            prefix = f"hard_gate_conflicts[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if unknown_fields(item, HARD_GATE_FIELDS) or set(item) != HARD_GATE_FIELDS:
                errors.append(f"{prefix} must contain exactly the required fields")
            for key in ("criterion_id", "requirement_text", "excerpt", "location", "rationale", "jd_version"):
                if not nonempty(item.get(key)):
                    errors.append(f"{prefix}.{key} must be non-empty")
            if item.get("jd_version") != record.get("jd_version"):
                errors.append(f"{prefix}.jd_version must match the screening record")
            if item.get("approved_must_have") is not True:
                errors.append(f"{prefix}.approved_must_have must be true")
        if hard_gate_conflicts and recommendation != "do_not_advance_pending_human":
            errors.append("hard_gate_conflicts are only valid for negative recommendations")

    evidence = record.get("evidence")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must be a non-empty list")
    else:
        for index, item in enumerate(evidence):
            prefix = f"evidence[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if unknown_fields(item, EVIDENCE_FIELDS) or set(item) != EVIDENCE_FIELDS:
                errors.append(f"{prefix} must contain exactly the required fields")
            criterion_id = item.get("criterion_id")
            if not nonempty(criterion_id):
                errors.append(f"{prefix}.criterion_id must be non-empty")
            elif criterion_id not in CORE_CRITERIA:
                errors.append(f"{prefix}.criterion_id is not allowed for {ROLE}")
            elif criterion_id in evidence_by_id:
                errors.append(f"duplicate criterion_id: {criterion_id}")
            else:
                evidence_by_id[criterion_id] = item
            if not nonempty(item.get("criterion_name")):
                errors.append(f"{prefix}.criterion_name must be non-empty")
            if criterion_id in CORE_CRITERIA and item.get("criterion_name") != CORE_CRITERIA[criterion_id]:
                errors.append(f"{prefix}.criterion_name does not match {criterion_id}")
            state = item.get("state")
            strength = item.get("strength")
            if state not in STATES:
                errors.append(f"{prefix}.state is invalid")
            if strength not in STRENGTHS:
                errors.append(f"{prefix}.strength is invalid")
            if state == "supported" and strength not in {"E2", "E3"}:
                errors.append(f"{prefix}: supported requires E2 or E3")
            if state == "not_evidenced" and strength not in {"E0", "E1"}:
                errors.append(f"{prefix}: not_evidenced requires E0 or E1")
            if item.get("confidence") not in CONFIDENCES:
                errors.append(f"{prefix}.confidence is invalid")
            if not nonempty(item.get("rationale")):
                errors.append(f"{prefix}.rationale must be non-empty")
            if strength == "E0":
                if item.get("excerpt") is not None or item.get("location") is not None:
                    errors.append(f"{prefix}: E0 must use null excerpt and location")
            elif strength in {"E1", "E2", "E3"}:
                if not nonempty(item.get("excerpt")):
                    errors.append(f"{prefix}.excerpt is required for {strength}")
                if not nonempty(item.get("location")):
                    errors.append(f"{prefix}.location is required for {strength}")
        missing = sorted(set(CORE_CRITERIA) - set(evidence_by_id))
        if missing:
            errors.append(f"missing core criteria: {', '.join(missing)}")
        if len(evidence) != len(CORE_CRITERIA):
            errors.append(f"evidence must contain exactly {len(CORE_CRITERIA)} items")
        if isinstance(summary, dict):
            for index, item in enumerate(summary.get("top_strengths", [])):
                if not isinstance(item, dict):
                    continue
                criterion_id = item.get("criterion_id")
                if evidence_by_id.get(criterion_id, {}).get("state") != "supported":
                    errors.append(
                        f"summary.top_strengths[{index}]: top_strengths requires supported evidence"
                    )

    codes: list[str] = []
    uncertainties = record.get("uncertainties")
    if not isinstance(uncertainties, list):
        errors.append("uncertainties must be a list")
    else:
        for index, item in enumerate(uncertainties):
            prefix = f"uncertainties[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if unknown_fields(item, UNCERTAINTY_FIELDS) or set(item) != UNCERTAINTY_FIELDS:
                errors.append(f"{prefix} must contain exactly the required fields")
            code = item.get("code")
            if code not in UNCERTAINTIES:
                errors.append(f"{prefix}.code is invalid")
            elif code in codes:
                errors.append(f"duplicate uncertainty code: {code}")
            else:
                codes.append(code)
            if not nonempty(item.get("description")):
                errors.append(f"{prefix}.description must be non-empty")
            if item.get("requires_second_review") is not True:
                errors.append(f"{prefix}.requires_second_review must be true")

    probes = record.get("interview_probes")
    if not isinstance(probes, list) or not 3 <= len(probes) <= 6:
        errors.append("interview_probes must contain 3 to 6 questions")
    elif any(not nonempty(probe) for probe in probes):
        errors.append("every interview probe must be non-empty")
    elif len(set(probes)) != len(probes):
        errors.append("interview_probes must be unique")

    if any(item.get("state") == "conflicting" for item in evidence_by_id.values()) and "U03_CONFLICTING_FACTS" not in codes:
        errors.append("conflicting evidence requires U03_CONFLICTING_FACTS")
    if basis == "provisional_baseline":
        if recommendation != "second_review":
            errors.append("provisional baseline requires recommendation=second_review")
        if "U10_RUBRIC_AMBIGUITY" not in codes:
            errors.append("provisional baseline requires U10_RUBRIC_AMBIGUITY")
    if codes and recommendation != "second_review":
        errors.append("decision-relevant uncertainties require recommendation=second_review")
    if recommendation == "second_review" and not codes:
        errors.append("second_review requires at least one uncertainty code")
    if recommendation == "advance_pending_human":
        if approved is not True:
            errors.append("advance recommendation requires approved JD hard gates")
        for criterion_id in ("AIPM-PROD-01", "AIPM-AI-01", "AIPM-DELIV-01"):
            if evidence_by_id.get(criterion_id, {}).get("state") != "supported":
                errors.append(f"advance recommendation requires supported {criterion_id}")
        eval_supported = evidence_by_id.get("AIPM-EVAL-01", {}).get("state") == "supported"
        out_supported = evidence_by_id.get("AIPM-OUT-01", {}).get("state") == "supported"
        eval_strength = evidence_by_id.get("AIPM-EVAL-01", {}).get("strength")
        out_strength = evidence_by_id.get("AIPM-OUT-01", {}).get("strength")
        if not (eval_supported or out_supported) or eval_strength == "E0" or out_strength == "E0":
            errors.append("advance recommendation does not satisfy evaluation/outcome gate")
        if any(
            evidence_by_id.get(criterion_id, {}).get("confidence") == "low"
            for criterion_id in (
                "AIPM-PROD-01", "AIPM-AI-01", "AIPM-DELIV-01",
                "AIPM-EVAL-01", "AIPM-OUT-01",
            )
        ):
            errors.append("low-confidence decision evidence requires second review")
    if recommendation == "do_not_advance_pending_human" and approved is not True:
        errors.append("negative recommendation requires approved JD hard gates")
    if recommendation == "do_not_advance_pending_human":
        weak_core_count = sum(
            evidence_by_id.get(criterion_id, {}).get("state") != "supported"
            for criterion_id in ("AIPM-PROD-01", "AIPM-AI-01", "AIPM-DELIV-01")
        )
        if not hard_gate_conflicts and weak_core_count < 2:
            errors.append("negative recommendation requires a hard-gate conflict or two weak core dimensions")
        if not hard_gate_conflicts and any(
            evidence_by_id.get(criterion_id, {}).get("state") != "supported"
            and evidence_by_id.get(criterion_id, {}).get("confidence") == "low"
            for criterion_id in ("AIPM-PROD-01", "AIPM-AI-01", "AIPM-DELIV-01")
        ):
            errors.append("low-confidence negative gate requires second review")

    review = record.get("human_review")
    if not isinstance(review, dict):
        errors.append("human_review must be an object")
        return errors
    for key in sorted(HUMAN_REVIEW_FIELDS - review.keys()):
        errors.append(f"human_review missing field: {key}")
    for key in sorted(unknown_fields(review, HUMAN_REVIEW_FIELDS)):
        errors.append(f"human_review has unknown field: {key}")
    if review.get("level_1_required") is not True:
        errors.append("human_review.level_1_required must be true")
    l2_required = review.get("level_2_required") is True
    mode = review.get("level_2_mode")
    reasons = review.get("level_2_reason_codes")
    if mode not in L2_MODES:
        errors.append("human_review.level_2_mode is invalid")
    if not isinstance(reasons, list):
        errors.append("human_review.level_2_reason_codes must be a list")
        reasons = []
    else:
        if len(reasons) != len(set(reasons)):
            errors.append("human_review.level_2_reason_codes must be unique")
        if any(reason not in HUMAN_REVIEW_REASONS for reason in reasons):
            errors.append("human_review.level_2_reason_codes contains an invalid code")

    must_have_l2 = bool(codes) or recommendation in {"second_review", "do_not_advance_pending_human"}
    if must_have_l2 and not l2_required:
        errors.append("this recommendation or uncertainty requires level_2_required=true")
    if recommendation == "do_not_advance_pending_human" and "H02_NEGATIVE_RECOMMENDATION" not in reasons:
        errors.append("negative recommendation requires H02_NEGATIVE_RECOMMENDATION")
    if "H02_NEGATIVE_RECOMMENDATION" in reasons and recommendation != "do_not_advance_pending_human":
        errors.append("H02_NEGATIVE_RECOMMENDATION is only valid for negative recommendations")
    if "H03_BATCH_AUDIT" in reasons and recommendation != "advance_pending_human":
        errors.append("H03_BATCH_AUDIT is only valid for advance batch audits")
    if recommendation == "advance_pending_human" and l2_required and "H03_BATCH_AUDIT" not in reasons:
        errors.append("advance second review requires H03_BATCH_AUDIT")
    if l2_required:
        if mode == "not_required":
            errors.append("level_2_mode cannot be not_required when required")
        if not reasons:
            errors.append("level_2_reason_codes cannot be empty when required")
        if not set(codes).issubset(set(reasons)):
            errors.append("all uncertainty codes must appear in level_2_reason_codes")
        if review.get("prior_recommendations_hidden_during_recheck") is not True:
            errors.append("second review must hide prior recommendations")
    else:
        if mode != "not_required":
            errors.append("level_2_mode must be not_required when level 2 is not required")
        if reasons:
            errors.append("level_2_reason_codes must be empty when level 2 is not required")

    expected_review_text = expected_human_review_requirement(review)
    if isinstance(summary, dict) and expected_review_text is not None:
        if summary.get("human_review_requirement") != expected_review_text:
            errors.append("summary.human_review_requirement does not match human review gate")

    for level in ("level_1", "level_2"):
        decision = review.get(f"{level}_decision")
        if decision is not None and decision not in HUMAN_DECISIONS:
            errors.append(f"human_review.{level}_decision is invalid")

    if record.get("screening_status") == "non_final":
        completed_fields = (
            "level_1_reviewer", "level_1_decision", "level_1_reviewed_at",
            "level_2_reviewer", "level_2_decision", "level_2_reviewed_at",
            "reviewers_agree", "disagreement_reason", "resolution_owner", "resolution",
        )
        if any(review.get(field) is not None for field in completed_fields):
            errors.append("non_final record cannot contain completed human review fields")
    elif record.get("screening_status") == "human_finalized":
        if not nonempty(review.get("level_1_reviewer")) or not nonempty(review.get("level_1_decision")):
            errors.append("human_finalized requires completed level 1 review")
        level_1_time = parse_timestamp(review.get("level_1_reviewed_at"))
        if level_1_time is None:
            errors.append("human_finalized requires timezone-aware level_1_reviewed_at")
        if l2_required:
            if not nonempty(review.get("level_2_reviewer")) or not nonempty(review.get("level_2_decision")):
                errors.append("human_finalized requires completed level 2 review")
            level_2_time = parse_timestamp(review.get("level_2_reviewed_at"))
            if level_2_time is None:
                errors.append("human_finalized requires timezone-aware level_2_reviewed_at")
            if not isinstance(review.get("reviewers_agree"), bool):
                errors.append("completed level 2 review requires reviewers_agree boolean")
            elif (
                review.get("reviewers_agree")
                != (review.get("level_1_decision") == review.get("level_2_decision"))
            ):
                errors.append("reviewers_agree does not match reviewer decisions")
            if mode == "independent_reviewer" and review.get("level_1_reviewer") == review.get("level_2_reviewer"):
                errors.append("independent reviewers must differ")
            if mode == "same_owner_separate_pass":
                if review.get("level_1_reviewer") != review.get("level_2_reviewer"):
                    errors.append("same-owner review requires the same reviewer")
            if level_1_time is not None and level_2_time is not None and level_2_time <= level_1_time:
                errors.append("level 2 review must occur after level 1")
            if review.get("reviewers_agree") is False:
                for key in ("disagreement_reason", "resolution_owner", "resolution"):
                    if not nonempty(review.get(key)):
                        errors.append(f"review disagreement requires {key}")
        for key in ("resolution_owner", "resolution"):
            if not nonempty(review.get(key)):
                errors.append(f"human_finalized requires {key}")
    errors.extend(find_pii(record))
    return errors


def main(argv: list[str]) -> int:
    allow_human_finalized = len(argv) == 3 and argv[1] == "--allow-human-finalized"
    if len(argv) not in {2, 3} or (len(argv) == 3 and not allow_human_finalized):
        print(
            "usage: validate_screening_output.py [--allow-human-finalized] <record.json>",
            file=sys.stderr,
        )
        return 2
    try:
        record = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    errors = validate(record, allow_human_finalized=allow_human_finalized)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("valid AI product manager screening record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

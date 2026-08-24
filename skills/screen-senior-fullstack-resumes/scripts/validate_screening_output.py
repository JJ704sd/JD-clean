#!/usr/bin/env python3
"""Validate a senior full-stack resume-screening record and human-review gate."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.2"
LEGACY_SCHEMA_VERSION = "1.1"
EXPECTED_ROLE = "senior-fullstack-engineer"
EXPECTED_JD_VERSION = "senior-fullstack-2026-08-14-v1"
EXPECTED_RUBRIC_VERSION = "senior-fullstack-2026-08-24-v4"
PREVIOUS_RUBRIC_VERSION = "senior-fullstack-2026-08-18-v3"
LEGACY_RUBRIC_VERSION = "senior-fullstack-2026-08-18-v2"
COMPATIBILITY_PAIRS = {
    (LEGACY_SCHEMA_VERSION, LEGACY_RUBRIC_VERSION),
    (SCHEMA_VERSION, PREVIOUS_RUBRIC_VERSION),
    (SCHEMA_VERSION, EXPECTED_RUBRIC_VERSION),
}
CRITERIA = (
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
ADVANCE_MINIMUMS = {
    "SEN-EXP-01": "E2",
    "SEN-BE-01": "E2",
    "SEN-ARCH-01": "E2",
    "SEN-FE-01": "E2",
    "SEN-DATA-01": "E2",
    "SEN-LEVEL-01": "E3",
    "SEN-ADM-01": "E1",
}
NEGATIVE_CORE = {"SEN-BE-01", "SEN-ARCH-01", "SEN-FE-01", "SEN-DATA-01"}
DIRECT_CRITICAL = {"SEN-BE-01", "SEN-FE-01"}
INDEPENDENT_REVIEW_CODES = {
    "U05_TRANSFERABILITY",
    "U07_BIAS_OR_PROXY",
    "U09_ROLE_AMBIGUITY",
    "U10_RUBRIC_AMBIGUITY",
    "U11_UNTRUSTED_CONTENT",
}

RECOMMENDATIONS = {
    "advance_pending_human",
    "second_review",
    "do_not_advance_pending_human",
}
EVIDENCE_STATES = {"supported", "not_evidenced", "conflicting", "directly_not_met"}
STRENGTHS = {"E0", "E1", "E2", "E3"}
STRENGTH_RANK = {"E0": 0, "E1": 1, "E2": 2, "E3": 3}
CONFIDENCES = {"high", "medium", "low"}
L1_STATUSES = {"pending", "completed"}
L1_DECISIONS = {"advance", "do_not_advance", "second_review"}
L2_STATUSES = {"not_required", "pending", "completed"}
L2_MODES = {
    "not_required",
    "source_fact_confirmation",
    "same_owner_separate_pass",
    "independent_reviewer",
}
L2_DECISIONS = {"advance", "do_not_advance", "interview_verify", "rubric_escalation"}
FINAL_DISPOSITIONS = {"advance", "do_not_advance", "interview_verify", "rubric_escalation"}
UNCERTAINTY_CODES = {
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
SOURCE_FACT_CODES = {
    "U01_PARSE_QUALITY",
    "U02_MUST_HAVE_MISSING",
    "U03_CONFLICTING_FACTS",
}
PII_PATTERNS = (
    re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b"),
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _find_pii(value: Any, path: str = "record") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            errors.extend(_find_pii(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_find_pii(item, f"{path}[{index}]"))
    elif isinstance(value, str) and any(pattern.search(value) for pattern in PII_PATTERNS):
        errors.append(f"possible PII found at {path}")
    return errors


def _parse_timestamp(value: Any) -> datetime | None:
    if not _nonempty(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _string_list(
    value: Any,
    label: str,
    errors: list[str],
    *,
    max_items: int | None = None,
    max_chars: int | None = None,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be a list")
        return []
    if max_items is not None and len(value) > max_items:
        errors.append(f"{label} must contain at most {max_items} items")
    for index, item in enumerate(value):
        if not _nonempty(item):
            errors.append(f"{label}[{index}] must be a non-empty string")
        elif max_chars is not None and len(item) > max_chars:
            errors.append(f"{label}[{index}] must contain at most {max_chars} characters")
    return [item for item in value if _nonempty(item)]


def _validate_evidence(record: dict[str, Any], errors: list[str]) -> dict[str, dict[str, Any]]:
    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence must be a list")
        return {}

    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(evidence):
        prefix = f"evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        criterion = item.get("criterion_id")
        if criterion not in CRITERIA:
            errors.append(f"{prefix}.criterion_id is not allowed for {EXPECTED_ROLE}")
        elif criterion in result:
            errors.append(f"duplicate evidence criterion_id: {criterion}")
        else:
            result[criterion] = item

        state = item.get("state")
        strength = item.get("strength")
        if state not in EVIDENCE_STATES:
            errors.append(f"{prefix}.state is invalid")
        if strength not in STRENGTHS:
            errors.append(f"{prefix}.strength is invalid")
        if item.get("confidence") not in CONFIDENCES:
            errors.append(f"{prefix}.confidence is invalid")
        if not _nonempty(item.get("rationale")):
            errors.append(f"{prefix}.rationale must be non-empty")

        if strength == "E0":
            if state != "not_evidenced":
                errors.append(f"{prefix}: E0 must use state=not_evidenced")
            if item.get("excerpt") is not None or item.get("location") is not None:
                errors.append(f"{prefix}: E0 must use null excerpt and location")
        elif strength in {"E1", "E2", "E3"}:
            if not _nonempty(item.get("excerpt")):
                errors.append(f"{prefix}.excerpt is required for {strength}")
            if not _nonempty(item.get("location")):
                errors.append(f"{prefix}.location is required for {strength}")
        if state == "not_evidenced" and strength in {"E2", "E3"}:
            errors.append(f"{prefix}: not_evidenced cannot use {strength}")

    missing = sorted(set(CRITERIA) - set(result))
    if missing:
        errors.append(f"evidence is missing criteria: {', '.join(missing)}")
    if len(evidence) != len(CRITERIA):
        errors.append(f"evidence must contain exactly {len(CRITERIA)} items")
    return result


def _validate_uncertainties(record: dict[str, Any], errors: list[str]) -> list[str]:
    uncertainties = record.get("uncertainties")
    if not isinstance(uncertainties, list):
        errors.append("uncertainties must be a list")
        return []
    codes: list[str] = []
    for index, item in enumerate(uncertainties):
        prefix = f"uncertainties[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        code = item.get("code")
        if code not in UNCERTAINTY_CODES:
            errors.append(f"{prefix}.code is invalid")
        elif code in codes:
            errors.append(f"duplicate uncertainty code: {code}")
        else:
            codes.append(code)
        for field in ("description", "decision_impact", "required_human_action"):
            if not _nonempty(item.get(field)):
                errors.append(f"{prefix}.{field} must be non-empty")
    return codes


def _validate_probes(record: dict[str, Any], errors: list[str]) -> set[str]:
    probes = record.get("interview_probes")
    if not isinstance(probes, list):
        errors.append("interview_probes must be a list")
        return set()
    if not 1 <= len(probes) <= 5:
        errors.append("interview_probes must contain 1 to 5 prioritized probes")
    priorities: list[int] = []
    criteria: set[str] = set()
    for index, item in enumerate(probes):
        prefix = f"interview_probes[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        priority = item.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
            errors.append(f"{prefix}.priority must be a positive integer")
        else:
            priorities.append(priority)
        criterion = item.get("criterion_id")
        if criterion not in CRITERIA:
            errors.append(f"{prefix}.criterion_id is invalid")
        else:
            criteria.add(criterion)
        for field in ("question", "expected_signal"):
            if not _nonempty(item.get(field)):
                errors.append(f"{prefix}.{field} must be non-empty")
    if priorities and sorted(priorities) != list(range(1, len(probes) + 1)):
        errors.append("interview probe priorities must be unique and contiguous from 1")
    return criteria


def _validate_human_review(
    record: dict[str, Any], uncertainty_codes: list[str], errors: list[str]
) -> None:
    review = record.get("human_review")
    if not isinstance(review, dict):
        errors.append("human_review must be an object")
        return

    required_fields = {
        "level_1_required",
        "level_1_status",
        "level_1_reviewer",
        "level_1_decision",
        "level_2_required",
        "level_2_status",
        "level_2_mode",
        "level_2_reason_codes",
        "independent_review_preferred",
        "independent_review_fallback_reason",
        "blind_review_required",
        "blind_review_confirmed",
        "level_2_reviewer",
        "level_2_decision",
        "final_disposition",
        "resolution",
    }
    current_schema = record.get("schema_version") == SCHEMA_VERSION
    if current_schema:
        required_fields |= {"level_1_reviewed_at", "level_2_reviewed_at"}
    for field in sorted(required_fields - review.keys()):
        errors.append(f"human_review is missing field: {field}")

    if review.get("level_1_required") is not True:
        errors.append("human_review.level_1_required must be true")
    l1_status = review.get("level_1_status")
    if l1_status not in L1_STATUSES:
        errors.append("human_review.level_1_status is invalid")
    if l1_status == "pending":
        if review.get("level_1_reviewer") is not None or review.get("level_1_decision") is not None:
            errors.append("pending level 1 review cannot contain reviewer or decision")
        if current_schema and review.get("level_1_reviewed_at") is not None:
            errors.append("pending level 1 review cannot contain level_1_reviewed_at")
    elif l1_status == "completed":
        if not _nonempty(review.get("level_1_reviewer")):
            errors.append("completed level 1 review requires a reviewer")
        if review.get("level_1_decision") not in L1_DECISIONS:
            errors.append("completed level 1 review requires a valid decision")
        if current_schema and _parse_timestamp(review.get("level_1_reviewed_at")) is None:
            errors.append("completed level 1 review requires timezone-aware level_1_reviewed_at")

    l2_required = review.get("level_2_required")
    if not isinstance(l2_required, bool):
        errors.append("human_review.level_2_required must be boolean")
        l2_required = False
    l2_status = review.get("level_2_status")
    if l2_status not in L2_STATUSES:
        errors.append("human_review.level_2_status is invalid")
    mode = review.get("level_2_mode")
    if mode not in L2_MODES:
        errors.append("human_review.level_2_mode is invalid")
    preferred = review.get("independent_review_preferred")
    if not isinstance(preferred, bool):
        errors.append("human_review.independent_review_preferred must be boolean")
        preferred = False
    blind_required = review.get("blind_review_required")
    if not isinstance(blind_required, bool):
        errors.append("human_review.blind_review_required must be boolean")

    reason_codes = review.get("level_2_reason_codes")
    if not isinstance(reason_codes, list):
        errors.append("human_review.level_2_reason_codes must be a list")
        reason_codes = []
    else:
        for index, code in enumerate(reason_codes):
            if code not in UNCERTAINTY_CODES:
                errors.append(f"human_review.level_2_reason_codes[{index}] is invalid")
        if len(reason_codes) != len(set(reason_codes)):
            errors.append("human_review.level_2_reason_codes must not contain duplicates")

    model_recommendation = record.get("model_recommendation")
    if uncertainty_codes:
        if model_recommendation != "second_review":
            errors.append("records with uncertainties must use model_recommendation=second_review")
        if l2_required is not True:
            errors.append("decision-relevant uncertainty requires level_2_required=true")
    elif model_recommendation == "second_review":
        errors.append("second_review requires at least one documented uncertainty")
    if set(reason_codes) != set(uncertainty_codes):
        errors.append("level_2_reason_codes must exactly match uncertainty codes")

    if not l2_required:
        if l2_status != "not_required" or mode != "not_required":
            errors.append("level 2 status and mode must be not_required when level 2 is not required")
        if reason_codes:
            errors.append("level 2 reason codes must be empty when level 2 is not required")
        if preferred is not False:
            errors.append("independent review cannot be preferred when level 2 is not required")
        if review.get("independent_review_fallback_reason") is not None:
            errors.append("independent review fallback reason must be null when level 2 is not required")
        if blind_required is not False or review.get("blind_review_confirmed") is not None:
            errors.append("blind review fields must be false/null when level 2 is not required")
        if review.get("level_2_reviewer") is not None or review.get("level_2_decision") is not None:
            errors.append("level 2 reviewer and decision must be null when level 2 is not required")
        if current_schema and review.get("level_2_reviewed_at") is not None:
            errors.append("level 2 review time must be null when level 2 is not required")
        if l1_status == "completed" and review.get("level_1_decision") == "second_review":
            errors.append("level 1 cannot decide second_review when level 2 is not required")
    else:
        if l2_status not in {"pending", "completed"}:
            errors.append("required level 2 review must be pending or completed")
        if mode not in {
            "source_fact_confirmation",
            "same_owner_separate_pass",
            "independent_reviewer",
        }:
            errors.append("required level 2 review needs a review mode")
        if mode == "source_fact_confirmation":
            if not set(uncertainty_codes).issubset(SOURCE_FACT_CODES):
                errors.append("source_fact_confirmation is only allowed for U01/U02/U03")
            if blind_required is not False or review.get("blind_review_confirmed") is not None:
                errors.append("source/fact confirmation must not claim blind review")
        elif blind_required is not True:
            errors.append("interpretive level 2 review must be blind")
        if set(uncertainty_codes) & INDEPENDENT_REVIEW_CODES and preferred is not True:
            errors.append("these uncertainty codes require independent_review_preferred=true")
        fallback = review.get("independent_review_fallback_reason")
        if preferred and mode != "independent_reviewer" and not _nonempty(fallback):
            errors.append("preferred independent review needs a fallback reason when using another mode")
        if (not preferred or mode == "independent_reviewer") and fallback is not None:
            errors.append("independent review fallback reason must be null unless a preferred review falls back")
        if l1_status == "completed" and review.get("level_1_decision") != "second_review":
            errors.append("level 1 decision must be second_review when level 2 is required")
        if l2_status == "pending":
            if review.get("blind_review_confirmed") is not None:
                errors.append("pending level 2 review cannot confirm blind review")
            if review.get("level_2_reviewer") is not None or review.get("level_2_decision") is not None:
                errors.append("pending level 2 review cannot contain reviewer or decision")
            if current_schema and review.get("level_2_reviewed_at") is not None:
                errors.append("pending level 2 review cannot contain level_2_reviewed_at")
        elif l2_status == "completed":
            if l1_status != "completed":
                errors.append("level 2 cannot complete before level 1")
            if mode != "source_fact_confirmation" and review.get("blind_review_confirmed") is not True:
                errors.append("completed level 2 review must confirm blind review")
            if not _nonempty(review.get("level_2_reviewer")):
                errors.append("completed level 2 review requires a reviewer")
            if review.get("level_2_decision") not in L2_DECISIONS:
                errors.append("completed level 2 review requires a valid decision")
            if current_schema:
                level_1_time = _parse_timestamp(review.get("level_1_reviewed_at"))
                level_2_time = _parse_timestamp(review.get("level_2_reviewed_at"))
                if level_2_time is None:
                    errors.append("completed level 2 review requires timezone-aware level_2_reviewed_at")
                elif level_1_time is not None and level_2_time <= level_1_time:
                    errors.append("level 2 review must occur after level 1")
            if mode == "independent_reviewer" and review.get("level_1_reviewer") == review.get("level_2_reviewer"):
                errors.append("independent reviewer must differ from level 1 reviewer")
            if mode == "same_owner_separate_pass" and review.get("level_1_reviewer") != review.get("level_2_reviewer"):
                errors.append("same-owner second pass must use the level 1 reviewer")

    screening_status = record.get("screening_status")
    if screening_status == "non_final":
        if review.get("final_disposition") is not None or review.get("resolution") is not None:
            errors.append("non_final record cannot contain final disposition or resolution")
    elif screening_status == "human_finalized":
        if l1_status != "completed":
            errors.append("human_finalized requires completed level 1 review")
        if l2_required and l2_status != "completed":
            errors.append("human_finalized requires completed level 2 review when required")
        if review.get("final_disposition") not in FINAL_DISPOSITIONS:
            errors.append("human_finalized requires a valid final disposition")
        if not _nonempty(review.get("resolution")):
            errors.append("human_finalized requires a resolution note")


def _validate_recommendation(
    record: dict[str, Any], evidence: dict[str, dict[str, Any]], probe_criteria: set[str], errors: list[str]
) -> None:
    recommendation = record.get("model_recommendation")
    if recommendation == "advance_pending_human":
        for criterion, minimum in ADVANCE_MINIMUMS.items():
            item = evidence.get(criterion, {})
            if item.get("state") != "supported" or STRENGTH_RANK.get(item.get("strength"), -1) < STRENGTH_RANK[minimum]:
                errors.append(f"advance_pending_human requires {criterion} supported at {minimum} or above")
        if any(
            evidence.get(criterion, {}).get("confidence") == "low"
            for criterion in ADVANCE_MINIMUMS
        ):
            errors.append("low-confidence decision evidence requires second review")
        ai = evidence.get("SEN-AI-01", {})
        if ai.get("state") != "supported" and "SEN-AI-01" not in probe_criteria:
            errors.append("missing AI evidence on an advance record requires an SEN-AI-01 interview probe")
    elif recommendation == "second_review":
        summary = record.get("recruiter_summary")
        if isinstance(summary, dict) and not summary.get("critical_gaps"):
            errors.append("second_review requires at least one critical gap or pending item")
    elif recommendation == "do_not_advance_pending_human":
        direct_not_met = any(
            evidence.get(cid, {}).get("state") == "directly_not_met"
            for cid in DIRECT_CRITICAL
        )
        core_gaps = sum(
            evidence.get(cid, {}).get("state") == "not_evidenced"
            and evidence.get(cid, {}).get("strength") in {"E0", "E1"}
            for cid in NEGATIVE_CORE
        )
        admin_or_experience_not_met = any(
            evidence.get(cid, {}).get("state") == "directly_not_met"
            for cid in ("SEN-EXP-01", "SEN-ADM-01")
        )
        strong_adjacent_system_evidence = (
            evidence.get("SEN-LEVEL-01", {}).get("state") == "supported"
            and evidence.get("SEN-LEVEL-01", {}).get("strength") == "E3"
            and evidence.get("SEN-ARCH-01", {}).get("state") == "supported"
            and STRENGTH_RANK.get(evidence.get("SEN-ARCH-01", {}).get("strength"), -1) >= 2
            and evidence.get("SEN-DATA-01", {}).get("state") == "supported"
            and STRENGTH_RANK.get(evidence.get("SEN-DATA-01", {}).get("strength"), -1) >= 2
        )
        negative_gate = (
            direct_not_met
            or admin_or_experience_not_met
            or (core_gaps >= 2 and not strong_adjacent_system_evidence)
        )
        if not negative_gate:
            errors.append("do_not_advance_pending_human lacks the senior negative evidence gate")
        if any(
            evidence.get(criterion, {}).get("confidence") == "low"
            and evidence.get(criterion, {}).get("state") in {"not_evidenced", "directly_not_met"}
            for criterion in NEGATIVE_CORE | DIRECT_CRITICAL | {"SEN-EXP-01", "SEN-ADM-01"}
        ):
            errors.append("low-confidence negative gate requires second review")
        if (
            strong_adjacent_system_evidence
            and core_gaps >= 2
            and not direct_not_met
            and not admin_or_experience_not_met
        ):
            errors.append("strong adjacent system evidence requires transferability review before a negative recommendation")
        summary = record.get("recruiter_summary")
        if isinstance(summary, dict) and not summary.get("critical_gaps"):
            errors.append("negative recommendation requires at least one critical gap")


def validate_record(record: Any, *, allow_human_finalized: bool = False) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["record must be a JSON object"]

    required = {
        "schema_version",
        "screening_record_id",
        "candidate_id",
        "role",
        "jd_version",
        "rubric_version",
        "screening_status",
        "model_recommendation",
        "recommendation_rationale",
        "recruiter_summary",
        "evidence",
        "uncertainties",
        "interview_probes",
        "sensitive_attributes_used",
        "human_review",
        "automation_actions",
    }
    for key in sorted(required - record.keys()):
        errors.append(f"missing required field: {key}")

    expected_values = {
        "role": EXPECTED_ROLE,
        "jd_version": EXPECTED_JD_VERSION,
    }
    for field, expected in expected_values.items():
        if record.get(field) != expected:
            errors.append(f"{field} must be {expected!r}")
    if (record.get("schema_version"), record.get("rubric_version")) not in COMPATIBILITY_PAIRS:
        errors.append("schema/rubric compatibility pair (schema_version/rubric_version) is not supported")
    for field in ("screening_record_id", "candidate_id", "recommendation_rationale"):
        if not _nonempty(record.get(field)):
            errors.append(f"{field} must be a non-empty string")
    if "candidate_name" in record:
        if not _nonempty(record.get("candidate_name")) or len(record["candidate_name"].strip()) > 80:
            errors.append("candidate_name must be a non-empty string with at most 80 characters")
    if _nonempty(record.get("recommendation_rationale")) and len(record["recommendation_rationale"]) > 200:
        errors.append("recommendation_rationale must contain at most 200 characters")
    if record.get("screening_status") not in {"non_final", "human_finalized"}:
        errors.append("screening_status must be non_final or human_finalized")
    if record.get("screening_status") == "human_finalized" and not allow_human_finalized:
        errors.append("human_finalized requires explicit human-finalized validation mode")
    if record.get("model_recommendation") not in RECOMMENDATIONS:
        errors.append("model_recommendation is not an allowed policy state")
    if record.get("sensitive_attributes_used") is not False:
        errors.append("sensitive_attributes_used must be false")
    if record.get("automation_actions") != []:
        errors.append("automation_actions must be an empty list")

    summary = record.get("recruiter_summary")
    if not isinstance(summary, dict):
        errors.append("recruiter_summary must be an object")
    else:
        _string_list(
            summary.get("strongest_matches"),
            "recruiter_summary.strongest_matches",
            errors,
            max_items=3,
            max_chars=120,
        )
        _string_list(
            summary.get("critical_gaps"),
            "recruiter_summary.critical_gaps",
            errors,
            max_items=3,
            max_chars=120,
        )
        if not _nonempty(summary.get("human_next_action")):
            errors.append("recruiter_summary.human_next_action must be non-empty")
        elif len(summary["human_next_action"]) > 200:
            errors.append("recruiter_summary.human_next_action must contain at most 200 characters")

    evidence = _validate_evidence(record, errors)
    if record.get("schema_version") != SCHEMA_VERSION and any(
        item.get("state") == "directly_not_met" for item in evidence.values()
    ):
        errors.append("directly_not_met requires schema_version='1.2'")
    uncertainty_codes = _validate_uncertainties(record, errors)
    if (
        any(item.get("state") == "conflicting" for item in evidence.values())
        and "U03_CONFLICTING_FACTS" not in uncertainty_codes
    ):
        errors.append("conflicting evidence requires U03_CONFLICTING_FACTS")
    probe_criteria = _validate_probes(record, errors)
    _validate_human_review(record, uncertainty_codes, errors)
    _validate_recommendation(record, evidence, probe_criteria, errors)
    errors.extend(_find_pii(record))
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
    errors = validate_record(record, allow_human_finalized=allow_human_finalized)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"valid senior full-stack screening record (schema {record['schema_version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

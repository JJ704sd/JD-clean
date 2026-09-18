#!/usr/bin/env python3
"""Validate a draft or approved operations/DevOps resume-screening profile."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROLE = "operations-devops-engineer"
DRAFT_JD_VERSION = "operations-devops-engineer-2026-09-17-draft-v1"
LEGACY_CRITERIA = {
    "OPS-EDU-01",
    "OPS-EXP-01",
    "OPS-LINUX-01",
    "OPS-ENV-01",
    "OPS-DB-01",
    "OPS-DELIVERY-01",
    "OPS-OBS-01",
    "OPS-SEC-DR-01",
    "OPS-MODEL-01",
    "OPS-RAG-01",
    "OPS-AI-GOV-01",
    "OPS-AUTO-COST-01",
    "OPS-COLLAB-01",
}
V5_CRITERIA = LEGACY_CRITERIA | {
    "OPS-OWN-01",
    "OPS-AI-BONUS-01",
    "OPS-OUTSOURCE-01",
    "OPS-SCALE-01",
}
CRITERIA = V5_CRITERIA | {
    "OPS-OFFICE-IT-01",
    "OPS-OFFICE-NET-01",
    "OPS-DOMAIN-01",
}
V3_V4_CRITERIA = V5_CRITERIA - {"OPS-SCALE-01"}
CRITERIA_BY_SCHEMA = {
    "1.0": LEGACY_CRITERIA,
    "1.1": V3_V4_CRITERIA,
    "1.2": V5_CRITERIA,
    "1.3": CRITERIA,
}
V5_RUBRIC_VERSION = "operations-devops-rubric-2026-09-14-v5"
CURRENT_RUBRIC_VERSION = "operations-devops-rubric-2026-09-17-v1"
PREVIOUS_PROFILE_RUBRIC_VERSIONS = {
    "operations-devops-rubric-2026-09-14-v3",
    "operations-devops-rubric-2026-09-14-v4",
    V5_RUBRIC_VERSION,
}
PREFERENCE_ONLY_CRITERIA = {
    "OPS-EXP-01",
    "OPS-OWN-01",
    "OPS-AI-BONUS-01",
    "OPS-SCALE-01",
    "OPS-DOMAIN-01",
}
CATEGORIES = {
    "requires_calibration",
    "must_have",
    "exclude_if_evidenced",
    "preferred",
    "interview_only",
    "administrative",
    "prohibited",
}
MISSING_ACTIONS = {"second_review", "interview_verify", "no_effect"}
CONFLICT_ACTIONS = {"second_review", "human_confirm"}
PROXY_RISKS = {"none", "review_required", "prohibited"}
APPROVER_ROLES = {"recruiter", "hiring_manager"}


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def has_placeholder(value: Any) -> bool:
    return isinstance(value, str) and "__REPLACE" in value


def string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(nonempty(item) for item in value)


def validate_profile(profile: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["profile must be a JSON object"]
    schema_version = profile.get("schema_version")
    if schema_version not in CRITERIA_BY_SCHEMA:
        errors.append("schema_version must be '1.0', '1.1', '1.2', or '1.3'")
        expected_criteria = CRITERIA
    else:
        expected_criteria = CRITERIA_BY_SCHEMA[schema_version]
    if schema_version == "1.1" and profile.get("rubric_version") not in PREVIOUS_PROFILE_RUBRIC_VERSIONS:
        errors.append(
            "schema 1.1 rubric_version must be one of "
            f"{sorted(PREVIOUS_PROFILE_RUBRIC_VERSIONS)}"
        )
    if schema_version == "1.2" and profile.get("rubric_version") != V5_RUBRIC_VERSION:
        errors.append(f"schema 1.2 rubric_version must be {V5_RUBRIC_VERSION!r}")
    if schema_version == "1.3" and profile.get("rubric_version") != CURRENT_RUBRIC_VERSION:
        errors.append(f"schema 1.3 rubric_version must be {CURRENT_RUBRIC_VERSION!r}")
    if schema_version == "1.0" and "rubric_version" in profile:
        errors.append("schema 1.0 must not declare rubric_version")
    if profile.get("role") != ROLE:
        errors.append(f"role must be {ROLE!r}")
    for key in ("jd_version", "role_variant"):
        value = profile.get(key)
        if not nonempty(value):
            errors.append(f"{key} must be a non-empty string")
        elif profile.get("jd_hard_gates_approved") is True and has_placeholder(value):
            errors.append(f"{key} contains an unresolved placeholder")
        elif (
            key == "jd_version"
            and profile.get("jd_hard_gates_approved") is True
            and (value == DRAFT_JD_VERSION or "draft" in value.lower() or "provisional" in value.lower())
        ):
            errors.append("approved profile must use a new non-draft jd_version")

    approved_profile = profile.get("jd_hard_gates_approved")
    if not isinstance(approved_profile, bool):
        errors.append("jd_hard_gates_approved must be boolean")

    approvers = profile.get("approved_by")
    roles: set[str] = set()
    reviewer_ids: set[str] = set()
    if not isinstance(approvers, list):
        errors.append("approved_by must be a list")
    else:
        for index, approver in enumerate(approvers):
            prefix = f"approved_by[{index}]"
            if not isinstance(approver, dict):
                errors.append(f"{prefix} must be an object")
                continue
            reviewer_id = approver.get("reviewer_id")
            role = approver.get("role")
            if not nonempty(reviewer_id):
                errors.append(f"{prefix}.reviewer_id must be non-empty")
            elif reviewer_id in reviewer_ids:
                errors.append(f"duplicate reviewer_id: {reviewer_id}")
            else:
                reviewer_ids.add(reviewer_id)
            if not nonempty(role):
                errors.append(f"{prefix}.role must be non-empty")
            else:
                roles.add(role)
        missing_roles = sorted(APPROVER_ROLES - roles)
        if approved_profile is True and missing_roles:
            errors.append(f"approved_by missing required roles: {', '.join(missing_roles)}")
        if approved_profile is True and any(
            not nonempty(item.get("reviewer_id")) or has_placeholder(item.get("reviewer_id"))
            for item in approvers
            if isinstance(item, dict)
        ):
            errors.append("approved profile has an unresolved approver placeholder")
        if approved_profile is True and len(reviewer_ids) < 2:
            errors.append("approved profile requires two distinct approvers")

    criteria = profile.get("criteria")
    if not isinstance(criteria, list):
        errors.append("criteria must be a list")
        return errors
    actual_ids: set[str] = set()
    for index, criterion in enumerate(criteria):
        prefix = f"criteria[{index}]"
        if not isinstance(criterion, dict):
            errors.append(f"{prefix} must be an object")
            continue
        criterion_id = criterion.get("criterion_id")
        if not nonempty(criterion_id):
            errors.append(f"{prefix}.criterion_id must be non-empty")
        elif criterion_id in actual_ids:
            errors.append(f"duplicate criterion_id: {criterion_id}")
        else:
            actual_ids.add(criterion_id)
        for key in ("requirement_text", "job_relevance", "owner"):
            if not nonempty(criterion.get(key)):
                errors.append(f"{prefix}.{key} must be non-empty")
        category = criterion.get("category")
        if category not in CATEGORIES:
            errors.append(f"{prefix}.category is invalid")
        if schema_version == "1.0" and category == "exclude_if_evidenced":
            errors.append(f"{prefix}.category is unavailable in schema 1.0")
        if (
            schema_version in {"1.2", "1.3"}
            and criterion_id in PREFERENCE_ONLY_CRITERIA
            and category in {"must_have", "exclude_if_evidenced"}
        ):
            errors.append(f"{prefix}: {criterion_id} is preference-only and cannot be a hard gate")
        observable = criterion.get("resume_observable")
        if not isinstance(observable, bool):
            errors.append(f"{prefix}.resume_observable must be boolean")
        if not string_list(criterion.get("accepted_evidence")):
            errors.append(f"{prefix}.accepted_evidence must be a non-empty string list")
        if not string_list(criterion.get("insufficient_evidence")):
            errors.append(f"{prefix}.insufficient_evidence must be a non-empty string list")
        if criterion.get("missing_information_action") not in MISSING_ACTIONS:
            errors.append(f"{prefix}.missing_information_action is invalid")
        if criterion.get("conflict_action") not in CONFLICT_ACTIONS:
            errors.append(f"{prefix}.conflict_action is invalid")
        proxy_risk = criterion.get("proxy_risk")
        if proxy_risk not in PROXY_RISKS:
            errors.append(f"{prefix}.proxy_risk is invalid")
        approved = criterion.get("approved")
        if not isinstance(approved, bool):
            errors.append(f"{prefix}.approved must be boolean")

        if category == "must_have":
            if observable is not True:
                errors.append(f"{prefix}: must_have must be resume_observable")
            if approved is not True:
                errors.append(f"{prefix}: must_have must be approved")
            if criterion.get("missing_information_action") != "second_review":
                errors.append(f"{prefix}: missing must_have evidence requires second_review")
            if proxy_risk != "none":
                errors.append(f"{prefix}: must_have cannot carry unresolved proxy risk")
        if approved_profile is False and category == "must_have":
            errors.append(f"{prefix}: draft profile cannot declare an active must_have; use requires_calibration")
        if category == "exclude_if_evidenced":
            if observable is not True:
                errors.append(f"{prefix}: exclude_if_evidenced must be resume_observable")
            if approved is not True:
                errors.append(f"{prefix}: exclude_if_evidenced must be approved")
            if criterion.get("missing_information_action") != "no_effect":
                errors.append(f"{prefix}: missing exclude evidence must have no_effect")
            if proxy_risk != "none":
                errors.append(f"{prefix}: exclude_if_evidenced cannot carry proxy risk")
        if approved_profile is False and category == "exclude_if_evidenced":
            errors.append(f"{prefix}: draft profile cannot declare an active exclude_if_evidenced; use requires_calibration")
        if approved_profile is False and approved is True:
            errors.append(f"{prefix}: draft profile criteria must remain unapproved")
        if category == "interview_only" and observable is True:
            errors.append(f"{prefix}: interview_only should not be marked resume_observable")
        if category == "prohibited" and (approved is True or observable is True):
            errors.append(f"{prefix}: prohibited criterion cannot be approved or resume_observable")
        if approved_profile is True:
            if category == "requires_calibration":
                errors.append(f"{prefix}: approved profile still has requires_calibration")
            if category != "prohibited" and approved is not True:
                errors.append(f"{prefix}: approved profile contains an unapproved criterion")
            if category == "prohibited" and approved is not False:
                errors.append(f"{prefix}: prohibited criterion must remain unapproved")

    if actual_ids != expected_criteria:
        errors.append(
            f"criteria IDs must exactly match schema {schema_version!r}; "
            f"missing={sorted(expected_criteria - actual_ids)}, extra={sorted(actual_ids - expected_criteria)}"
        )
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_jd_profile.py <jd-profile.json>", file=sys.stderr)
        return 2
    try:
        profile = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    errors = validate_profile(profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if profile["jd_hard_gates_approved"]:
        print("valid approved operations/DevOps JD profile")
    else:
        print("valid draft profile only; not approved for hard-gate screening")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

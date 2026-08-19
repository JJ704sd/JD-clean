#!/usr/bin/env python3
"""Validate an approved AI product-manager JD screening profile."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

CATEGORIES = {"must_have", "preferred", "interview_only", "administrative", "prohibited"}
MISSING_ACTIONS = {"second_review", "interview_verify", "no_effect"}
CONFLICT_ACTIONS = {"second_review", "human_confirm"}
PROXY_RISKS = {"none", "review_required", "prohibited"}
APPROVER_ROLES = {"recruiter", "hiring_manager"}


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def unresolved_placeholder(value: Any) -> bool:
    return isinstance(value, str) and "__REPLACE" in value


def string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(nonempty(item) for item in value)


def validate(profile: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["profile must be a JSON object"]
    if profile.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")
    if profile.get("role") != "ai-product-manager":
        errors.append("role must be 'ai-product-manager'")
    for key in ("jd_version", "role_variant"):
        if not nonempty(profile.get(key)):
            errors.append(f"{key} must be a non-empty string")
        elif unresolved_placeholder(profile.get(key)):
            errors.append(f"{key} contains an unresolved template placeholder")

    approvers = profile.get("approved_by")
    approver_roles: set[str] = set()
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
            elif unresolved_placeholder(reviewer_id):
                errors.append(f"{prefix}.reviewer_id contains an unresolved template placeholder")
            elif reviewer_id in reviewer_ids:
                errors.append(f"duplicate reviewer_id: {reviewer_id}")
            else:
                reviewer_ids.add(reviewer_id)
            if not nonempty(role):
                errors.append(f"{prefix}.role must be non-empty")
            else:
                approver_roles.add(role)
        missing_roles = sorted(APPROVER_ROLES - approver_roles)
        if missing_roles:
            errors.append(f"approved_by missing required roles: {', '.join(missing_roles)}")

    criteria = profile.get("criteria")
    criterion_ids: set[str] = set()
    if not isinstance(criteria, list) or not criteria:
        errors.append("criteria must be a non-empty list")
        return errors
    for index, criterion in enumerate(criteria):
        prefix = f"criteria[{index}]"
        if not isinstance(criterion, dict):
            errors.append(f"{prefix} must be an object")
            continue
        criterion_id = criterion.get("criterion_id")
        if not nonempty(criterion_id):
            errors.append(f"{prefix}.criterion_id must be non-empty")
        elif criterion_id in criterion_ids:
            errors.append(f"duplicate criterion_id: {criterion_id}")
        else:
            criterion_ids.add(criterion_id)
        for key in ("requirement_text", "job_relevance", "owner"):
            if not nonempty(criterion.get(key)):
                errors.append(f"{prefix}.{key} must be non-empty")
        category = criterion.get("category")
        if category not in CATEGORIES:
            errors.append(f"{prefix}.category is invalid")
        observable = criterion.get("resume_observable")
        if not isinstance(observable, bool):
            errors.append(f"{prefix}.resume_observable must be boolean")
        if not string_list(criterion.get("accepted_evidence")):
            errors.append(f"{prefix}.accepted_evidence must be a non-empty string list")
        if not string_list(criterion.get("insufficient_evidence")):
            errors.append(f"{prefix}.insufficient_evidence must be a non-empty string list")
        missing_action = criterion.get("missing_information_action")
        if missing_action not in MISSING_ACTIONS:
            errors.append(f"{prefix}.missing_information_action is invalid")
        if criterion.get("conflict_action") not in CONFLICT_ACTIONS:
            errors.append(f"{prefix}.conflict_action is invalid")
        proxy_risk = criterion.get("proxy_risk")
        if proxy_risk not in PROXY_RISKS:
            errors.append(f"{prefix}.proxy_risk is invalid")
        if not isinstance(criterion.get("approved"), bool):
            errors.append(f"{prefix}.approved must be boolean")

        if category == "must_have":
            if observable is not True:
                errors.append(f"{prefix}: must_have must be resume_observable")
            if criterion.get("approved") is not True:
                errors.append(f"{prefix}: must_have must be approved")
            if missing_action != "second_review":
                errors.append(f"{prefix}: missing must_have evidence requires second_review")
            if proxy_risk != "none":
                errors.append(f"{prefix}: must_have cannot carry unresolved proxy risk")
        if category == "interview_only" and observable is True:
            errors.append(f"{prefix}: interview_only should not be marked resume_observable")
        if category == "prohibited" and criterion.get("approved") is True:
            errors.append(f"{prefix}: prohibited criterion cannot be approved for screening")
        if category == "prohibited" and observable is True:
            errors.append(f"{prefix}: prohibited criterion cannot be resume_observable")
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
    errors = validate(profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("valid AI product manager JD profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

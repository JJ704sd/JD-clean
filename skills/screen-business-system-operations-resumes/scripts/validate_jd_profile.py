#!/usr/bin/env python3
"""Validate the business-system operations role profile."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROLE = "business-system-operations-engineer"
JD_VERSION = "business-system-operations-engineer-2026-09-18-draft-v1"
RUBRIC_VERSION = "business-system-operations-rubric-2026-09-18-v1"
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
REQUIRED_CRITERION_FIELDS = {
    "criterion_id",
    "requirement_text",
    "category",
    "owner",
    "approved",
}


def validate_profile(profile: Any) -> list[str]:
    if not isinstance(profile, dict):
        return ["profile must be a JSON object"]
    errors: list[str] = []
    if profile.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")
    if profile.get("role") != ROLE:
        errors.append(f"role must be {ROLE!r}")
    if profile.get("jd_version") != JD_VERSION:
        errors.append(f"jd_version must be {JD_VERSION!r}")
    if profile.get("rubric_version") != RUBRIC_VERSION:
        errors.append(f"rubric_version must be {RUBRIC_VERSION!r}")
    if profile.get("jd_hard_gates_approved") is not False:
        errors.append("draft profile must set jd_hard_gates_approved=false")
    if profile.get("approved_by") != []:
        errors.append("draft profile approved_by must be an empty list")

    criteria = profile.get("criteria")
    if not isinstance(criteria, list):
        return errors + ["criteria must be a list"]
    seen: set[str] = set()
    for index, item in enumerate(criteria):
        prefix = f"criteria[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = REQUIRED_CRITERION_FIELDS - item.keys()
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(sorted(missing))}")
        criterion_id = item.get("criterion_id")
        if criterion_id not in CRITERIA:
            errors.append(f"{prefix}.criterion_id is unknown")
        elif criterion_id in seen:
            errors.append(f"duplicate criterion_id: {criterion_id}")
        else:
            seen.add(criterion_id)
        if item.get("category") not in {"requires_calibration", "bonus"}:
            errors.append(f"{prefix}.category is invalid")
        if not isinstance(item.get("requirement_text"), str) or not item["requirement_text"].strip():
            errors.append(f"{prefix}.requirement_text must be non-empty")
        if item.get("owner") != "hiring_manager":
            errors.append(f"{prefix}.owner must be hiring_manager")
        if item.get("approved") is not False:
            errors.append(f"{prefix}.approved must be false in a draft profile")
    if seen != set(CRITERIA):
        errors.append(
            "criteria must contain each criterion exactly once; "
            f"missing={sorted(set(CRITERIA) - seen)}, extra={sorted(seen - set(CRITERIA))}"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    args = parser.parse_args(argv)
    try:
        profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    errors = validate_profile(profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"profile valid: {ROLE} {JD_VERSION} {RUBRIC_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

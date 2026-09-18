#!/usr/bin/env python3
"""Validate a record and print its derived internal evidence-coverage scorecard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scoring import score_record
from validate_screening_output import read_json, validate_record


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
    try:
        scorecard = score_record(record)
    except (TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(scorecard, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

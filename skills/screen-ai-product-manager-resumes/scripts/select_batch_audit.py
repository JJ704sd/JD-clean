#!/usr/bin/env python3
"""Select a deterministic, reproducible audit sample from pseudonymous candidate IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys


def select(candidate_ids: list[str], batch_id: str, seed: str, rate: float) -> dict[str, object]:
    if not batch_id.strip():
        raise ValueError("batch_id must be non-empty")
    if not seed.strip():
        raise ValueError("seed must be non-empty")
    if not 0.2 <= rate <= 1.0:
        raise ValueError("rate must be between 0.2 and 1.0")
    normalized = [candidate_id.strip() for candidate_id in candidate_ids]
    if not normalized or any(not candidate_id for candidate_id in normalized):
        raise ValueError("candidate_ids must contain non-empty values")
    if len(normalized) != len(set(normalized)):
        raise ValueError("candidate_ids must be unique")

    def audit_key(candidate_id: str) -> str:
        payload = f"{seed}\x1f{batch_id}\x1f{candidate_id}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    ordered = sorted(normalized, key=lambda candidate_id: (audit_key(candidate_id), candidate_id))
    count = max(1, math.ceil(len(ordered) * rate))
    return {
        "schema_version": "1.0",
        "batch_id": batch_id,
        "candidate_count": len(ordered),
        "audit_rate": rate,
        "audit_count": count,
        "seed": seed,
        "selection_method": "sha256(seed US batch_id US candidate_id), ascending",
        "selected_candidate_ids": sorted(ordered[:count]),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--rate", type=float, default=0.2)
    parser.add_argument("candidate_ids", nargs="+")
    args = parser.parse_args(argv[1:])
    try:
        result = select(args.candidate_ids, args.batch_id, args.seed, args.rate)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

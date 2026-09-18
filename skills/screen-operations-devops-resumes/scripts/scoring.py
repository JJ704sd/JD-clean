#!/usr/bin/env python3
"""Deterministic, rubric-versioned evidence-coverage scoring."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

ROLE = "operations-devops-engineer"
RUBRIC_VERSION = "operations-devops-rubric-2026-09-17-v1"
SCORING_VERSION = "operations-devops-score-2026-09-17-v1"

WEIGHTS: dict[str, int] = {
    "OPS-EDU-01": 2,
    "OPS-EXP-01": 10,
    "OPS-LINUX-01": 8,
    "OPS-ENV-01": 7,
    "OPS-DB-01": 8,
    "OPS-DELIVERY-01": 8,
    "OPS-OBS-01": 7,
    "OPS-SEC-DR-01": 5,
    "OPS-MODEL-01": 5,
    "OPS-RAG-01": 4,
    "OPS-AI-GOV-01": 4,
    "OPS-AUTO-COST-01": 3,
    "OPS-COLLAB-01": 3,
    "OPS-OWN-01": 9,
    "OPS-SCALE-01": 3,
    "OPS-AI-BONUS-01": 3,
    "OPS-OFFICE-IT-01": 5,
    "OPS-OFFICE-NET-01": 5,
    "OPS-DOMAIN-01": 1,
    "OPS-OUTSOURCE-01": 0,
}

STRENGTH_FACTORS = {
    "E0": Decimal("0"),
    "E1": Decimal("0.40"),
    "E2": Decimal("0.75"),
    "E3": Decimal("1.00"),
}
ZERO_SCORE_STATES = {"conflicting", "directly_not_met"}
VALID_STATES = {"supported", "not_evidenced", *ZERO_SCORE_STATES}


def _grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "E"


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral() else float(value)


def score_record(record: dict[str, Any]) -> dict[str, Any]:
    """Compute an internal evidence-coverage score; never trust supplied totals."""

    if record.get("role") != ROLE:
        raise ValueError(f"unsupported role: {record.get('role')!r}")
    if record.get("rubric_version") != RUBRIC_VERSION:
        raise ValueError(
            f"scoring requires rubric_version {RUBRIC_VERSION!r}; "
            "historical rubric records are not scored retroactively"
        )
    recommendation = record.get("recommendation")
    if recommendation not in {
        "advance_pending_human",
        "second_review",
        "do_not_advance_pending_human",
    }:
        raise ValueError("record has no supported recommendation state")

    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        raise TypeError("evidence must be a list")
    by_criterion: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, dict):
            raise TypeError("each evidence item must be an object")
        criterion_id = item.get("criterion_id")
        if not isinstance(criterion_id, str) or criterion_id in by_criterion:
            raise ValueError("evidence criterion IDs must be unique strings")
        by_criterion[criterion_id] = item
    if set(by_criterion) != set(WEIGHTS):
        raise ValueError("evidence criteria do not match the current scoring weights")

    raw_components: dict[str, Decimal] = {}
    for criterion_id, weight in WEIGHTS.items():
        item = by_criterion[criterion_id]
        strength = item.get("strength")
        state = item.get("state")
        if state not in VALID_STATES:
            raise ValueError(f"invalid evidence state for {criterion_id}: {state!r}")
        if strength not in STRENGTH_FACTORS:
            raise ValueError(f"invalid evidence strength for {criterion_id}: {strength!r}")
        factor = (
            Decimal("0")
            if state in ZERO_SCORE_STATES
            else STRENGTH_FACTORS[strength]
        )
        raw_components[criterion_id] = Decimal(weight) * factor

    total = sum(raw_components.values(), Decimal("0"))
    score = int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "screening_record_id": record.get("screening_record_id"),
        "candidate_id": record.get("candidate_id"),
        "rubric_version": RUBRIC_VERSION,
        "score": score,
        "grade": _grade(score),
        "review_status": recommendation,
        "components": {
            criterion_id: _number(value)
            for criterion_id, value in raw_components.items()
        },
        "weights": dict(WEIGHTS),
        "scoring_version": SCORING_VERSION,
    }

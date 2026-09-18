#!/usr/bin/env python3
"""Deterministic evidence coverage scoring for the business-system role."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

STRENGTH_FACTORS = {
    "E0": Decimal("0"),
    "E1": Decimal("0.40"),
    "E2": Decimal("0.75"),
    "E3": Decimal("1.00"),
}
ROLE = "business-system-operations-engineer"
RUBRIC_VERSION = "business-system-operations-rubric-2026-09-18-v1"
SCORING_VERSION = "business-system-operations-score-2026-09-18-v1"
ROLE_WEIGHTS = {
    "BSO-EXP-01": 8,
    "BSO-INTAKE-01": 7,
    "BSO-TROUBLE-01": 15,
    "BSO-TRACK-01": 6,
    "BSO-ESCALATE-01": 9,
    "BSO-MAINT-01": 6,
    "BSO-CONTINUITY-01": 9,
    "BSO-KB-01": 5,
    "BSO-IMPROVE-01": 7,
    "BSO-SERVICE-01": 4,
    "BSO-SEC-01": 5,
    "BSO-COLLAB-01": 4,
    "BSO-ENTERPRISE-01": 4,
    "BSO-DATA-01": 4,
    "BSO-TICKET-01": 3,
    "BSO-TRAINING-01": 4,
}


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


@dataclass(frozen=True)
class ScoreResult:
    score: int
    grade: str
    review_status: str
    components: dict[str, int | float]
    scoring_version: str = SCORING_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "grade": self.grade,
            "review_status": self.review_status,
            "components": self.components,
            "scoring_version": self.scoring_version,
        }


def score_record(record: dict[str, Any]) -> ScoreResult:
    if record.get("role") != ROLE:
        raise ValueError(f"role must be {ROLE!r}")
    if record.get("rubric_version") != RUBRIC_VERSION:
        raise ValueError(f"rubric_version must be {RUBRIC_VERSION!r}")
    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        raise TypeError("evidence must be a list")
    by_id = {item.get("criterion_id"): item for item in evidence if isinstance(item, dict)}
    if set(by_id) != set(ROLE_WEIGHTS):
        raise ValueError("evidence criteria do not match the business-system rubric")
    raw: dict[str, Decimal] = {}
    for criterion_id, weight in ROLE_WEIGHTS.items():
        item = by_id[criterion_id]
        try:
            factor = STRENGTH_FACTORS[item.get("strength")]
        except KeyError as exc:
            raise ValueError(f"invalid strength for {criterion_id}") from exc
        if item.get("state") in {"conflicting", "directly_not_met"}:
            factor = Decimal("0")
        raw[criterion_id] = Decimal(weight) * factor
    total = sum(raw.values(), Decimal("0"))
    score = int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    components = {
        key: int(value) if value == value.to_integral() else float(value)
        for key, value in raw.items()
    }
    recommendation = record.get("model_recommendation", record.get("recommendation"))
    if recommendation not in {"advance_pending_human", "second_review", "do_not_advance_pending_human"}:
        raise ValueError("record has no supported recommendation state")
    return ScoreResult(score, _grade(score), recommendation, components)

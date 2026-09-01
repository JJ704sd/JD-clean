"""Deterministic evidence scoring applied after model output validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

STRENGTH_FACTORS = {
    "E0": Decimal(0),
    "E1": Decimal("0.40"),
    "E2": Decimal("0.75"),
    "E3": Decimal("1.00"),
}

ROLE_WEIGHTS: dict[str, dict[str, int]] = {
    "ai-product-manager": {
        "AIPM-PROD-01": 15,
        "AIPM-AI-01": 15,
        "AIPM-EVAL-01": 15,
        "AIPM-DATA-01": 10,
        "AIPM-DELIV-01": 15,
        "AIPM-OUT-01": 15,
        "AIPM-RISK-01": 10,
        "AIPM-COLLAB-01": 5,
    },
    "senior-fullstack-engineer": {
        "SEN-EXP-01": 10,
        "SEN-BE-01": 20,
        "SEN-ARCH-01": 15,
        "SEN-FE-01": 8,
        "SEN-DATA-01": 7,
        "SEN-AI-01": 3,
        "SEN-DOMAIN-01": 15,
        "SEN-LEVEL-01": 20,
        "SEN-ADM-01": 2,
    },
    "fullstack-development-intern": {
        "INT-ADM-01": 5,
        "INT-AVAIL-01": 5,
        "INT-BE-01": 20,
        "INT-WEB-01": 15,
        "INT-FE-01": 10,
        "INT-DATA-01": 15,
        "INT-PROJECT-01": 15,
        "INT-QUALITY-01": 5,
        "INT-AI-01": 5,
        "INT-DOMAIN-01": 5,
    },
}

@dataclass(frozen=True)
class ScoreResult:
    score: int
    grade: str
    review_status: str
    components: dict[str, int | float]
    scoring_version: str = "evidence-score-2026-09-01-v2"

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "grade": self.grade,
            "review_status": self.review_status,
            "components": self.components,
            "scoring_version": self.scoring_version,
        }


def _recommendation(record: dict[str, Any]) -> str:
    value = record.get("model_recommendation", record.get("recommendation"))
    if value not in {
        "advance_pending_human",
        "second_review",
        "do_not_advance_pending_human",
    }:
        raise ValueError("record has no supported recommendation state")
    return value


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


def score_record(record: dict[str, Any]) -> ScoreResult:
    """Return a rubric-versioned score without accepting model-supplied totals."""

    role = record.get("role")
    if role not in ROLE_WEIGHTS:
        raise ValueError(f"unsupported role: {role!r}")
    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        raise TypeError("evidence must be a list")
    by_criterion = {
        item.get("criterion_id"): item for item in evidence if isinstance(item, dict)
    }
    weights = ROLE_WEIGHTS[role]
    if set(by_criterion) != set(weights):
        raise ValueError("evidence criteria do not match the approved role weights")

    raw_components: dict[str, Decimal] = {}
    for criterion, weight in weights.items():
        item = by_criterion[criterion]
        strength = item.get("strength")
        try:
            factor = STRENGTH_FACTORS[strength]
        except KeyError as exc:
            raise ValueError(
                f"invalid evidence strength for {criterion}: {strength!r}"
            ) from exc
        if item.get("state") in {"directly_not_met", "conflicting"}:
            factor = Decimal(0)
        raw_components[criterion] = Decimal(weight) * factor

    total = sum(raw_components.values(), Decimal(0))
    score = int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    components: dict[str, int | float] = {}
    for criterion, value in raw_components.items():
        components[criterion] = (
            int(value) if value == value.to_integral() else float(value)
        )
    return ScoreResult(
        score=score,
        grade=_grade(score),
        review_status=_recommendation(record),
        components=components,
    )

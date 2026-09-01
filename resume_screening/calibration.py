"""Import human review reasons and produce non-prescriptive calibration reports."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

CALIBRATION_CATEGORIES = (
    "capability",
    "hard_eligibility",
    "process_or_commercial",
    "intent",
    "unknown",
)
CALIBRATION_CATEGORIES_FOR_STATS = {"capability", "hard_eligibility"}
NON_CAPABILITY_CATEGORIES = {
    "process_or_commercial",
    "intent",
    "unknown",
}
STATUS_ALIASES = {
    "advance": "advance",
    "推进": "advance",
    "通过": "advance",
    "推荐": "advance",
    "do_not_advance": "do_not_advance",
    "do-not-advance": "do_not_advance",
    "暂不推进": "do_not_advance",
    "不推进": "do_not_advance",
    "不通过": "do_not_advance",
    "淘汰": "do_not_advance",
    "second_review": "second_review",
    "second-review": "second_review",
    "二审": "second_review",
    "复核": "second_review",
    "待定": "second_review",
    "unknown": "unknown",
    "未知": "unknown",
    "": "unknown",
}
MODEL_RECOMMENDATION_ALIASES = {
    **STATUS_ALIASES,
    "advance_pending_human": "advance",
    "do_not_advance_pending_human": "do_not_advance",
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _value(row: dict[str, str], *names: str) -> str:
    for name in names:
        if name in row and _clean(row[name]):
            return _clean(row[name])
    return ""


def _normalize_status(value: str, *, model: bool = False) -> str:
    aliases = MODEL_RECOMMENDATION_ALIASES if model else STATUS_ALIASES
    normalized = aliases.get(value.casefold(), aliases.get(value, ""))
    if not normalized:
        raise ValueError(f"不支持的人工结论: {value!r}")
    return normalized


def read_reviews_csv(csv_path: str | Path) -> list[dict[str, Any]]:
    """Read a deliberately small, aggregate-safe CSV interchange format."""

    path = Path(csv_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("人工结果 CSV 缺少表头")
        rows: list[dict[str, Any]] = []
        for line_number, raw in enumerate(reader, start=2):
            row = {str(key): _clean(value) for key, value in raw.items() if key is not None}
            task_id_text = _value(row, "task_id", "task")
            task_id: int | None = None
            if task_id_text:
                try:
                    task_id = int(task_id_text)
                except ValueError as exc:
                    raise ValueError(f"第 {line_number} 行 task_id 不是整数") from exc
                if task_id < 1:
                    raise ValueError(f"第 {line_number} 行 task_id 必须为正整数")

            candidate_id = _value(row, "candidate_id", "candidate")
            if task_id is None and not candidate_id:
                raise ValueError(f"第 {line_number} 行必须提供 task_id 或 candidate_id")

            category = _value(row, "reason_category", "category", "reason_type").casefold()
            if category not in CALIBRATION_CATEGORIES:
                raise ValueError(
                    f"第 {line_number} 行 reason_category 必须是 "
                    f"{', '.join(CALIBRATION_CATEGORIES)}"
                )

            human_raw = _value(
                row,
                "human_capability_conclusion",
                "human_conclusion",
                "human_status",
                "decision",
                "status",
            )
            human_conclusion = _normalize_status(human_raw)
            model_raw = _value(
                row,
                "model_recommendation",
                "suggestion",
                "recommendation",
            )
            model_recommendation = (
                _normalize_status(model_raw, model=True) if model_raw else "unknown"
            )
            criterion_id = _value(row, "criterion_id", "disagreement_criterion") or None
            role = _value(row, "role") or None
            rubric_version = _value(row, "rubric_version") or None
            canonical = json.dumps(
                {
                    "task_id": task_id,
                    "candidate_id": candidate_id,
                    "role": role,
                    "rubric_version": rubric_version,
                    "model_recommendation": model_recommendation,
                    "human_conclusion": human_conclusion,
                    "reason_category": category,
                    "criterion_id": criterion_id,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            rows.append(
                {
                    "source_row_key": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    "task_id": task_id,
                    "candidate_id": candidate_id or None,
                    "role": role,
                    "rubric_version": rubric_version,
                    "model_recommendation": model_recommendation,
                    "human_conclusion": human_conclusion,
                    "reason_category": category,
                    "criterion_id": criterion_id,
                }
            )
    return rows


def _model_status(value: object) -> str:
    text = _clean(value)
    return MODEL_RECOMMENDATION_ALIASES.get(text.casefold(), "unknown")


def _human_status(value: object) -> str:
    text = _clean(value)
    return STATUS_ALIASES.get(text.casefold(), "unknown")


def _score(result: dict[str, Any]) -> int | None:
    scorecard = result.get("scorecard")
    value = scorecard.get("score") if isinstance(scorecard, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _go_gate(result: dict[str, Any]) -> bool | None:
    record = result.get("screening_record")
    if not isinstance(record, dict) or record.get("role") != "senior-fullstack-engineer":
        return None
    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if not isinstance(item, dict) or item.get("criterion_id") != "SEN-BE-01":
            continue
        strength = item.get("strength")
        return item.get("state") == "supported" and strength in {"E2", "E3"}
    return None


def _distribution(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "bands": {}}
    bands = Counter(
        "A" if value >= 85 else
        "B" if value >= 70 else
        "C" if value >= 55 else
        "D" if value >= 40 else "E"
        for value in values
    )
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(sum(values) / len(values), 2),
        "bands": {key: bands[key] for key in ("A", "B", "C", "D", "E") if bands[key]},
    }


def build_calibration_report(
    rows: list[dict[str, Any]], *, minimum_sample_size: int = 10
) -> dict[str, Any]:
    if minimum_sample_size < 1:
        raise ValueError("minimum_sample_size must be positive")
    eligible = [
        row
        for row in rows
        if row.get("reason_category") in CALIBRATION_CATEGORIES_FOR_STATS
    ]
    non_capability = [
        row for row in rows if row.get("reason_category") in NON_CAPABILITY_CATEGORIES
    ]

    scores_by_status: dict[str, list[int]] = defaultdict(list)
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    go_hits = 0
    go_total = 0
    disagreement_criteria: Counter[str] = Counter()
    for row in eligible:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        score = _score(result)
        human = _human_status(row.get("human_conclusion"))
        model = _model_status(row.get("model_recommendation"))
        if score is not None:
            scores_by_status[human].append(score)
        confusion[model][human] += 1
        gate = _go_gate(result)
        if gate is not None:
            go_total += 1
            go_hits += int(gate)
        criterion = _clean(row.get("criterion_id"))
        if criterion and model != human:
            disagreement_criteria[criterion] += 1

    sample_size = len(eligible)
    sample_sufficient = sample_size >= minimum_sample_size
    report: dict[str, Any] = {
        "report_version": "calibration-report-2026-09-01-v1",
        "sample_size": sample_size,
        "minimum_sample_size": minimum_sample_size,
        "sample_sufficient": sample_sufficient,
        "sample_warning": (
            None
            if sample_sufficient
            else f"能力/硬资格样本不足（当前 {sample_size}，至少需要 {minimum_sample_size}），不生成调权建议。"
        ),
        "score_distribution_by_status": {
            status: _distribution(scores_by_status[status])
            for status in sorted(scores_by_status)
        },
        "go_gate_hit_rate": {
            "hit": go_hits,
            "total": go_total,
            "rate": round(go_hits / go_total, 4) if go_total else None,
        },
        "recommendation_vs_human_capability_confusion_matrix": {
            model: {
                human: confusion[model][human]
                for human in ("advance", "do_not_advance", "second_review", "unknown")
                if confusion[model][human]
            }
            for model in ("advance", "do_not_advance", "second_review", "unknown")
            if confusion[model]
        },
        "primary_disagreement_criteria": [
            {"criterion_id": criterion, "count": count}
            for criterion, count in disagreement_criteria.most_common()
        ],
        "non_capability_reason_counts": {
            category: sum(1 for row in non_capability if row.get("reason_category") == category)
            for category in ("process_or_commercial", "intent", "unknown")
        },
        "excluded_non_capability_sample_size": len(non_capability),
        "weight_changes": None,
        "calibration_note": "报告只用于观察分歧；不会自动修改权重。",
    }
    return report

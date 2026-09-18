"""Versioned, provider-neutral AI results used by the desktop workbench.

The role-owned screening records remain the compatibility format used by the
CLI.  This module adds the smaller desktop contract around those records.  It
never trusts a model supplied score: the score is recomputed by Python and
evidence excerpts are checked against the exact redacted text sent to the
provider.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from .scoring import score_record
from .versions import AI_OUTPUT_CONTRACT_VERSION

_ASSESSMENTS = {
    "supported": "supported",
    "directly_not_met": "not_supported",
    "not_evidenced": "insufficient",
    "conflicting": "insufficient",
}
_RECOMMENDATIONS = {
    "advance_pending_human": "advance",
    "do_not_advance_pending_human": "do_not_advance",
    "second_review": "manual_review",
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _summary(record: dict[str, Any]) -> str:
    if record.get("role") in {"ai-product-manager", "operations-devops-engineer"}:
        value = record.get("summary")
        if isinstance(value, dict):
            return str(value.get("one_line_conclusion") or "")
    return str(record.get("recommendation_rationale") or record.get("summary") or "")


def _recommendation(record: dict[str, Any]) -> str:
    value = record.get("model_recommendation", record.get("recommendation"))
    try:
        return _RECOMMENDATIONS[value]
    except KeyError as exc:
        raise ValueError("模型建议不属于版本化 AI 合同允许的状态") from exc


def _evidence_quote(item: dict[str, Any], redacted_text: str) -> tuple[str | None, str | None]:
    excerpt = item.get("excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return None, None
    quote = excerpt.strip()
    if quote not in redacted_text:
        return None, str(item.get("criterion_id") or "unknown")
    return quote, None


def _location(item: dict[str, Any]) -> str:
    value = item.get("location")
    return str(value).strip() if value else "未标注位置"


def _text_items(*values: Any) -> list[str]:
    """Keep provider phrasing while making list/string variants deterministic."""

    result: list[str] = []
    for value in values:
        if value is None:
            continue
        candidates = value if isinstance(value, (list, tuple)) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate = (
                    candidate.get("description")
                    or candidate.get("reason")
                    or candidate.get("text")
                    or ""
                )
            text = str(candidate).strip()
            if text and text not in result:
                result.append(text)
    return result


def _location_aware_score(record: dict[str, Any], redacted_text: str) -> dict[str, Any]:
    """Recompute the score with unlocatable E1+ excerpts treated as E0."""

    evidence = []
    for original in record.get("evidence", []):
        item = dict(original)
        strength = item.get("strength")
        quote, _ = _evidence_quote(item, redacted_text)
        if strength != "E0" and not quote:
            item["strength"] = "E0"
        evidence.append(item)
    score_input = dict(record)
    score_input["evidence"] = evidence
    return score_record(score_input).as_dict()


def build_ai_result(
    record: dict[str, Any],
    scorecard: dict[str, Any],
    *,
    document_id: str,
    redacted_text: str,
    provider: str = "",
    endpoint_identity: str = "",
    model: str = "",
    parser_version: str = "",
    scoring_version: str = "",
    prompt_version: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Normalize a validated role record into the desktop AI result contract."""

    criteria: list[dict[str, Any]] = []
    locator_warnings: list[dict[str, str]] = []
    for item in record.get("evidence", []):
        if not isinstance(item, dict):
            raise TypeError("每项 AI 证据必须是对象")
        criterion = str(item.get("criterion_id") or "").strip()
        if not criterion:
            raise ValueError("AI 证据缺少 criterion_id")
        quote, warning = _evidence_quote(item, redacted_text)
        evidence = (
            [{"location": _location(item), "quote": quote}]
            if quote
            else []
        )
        if warning:
            locator_warnings.append(
                {"criterion": warning, "reason": "引用无法在本次脱敏输入中定位，未计分"}
            )
        state = str(item.get("state") or "")
        assessment = _ASSESSMENTS.get(state, "insufficient")
        rationale = str(item.get("rationale") or "").strip()
        gaps = _text_items(
            item.get("gaps"),
            item.get("missing"),
            item.get("conflicts"),
            rationale if state != "supported" else None,
        )
        risks = _text_items(
            item.get("risks"), item.get("risk_flags"), item.get("risk")
        )
        if state == "conflicting" and "证据存在冲突" not in risks:
            risks.append("证据存在冲突")
        criteria.append(
            {
                "code": criterion,
                "assessment": assessment,
                "evidence": evidence,
                "gaps": gaps,
                "risks": risks,
            }
        )

    location_score = _location_aware_score(record, redacted_text)
    rule_versions = {
        "jd": record.get("jd_version", ""),
        "rubric": record.get("rubric_version", ""),
        "parser": parser_version,
        "scoring": scoring_version or location_score.get("scoring_version", ""),
        "prompt": prompt_version,
    }
    probes = record.get("interview_probes", [])
    if not isinstance(probes, (list, tuple)):
        probes = []
    result = {
        "output_contract_version": AI_OUTPUT_CONTRACT_VERSION,
        "document_id": document_id,
        "role": record.get("role", ""),
        "input_redacted_sha256": _sha256_text(redacted_text),
        "provider": provider,
        "endpoint_identity": endpoint_identity,
        "model": model,
        "rule_versions": rule_versions,
        "criteria": criteria,
        "computed_score": {
            "value": location_score["score"],
            "band": location_score["grade"],
            "components": location_score.get("components", {}),
        },
        "model_recommendation": _recommendation(record),
        "summary": _summary(record),
        "interview_questions": _text_items(
            [
                item.get("question", "")
                if isinstance(item, dict)
                else item
                for item in probes
            ]
        ),
        "uncertainty_reasons": _text_items(
            record.get("uncertainties"), record.get("uncertainty_reasons")
        ),
        "created_at": created_at or datetime.now(UTC).isoformat(),
    }
    if locator_warnings:
        result["evidence_warnings"] = locator_warnings
    return result


def validate_ai_result(result: dict[str, Any], redacted_text: str) -> list[str]:
    """Validate the public desktop contract and exact evidence locations."""

    errors: list[str] = []
    if not isinstance(result, dict):
        return ["AI 结果必须是 JSON 对象"]
    if result.get("output_contract_version") != AI_OUTPUT_CONTRACT_VERSION:
        errors.append("AI 输出合同版本不受支持")
    required = (
        "document_id",
        "role",
        "input_redacted_sha256",
        "provider",
        "endpoint_identity",
        "model",
        "rule_versions",
        "criteria",
        "computed_score",
        "model_recommendation",
        "summary",
        "interview_questions",
        "uncertainty_reasons",
        "created_at",
    )
    for key in required:
        if key not in result:
            errors.append(f"AI 结果缺少字段：{key}")
    if result.get("input_redacted_sha256") != _sha256_text(redacted_text):
        errors.append("AI 结果输入 hash 与本次脱敏文本不一致")
    recommendation = result.get("model_recommendation")
    if recommendation not in {"advance", "do_not_advance", "manual_review"}:
        errors.append("AI 建议状态无效")
    score = result.get("computed_score")
    if not isinstance(score, dict) or not isinstance(score.get("value"), int):
        errors.append("computed_score 必须包含整数 value")
    else:
        if not 0 <= score["value"] <= 100:
            errors.append("computed_score.value 必须在 0 到 100 之间")
        if score.get("band") not in {"A", "B", "C", "D", "E"}:
            errors.append("computed_score.band 无效")
    if not isinstance(result.get("rule_versions"), dict):
        errors.append("rule_versions 必须是对象")
    if not isinstance(result.get("summary"), str):
        errors.append("summary 必须是字符串")
    for key in ("interview_questions", "uncertainty_reasons"):
        if not isinstance(result.get(key), list):
            errors.append(f"{key} 必须是数组")
    criteria = result.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("criteria 必须是非空数组")
        criteria = []
    seen: set[str] = set()
    for item in criteria:
        if not isinstance(item, dict):
            errors.append("criteria 中存在非对象项")
            continue
        code = item.get("code")
        if not isinstance(code, str) or not code:
            errors.append("criteria 项缺少 code")
        elif code in seen:
            errors.append(f"criterion 重复：{code}")
        else:
            seen.add(code)
        if item.get("assessment") not in {"supported", "not_supported", "insufficient"}:
            errors.append(f"criterion {code} 的 assessment 无效")
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"criterion {code} 的 evidence 必须是数组")
            continue
        for citation in evidence:
            if (
                not isinstance(citation, dict)
                or not isinstance(citation.get("location"), str)
                or not citation.get("location", "").strip()
                or not isinstance(citation.get("quote"), str)
                or not citation.get("quote", "").strip()
            ):
                errors.append(f"criterion {code} 的 evidence 引用格式无效")
            elif citation["quote"] not in redacted_text:
                errors.append(f"criterion {code} 的引用无法在脱敏输入中定位")
        for key in ("gaps", "risks"):
            if not isinstance(item.get(key, []), list):
                errors.append(f"criterion {code} 的 {key} 必须是数组")
    return errors


# Explicit aliases make the normalization seam easy to discover for callers
# and keep the public name stable if the implementation module is reorganized.
normalize_ai_result = build_ai_result
validate_result_contract = validate_ai_result

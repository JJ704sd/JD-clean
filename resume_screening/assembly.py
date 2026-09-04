"""Build policy-controlled screening records from model-extracted evidence."""

from __future__ import annotations

import re
from typing import Any

SENIOR_CRITERIA = (
    "SEN-EXP-01",
    "SEN-BE-01",
    "SEN-ARCH-01",
    "SEN-FE-01",
    "SEN-DATA-01",
    "SEN-AI-01",
    "SEN-DOMAIN-01",
    "SEN-LEVEL-01",
    "SEN-ADM-01",
)
STRENGTH_RANK = {"E0": 0, "E1": 1, "E2": 2, "E3": 3}
V7_ADVANCE_MINIMUMS = {
    "SEN-EXP-01": "E2",
    "SEN-BE-01": "E2",
    "SEN-ARCH-01": "E2",
    "SEN-FE-01": "E2",
    "SEN-DATA-01": "E2",
    "SEN-LEVEL-01": "E3",
    "SEN-ADM-01": "E1",
}
V8_ADVANCE_MINIMUMS = {
    "SEN-EXP-01": "E2",
    "SEN-BE-01": "E2",
    "SEN-ARCH-01": "E2",
    "SEN-DATA-01": "E2",
    "SEN-LEVEL-01": "E2",
    "SEN-ADM-01": "E1",
}
V9_ADVANCE_MINIMUMS = V8_ADVANCE_MINIMUMS
V7_NEGATIVE_CORE = {"SEN-BE-01", "SEN-ARCH-01", "SEN-FE-01", "SEN-DATA-01"}
V8_NEGATIVE_CORE = {"SEN-BE-01", "SEN-ARCH-01", "SEN-DATA-01", "SEN-LEVEL-01"}
V9_NEGATIVE_CORE = V8_NEGATIVE_CORE
V7_DIRECT_CRITICAL = {"SEN-BE-01", "SEN-FE-01"}
V8_DIRECT_CRITICAL = {"SEN-BE-01"}
V9_DIRECT_CRITICAL = V8_DIRECT_CRITICAL
V10_RUBRIC_VERSION = "senior-fullstack-2026-09-04-v10"
V10_DIMENSION_CRITERIA = {
    "education": ("SEN-ADM-01", "E1"),
    "logistics": ("SEN-DOMAIN-01", "E2"),
    "valuable_project": ("SEN-LEVEL-01", "E2"),
}
UNCERTAINTY_CODES = {
    "U01_PARSE_QUALITY",
    "U02_MUST_HAVE_MISSING",
    "U03_CONFLICTING_FACTS",
    "U04_CONTRIBUTION_UNCLEAR",
    "U05_TRANSFERABILITY",
    "U06_BOUNDARY_CASE",
    "U07_BIAS_OR_PROXY",
    "U08_DIMENSION_CONFLICT",
    "U09_ROLE_AMBIGUITY",
    "U10_RUBRIC_AMBIGUITY",
    "U11_UNTRUSTED_CONTENT",
}
SOURCE_FACT_CODES = {
    "U01_PARSE_QUALITY",
    "U02_MUST_HAVE_MISSING",
    "U03_CONFLICTING_FACTS",
}
INDEPENDENT_REVIEW_CODES = {
    "U05_TRANSFERABILITY",
    "U07_BIAS_OR_PROXY",
    "U09_ROLE_AMBIGUITY",
    "U10_RUBRIC_AMBIGUITY",
    "U11_UNTRUSTED_CONTENT",
}
GO_PATTERN = re.compile(r"(?i)(?:\bgo\b|golang|go-zero|\bgin\b)")
LANGUAGE_TRANSITION_PATTERN = re.compile(
    r"转(?:语言|栈)|跨语言|技术栈迁移|语言迁移|从.{0,20}(?:转到|迁移到|切换到).{0,20}|"
    r"migrat(?:e|ed|ion)",
    re.IGNORECASE,
)
LEARNING_ACTION_PATTERN = re.compile(r"自学|快速学习|主动学习|learn(?:ed|ing)?", re.IGNORECASE)
DELIVERY_RESULT_PATTERN = re.compile(r"上线|交付|落地|投产|生产|发布|deliver(?:ed|y)?", re.IGNORECASE)
LANGUAGE_NEGATION_PATTERN = re.compile(
    r"(?:未提供|未体现|未说明|没有|无).{0,16}(?:转语言|转栈|跨语言|语言迁移|技术栈迁移|学习|自学|交付)",
    re.IGNORECASE,
)
REFACTOR_PATTERN = re.compile(r"重构|迁移|拆分|改造|re-?architect|refactor", re.IGNORECASE)
ROLE_MISMATCH_PATTERN = re.compile(
    r"求职意向\s*[:：]?[^\n]{0,40}(?:AI\s*产品经理|产品经理|数据分析师|运营)",
    re.IGNORECASE,
)
UNTRUSTED_INSTRUCTION_PATTERN = re.compile(
    r"(?:忽略|绕过|无视).{0,20}(?:岗位|JD|规则|要求|指令)|"
    r"(?:直接|必须).{0,12}(?:通过|录用|给高分)|"
    r"(?:运行|执行).{0,12}(?:命令|脚本|代码)|"
    r"(?:读取|泄露|输出).{0,12}(?:环境变量|密钥|密码|系统提示)",
    re.IGNORECASE,
)
E3_FACTOR_FIELDS = (
    "project_context",
    "personal_action",
    "method_or_tradeoff",
    "result_scope",
    "verifiable_impact",
)
MISSING_FACT_MARKERS = {
    "",
    "null",
    "none",
    "n/a",
    "na",
    "unknown",
    "not provided",
    "not mentioned",
    "not specified",
    "no evidence",
    "未提供",
    "未提及",
    "未说明",
    "未写明",
    "无",
    "无相关信息",
    "无明确证据",
    "无法确认",
}

PROBE_TEXT = {
    "SEN-EXP-01": "请核实研发年限、全栈职责范围及对应项目时间。",
    "SEN-BE-01": "请说明目标语言项目，或一次转语言/转技术栈的学习过程、个人动作和交付结果。",
    "SEN-ARCH-01": "请说明架构方案中的个人决策、约束、备选方案和结果。",
    "SEN-FE-01": "请说明独立负责的前端模块、技术方案和上线结果。",
    "SEN-DATA-01": "请说明数据建模、数据库或缓存方面的个人工程动作。",
    "SEN-AI-01": "请说明 AI 或 RAG 工程接入、评测、降级和监控经验。",
    "SEN-DOMAIN-01": "请说明 WMS、TMS/VMS、ERP、订单、履约、轨迹、计费等物流业务场景和个人交付内容。",
    "SEN-LEVEL-01": "请说明最有价值项目的业务量、使用量、个人参与程度、业务复杂度和可验证结果。",
    "SEN-ADM-01": "请由招聘责任人核对教育背景等行政信息。",
}

AI_CRITERIA = {
    "AIPM-PROD-01": "产品发现与定义",
    "AIPM-AI-01": "AI 方案理解与边界",
    "AIPM-EVAL-01": "评测与迭代",
    "AIPM-DATA-01": "数据/知识治理",
    "AIPM-DELIV-01": "端到端交付",
    "AIPM-OUT-01": "用户与业务结果",
    "AIPM-RISK-01": "风险、安全与合规",
    "AIPM-COLLAB-01": "协作与所有权",
}
AI_DECISION_CRITERIA = {
    "AIPM-PROD-01",
    "AIPM-AI-01",
    "AIPM-DELIV-01",
    "AIPM-EVAL-01",
    "AIPM-OUT-01",
}
AI_PROBE_TEXT = {
    "AIPM-PROD-01": "请说明目标用户、问题优先级、成功指标及为何需要 AI。",
    "AIPM-AI-01": "请说明 AI 方案选择、能力边界、成本延迟取舍和替代方案。",
    "AIPM-EVAL-01": "请说明评测集来源、基线、指标口径和 Bad Case 迭代方法。",
    "AIPM-DATA-01": "请说明数据或知识的来源、更新、权限和质量治理机制。",
    "AIPM-DELIV-01": "请说明你本人负责的研发协作、测试、上线和迭代环节。",
    "AIPM-OUT-01": "请说明业务结果的基线、分母、观察周期及个人归因边界。",
    "AIPM-RISK-01": "请举例说明幻觉、拒答、转人工、权限或回滚机制。",
    "AIPM-COLLAB-01": "请说明一次跨团队分歧、你的决策动作和最终结果。",
}


def _trim(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _ai_supported(item: dict[str, Any]) -> bool:
    return item.get("state") == "supported" and item.get("strength") in {"E2", "E3"}


def _normalize_ai_uncertainties(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("model evidence payload uncertainties must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("model uncertainty must be an object")
        code = item.get("code")
        description = _trim(item.get("description"), 160)
        if code not in UNCERTAINTY_CODES:
            raise ValueError(f"model uncertainty code is invalid: {code!r}")
        if not description:
            raise ValueError(f"model uncertainty {code} is missing description")
        if code not in seen:
            result.append(
                {
                    "code": code,
                    "description": description,
                    "requires_second_review": True,
                }
            )
            seen.add(code)
    return result


def _add_ai_uncertainty(
    uncertainties: list[dict[str, Any]], code: str, description: str
) -> None:
    if code not in {item["code"] for item in uncertainties}:
        uncertainties.append(
            {
                "code": code,
                "description": _trim(description, 160),
                "requires_second_review": True,
            }
        )


def _normalize_ai_probes(value: Any, evidence: list[dict[str, Any]]) -> list[str]:
    probes: list[str] = []
    if isinstance(value, list):
        for item in value:
            question = item.get("question") if isinstance(item, dict) else item
            question = _trim(question, 200)
            if question and question not in probes:
                probes.append(question)
            if len(probes) == 6:
                break
    weakest = sorted(
        evidence,
        key=lambda item: (
            STRENGTH_RANK.get(item.get("strength"), -1),
            list(AI_CRITERIA).index(item["criterion_id"]),
        ),
    )
    for item in weakest:
        question = AI_PROBE_TEXT[item["criterion_id"]]
        if question not in probes:
            probes.append(question)
        if len(probes) >= 3:
            break
    return probes[:6]


def _ai_human_review(
    recommendation: str, uncertainties: list[dict[str, Any]]
) -> dict[str, Any]:
    reasons = [item["code"] for item in uncertainties]
    if recommendation == "do_not_advance_pending_human":
        reasons.append("H02_NEGATIVE_RECOMMENDATION")
    level_2_required = bool(reasons)
    return {
        "level_1_required": True,
        "level_1_reviewer": None,
        "level_1_decision": None,
        "level_1_reviewed_at": None,
        "level_2_required": level_2_required,
        "level_2_mode": "independent_reviewer" if level_2_required else "not_required",
        "level_2_reason_codes": reasons,
        "level_2_reviewer": None,
        "level_2_decision": None,
        "level_2_reviewed_at": None,
        "prior_recommendations_hidden_during_recheck": level_2_required,
        "reviewers_agree": None,
        "disagreement_reason": None,
        "resolution_owner": None,
        "resolution": None,
    }


def assemble_ai_product_manager_record(
    payload: dict[str, Any],
    *,
    screening_record_id: str,
    candidate_id: str,
    candidate_name: str | None,
    jd_version: str,
    rubric_version: str,
) -> dict[str, Any]:
    """Convert AI-PM evidence into a canonical policy-controlled record."""

    value = payload.get("evidence")
    if not isinstance(value, list):
        raise TypeError("model evidence payload must contain an evidence list")
    by_id: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("model evidence item must be an object")
        criterion_id = item.get("criterion_id")
        if criterion_id not in AI_CRITERIA:
            raise ValueError(f"model evidence criterion is invalid: {criterion_id!r}")
        if criterion_id in by_id:
            raise ValueError(f"duplicate model evidence criterion: {criterion_id}")
        normalized = {
            "criterion_id": criterion_id,
            "criterion_name": AI_CRITERIA[criterion_id],
            "state": item.get("state"),
            "strength": item.get("strength"),
            "excerpt": item.get("excerpt"),
            "location": item.get("location"),
            "rationale": _trim(item.get("rationale"), 240),
            "confidence": item.get("confidence"),
        }
        by_id[criterion_id] = normalized
    missing = set(AI_CRITERIA) - set(by_id)
    if missing:
        raise ValueError(
            f"missing model evidence criteria: {', '.join(sorted(missing))}"
        )
    evidence = [by_id[criterion_id] for criterion_id in AI_CRITERIA]

    uncertainties = _normalize_ai_uncertainties(payload.get("uncertainties"))
    if any(item["state"] == "conflicting" for item in evidence):
        _add_ai_uncertainty(
            uncertainties, "U03_CONFLICTING_FACTS", "简历中的决策相关证据存在冲突"
        )
    if any(
        item["criterion_id"] in AI_DECISION_CRITERIA and item["confidence"] == "low"
        for item in evidence
    ):
        _add_ai_uncertainty(
            uncertainties, "U06_BOUNDARY_CASE", "方向性判断依赖低置信度证据"
        )

    core = ("AIPM-PROD-01", "AIPM-AI-01", "AIPM-DELIV-01")
    weak_core = [
        by_id[criterion] for criterion in core if not _ai_supported(by_id[criterion])
    ]
    evaluation = by_id["AIPM-EVAL-01"]
    outcome = by_id["AIPM-OUT-01"]
    advance_gate = (
        not weak_core
        and (_ai_supported(evaluation) or _ai_supported(outcome))
        and evaluation.get("strength") != "E0"
        and outcome.get("strength") != "E0"
    )
    if uncertainties:
        recommendation = "second_review"
    elif advance_gate:
        recommendation = "advance_pending_human"
    elif len(weak_core) >= 2 and all(
        item.get("confidence") != "low" for item in weak_core
    ):
        recommendation = "do_not_advance_pending_human"
    else:
        _add_ai_uncertainty(
            uncertainties,
            "U06_BOUNDARY_CASE",
            "核心证据尚未达到直接推进或明确暂不推进门槛",
        )
        recommendation = "second_review"

    review = _ai_human_review(recommendation, uncertainties)
    probes = _normalize_ai_probes(payload.get("interview_probes"), evidence)
    strengths = [
        {
            "criterion_id": item["criterion_id"],
            "finding": _trim(item["rationale"], 100),
        }
        for item in evidence
        if _ai_supported(item)
    ][:3]
    gaps = [
        {
            "criterion_id": item["criterion_id"],
            "finding": _trim(item["rationale"], 100),
        }
        for item in evidence
        if not _ai_supported(item)
    ][:3]

    if recommendation == "advance_pending_human":
        label = "建议推进（待人工确认）"
        conclusion = (
            "产品发现、AI 方案和端到端交付证据达到推进门槛，结论待人工一审核验。"
        )
        next_step = "核对核心证据原文、个人贡献和结果口径并完成人工一审"
    elif recommendation == "do_not_advance_pending_human":
        label = "建议暂不推进（待双重人工确认）"
        conclusion = "至少两个核心维度未提供充分证据，建议经双重人工核验后再决定。"
        next_step = "独立复核核心证据缺口并确认是否存在可迁移经验"
    else:
        label = "建议二次复核"
        conclusion = "存在可能改变判断的不确定性，需独立复核证据和原因码后再决定。"
        next_step = "按原因码独立回看原始简历并核对相关证据"

    if review["level_2_required"]:
        requirement = (
            "人工一审 + 独立二审（原因："
            + "、".join(review["level_2_reason_codes"])
            + "）"
        )
    else:
        requirement = "仅人工一审"
    record: dict[str, Any] = {
        "schema_version": "1.2",
        "screening_record_id": screening_record_id,
        "candidate_id": candidate_id,
        "role": "ai-product-manager",
        "screening_basis": "approved_jd",
        "jd_hard_gates_approved": True,
        "jd_version": jd_version,
        "rubric_version": rubric_version,
        "screening_status": "non_final",
        "recommendation": recommendation,
        "summary": {
            "conclusion_label": label,
            "one_line_conclusion": conclusion,
            "top_strengths": strengths,
            "key_gaps": gaps,
            "human_review_requirement": requirement,
            "next_step": next_step,
        },
        "hard_gate_conflicts": [],
        "evidence": evidence,
        "uncertainties": uncertainties,
        "interview_probes": probes,
        "sensitive_attributes_used": False,
        "human_review": review,
        "automation_actions": [],
    }
    if candidate_name:
        record["candidate_name"] = candidate_name
    return record


def _supported(item: dict[str, Any], minimum: str) -> bool:
    return (
        item.get("state") == "supported"
        and STRENGTH_RANK.get(item.get("strength"), -1) >= STRENGTH_RANK[minimum]
    )


def _derive_v4_senior_evidence(value: list[Any]) -> list[dict[str, Any]]:
    def has_fact(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        normalized = " ".join(value.split()).strip().strip("。；;，,、:：")
        folded = normalized.casefold()
        return bool(normalized) and folded not in MISSING_FACT_MARKERS and not folded.startswith(
            (
                "未提供",
                "未提及",
                "未说明",
                "未写明",
                "无法确认",
                "not provided",
                "not mentioned",
                "not specified",
            )
        )

    evidence: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise TypeError("model evidence item must be an object")
        item = dict(raw)
        factors = item.pop("evidence_factors", None)
        factor_map = factors if isinstance(factors, dict) else {}
        present = {
            field: has_fact(factor_map.get(field))
            for field in E3_FACTOR_FIELDS
        }
        state = item.get("state")
        excerpt_present = isinstance(item.get("excerpt"), str) and bool(
            item["excerpt"].strip()
        )
        location_present = isinstance(item.get("location"), str) and bool(
            item["location"].strip()
        )
        if state == "supported":
            if item.get("criterion_id") == "SEN-ADM-01":
                item["strength"] = "E1" if excerpt_present and location_present else "E0"
                if item["strength"] == "E0":
                    item["state"] = "not_evidenced"
            elif (
                item.get("criterion_id") == "SEN-LEVEL-01"
                and present["project_context"]
                and present["personal_action"]
                and any(
                    present[field]
                    for field in ("method_or_tradeoff", "result_scope", "verifiable_impact")
                )
            ):
                item["strength"] = "E3" if all(present.values()) else "E2"
            elif (
                item.get("criterion_id") != "SEN-LEVEL-01"
                and present["project_context"]
                and present["personal_action"]
            ):
                item["strength"] = "E3" if all(present.values()) else "E2"
            else:
                item["state"] = "not_evidenced"
                item["strength"] = "E1" if excerpt_present and location_present else "E0"
        elif state in {"conflicting", "directly_not_met"}:
            item["strength"] = "E1" if excerpt_present and location_present else "E0"
        else:
            item["state"] = "not_evidenced"
            item["strength"] = "E1" if excerpt_present and location_present else "E0"
            if item["strength"] == "E0":
                item["excerpt"] = None
                item["location"] = None
        evidence.append(item)
    return evidence


def _uncertainty(
    code: str, description: str, decision_impact: str, action: str
) -> dict[str, str]:
    return {
        "code": code,
        "description": _trim(description, 120),
        "decision_impact": _trim(decision_impact, 200),
        "required_human_action": _trim(action, 200),
    }


def _normalize_uncertainties(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise TypeError("model evidence payload uncertainties must be a list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("model uncertainty must be an object")
        code = item.get("code")
        if code not in UNCERTAINTY_CODES:
            raise ValueError(f"model uncertainty code is invalid: {code!r}")
        if code in seen:
            continue
        fields = (
            item.get("description"),
            item.get("decision_impact"),
            item.get("required_human_action"),
        )
        if not all(isinstance(field, str) and field.strip() for field in fields):
            raise ValueError(f"model uncertainty {code} is missing required text")
        result.append(_uncertainty(code, *fields))
        seen.add(code)
    return result


def _add_uncertainty(uncertainties: list[dict[str, str]], item: dict[str, str]) -> None:
    if item["code"] not in {existing["code"] for existing in uncertainties}:
        uncertainties.append(item)


def _normalize_probes(
    value: Any, evidence: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            criterion = item.get("criterion_id")
            question = _trim(item.get("question"), 200)
            signal = _trim(item.get("expected_signal"), 200)
            key = (str(criterion), question)
            if criterion in SENIOR_CRITERIA and question and signal and key not in seen:
                result.append(
                    {
                        "criterion_id": criterion,
                        "question": question,
                        "expected_signal": signal,
                    }
                )
                seen.add(key)
            if len(result) == 5:
                break
    if not result:
        criterion = min(
            SENIOR_CRITERIA,
            key=lambda item: STRENGTH_RANK.get(
                evidence.get(item, {}).get("strength"), -1
            ),
        )
        result.append(
            {
                "criterion_id": criterion,
                "question": PROBE_TEXT[criterion],
                "expected_signal": "能够给出可核对的个人动作、技术依据和结果。",
            }
        )
    return [dict(item, priority=index) for index, item in enumerate(result, start=1)]


def _ensure_probe(probes: list[dict[str, Any]], criterion: str) -> None:
    if any(item["criterion_id"] == criterion for item in probes):
        return
    if len(probes) == 5:
        probes.pop()
    probes.append(
        {
            "priority": len(probes) + 1,
            "criterion_id": criterion,
            "question": PROBE_TEXT[criterion],
            "expected_signal": "能够给出可核对的个人动作、技术依据和结果。",
        }
    )


def _review(uncertainties: list[dict[str, str]]) -> dict[str, Any]:
    codes = [item["code"] for item in uncertainties]
    required = bool(codes)
    if not required:
        mode = "not_required"
        blind = False
        preferred = False
        status = "not_required"
    elif set(codes).issubset(SOURCE_FACT_CODES):
        mode = "source_fact_confirmation"
        blind = False
        preferred = False
        status = "pending"
    elif set(codes) & INDEPENDENT_REVIEW_CODES:
        mode = "independent_reviewer"
        blind = True
        preferred = True
        status = "pending"
    else:
        mode = "same_owner_separate_pass"
        blind = True
        preferred = False
        status = "pending"
    return {
        "level_1_required": True,
        "level_1_status": "pending",
        "level_1_reviewer": None,
        "level_1_decision": None,
        "level_1_reviewed_at": None,
        "level_2_required": required,
        "level_2_status": status,
        "level_2_mode": mode,
        "level_2_reason_codes": codes,
        "independent_review_preferred": preferred,
        "independent_review_fallback_reason": None,
        "blind_review_required": blind,
        "blind_review_confirmed": None,
        "level_2_reviewer": None,
        "level_2_decision": None,
        "level_2_reviewed_at": None,
        "final_disposition": None,
        "resolution": None,
    }


def assemble_senior_record(
    payload: dict[str, Any],
    *,
    screening_record_id: str,
    candidate_id: str,
    candidate_name: str | None,
    jd_version: str,
    rubric_version: str,
    prompt_version: str | None = None,
    resume_text: str = "",
) -> dict[str, Any]:
    """Convert evidence-only model output into a complete policy record."""

    evidence_value = payload.get("evidence")
    if not isinstance(evidence_value, list):
        raise TypeError("model evidence payload must contain an evidence list")
    evidence = (
        _derive_v4_senior_evidence(evidence_value)
        if prompt_version
        in {
            "resume-screening-prompt-2026-09-01-v4",
            "resume-screening-prompt-2026-09-04-v5",
        }
        and rubric_version
        in {
            "senior-fullstack-2026-09-01-v7",
            "senior-fullstack-2026-09-01-v8",
            "senior-fullstack-2026-09-03-v9",
            V10_RUBRIC_VERSION,
        }
        else evidence_value
    )
    by_criterion = {
        item.get("criterion_id"): item for item in evidence if isinstance(item, dict)
    }
    current_v10 = rubric_version == V10_RUBRIC_VERSION
    current_v9 = rubric_version == "senior-fullstack-2026-09-03-v9"
    current_v8 = rubric_version == "senior-fullstack-2026-09-01-v8"
    current_v8_or_v9 = current_v8 or current_v9
    if current_v10:
        advance_minimums = V9_ADVANCE_MINIMUMS
        negative_core = V9_NEGATIVE_CORE
        direct_critical = V9_DIRECT_CRITICAL
    elif current_v9:
        advance_minimums = V9_ADVANCE_MINIMUMS
        negative_core = V9_NEGATIVE_CORE
        direct_critical = V9_DIRECT_CRITICAL
    elif current_v8:
        advance_minimums = V8_ADVANCE_MINIMUMS
        negative_core = V8_NEGATIVE_CORE
        direct_critical = V8_DIRECT_CRITICAL
    else:
        advance_minimums = V7_ADVANCE_MINIMUMS
        negative_core = V7_NEGATIVE_CORE
        direct_critical = V7_DIRECT_CRITICAL
    uncertainties = _normalize_uncertainties(payload.get("uncertainties"))
    incoming_codes = {item["code"] for item in uncertainties}
    uncertainties = [
        item
        for item in uncertainties
        if item["code"]
        not in {
            "U01_PARSE_QUALITY",
            "U09_ROLE_AMBIGUITY",
            "U10_RUBRIC_AMBIGUITY",
            "U11_UNTRUSTED_CONTENT",
        }
    ]
    if ROLE_MISMATCH_PATTERN.search(resume_text):
        _add_uncertainty(
            uncertainties,
            _uncertainty(
                "U09_ROLE_AMBIGUITY",
                "简历明确写出的求职意向与高级全栈岗位不同",
                "若候选人满足 Go 门槛，岗位意愿可能改变推进判断",
                "由招聘责任人确认候选人实际投递岗位和转岗意愿",
            ),
        )
    if UNTRUSTED_INSTRUCTION_PATTERN.search(resume_text):
        _add_uncertainty(
            uncertainties,
            _uncertainty(
                "U11_UNTRUSTED_CONTENT",
                "简历包含要求改变筛选规则、直接通过或运行命令的指令性内容",
                "该内容不能作为证据，且需要确认原文件是否被污染",
                "忽略指令性内容并由独立审核人核对原文件",
            ),
        )
    if any(item.get("state") == "conflicting" for item in by_criterion.values()):
        _add_uncertainty(
            uncertainties,
            _uncertainty(
                "U03_CONFLICTING_FACTS",
                "简历中的决策相关证据存在冲突",
                "冲突事实会改变证据强度和筛选方向",
                "回看原始简历并核对冲突字段",
            ),
        )

    backend = by_criterion.get("SEN-BE-01", {})
    level = by_criterion.get("SEN-LEVEL-01", {})
    domain = by_criterion.get("SEN-DOMAIN-01", {})
    qualifying_backend = _supported(backend, "E2")
    qualifying_go = qualifying_backend and bool(
        GO_PATTERN.search(str(backend.get("excerpt") or ""))
    )
    qualifying_logistics = _supported(domain, "E2")
    backend_text = " ".join(
        str(backend.get(field) or "") for field in ("excerpt", "rationale")
    )
    contribution_unclear = "U04_CONTRIBUTION_UNCLEAR" in incoming_codes
    logistics_exception_unclear = current_v9 and (
        domain.get("state") == "conflicting"
        or (qualifying_logistics and domain.get("confidence") == "low")
    )
    language_text = " ".join(
        str(backend.get(field) or "") for field in ("excerpt", "rationale")
    )
    qualifying_transfer = (
        qualifying_backend
        and not LANGUAGE_NEGATION_PATTERN.search(language_text)
        and bool(
            LANGUAGE_TRANSITION_PATTERN.search(language_text)
            or (
                LEARNING_ACTION_PATTERN.search(language_text)
                and DELIVERY_RESULT_PATTERN.search(language_text)
            )
        )
    )
    if current_v10:
        if backend.get("state") == "conflicting" or backend.get("confidence") == "low":
            target_stack = "unclear"
        elif qualifying_go:
            target_stack = "go_present"
        elif qualifying_transfer:
            target_stack = "language_transfer_supported"
        else:
            target_stack = "language_learning_not_evidenced"
    elif (
        backend.get("state") == "conflicting"
        or (not qualifying_go and logistics_exception_unclear)
        or (
            GO_PATTERN.search(backend_text)
            and not qualifying_go
            and (contribution_unclear or backend.get("confidence") == "low")
        )
    ):
        target_stack = "unclear"
    elif qualifying_go:
        target_stack = "go_present"
    elif current_v9 and qualifying_backend and qualifying_logistics:
        target_stack = "logistics_flexible_backend"
    else:
        target_stack = "no_qualifying_go"
        uncertainties = [
            item for item in uncertainties if item["code"] != "U05_TRANSFERABILITY"
        ]

    if not current_v10 and target_stack == "no_qualifying_go":
        uncertainties = [
            item for item in uncertainties if item["code"] == "U11_UNTRUSTED_CONTENT"
        ]
    else:
        uncertainties = [
            item
            for item in uncertainties
            if item["code"] != "U04_CONTRIBUTION_UNCLEAR"
            or any(
                by_criterion.get(criterion, {}).get("confidence") == "low"
                for criterion in advance_minimums
            )
        ]
        uncertainties = [
            item
            for item in uncertainties
            if item["code"] != "U05_TRANSFERABILITY"
            or (
                target_stack == "go_present"
                and any(
                    by_criterion.get(criterion, {}).get("strength") == "E1"
                    for criterion in ("SEN-ARCH-01", "SEN-FE-01", "SEN-DATA-01")
                )
            )
        ]

    priority_profile = {
        "target_stack": target_stack,
        "refactoring_experience": (
            "supported"
            if _supported(level, "E2")
            and REFACTOR_PATTERN.search(
                " ".join(str(level.get(field) or "") for field in ("excerpt", "rationale"))
            )
            else "unclear"
            if level.get("state") == "conflicting"
            else "not_evidenced"
        ),
        "logistics_experience": (
            "supported"
            if _supported(domain, "E2")
            else "unclear"
            if domain.get("state") == "conflicting"
            else "not_evidenced"
        ),
    }
    if current_v10:
        def dimension_state(criterion: str, minimum: str) -> str:
            item = by_criterion.get(criterion, {})
            if item.get("state") == "conflicting" or item.get("confidence") == "low":
                return "unclear"
            return "met" if _supported(item, minimum) else "not_met"

        qualification_dimensions = {
            name: dimension_state(criterion, minimum)
            for name, (criterion, minimum) in V10_DIMENSION_CRITERIA.items()
        }
        qualification_dimensions["language_learning"] = (
            "unclear"
            if target_stack == "unclear"
            else "met"
            if target_stack in {"go_present", "language_transfer_supported"}
            else "not_met"
        )
        priority_profile["valuable_project_experience"] = {
            "met": "supported",
            "not_met": "not_evidenced",
            "unclear": "unclear",
        }[qualification_dimensions["valuable_project"]]
        priority_profile["qualification_dimensions"] = qualification_dimensions
        priority_profile["unmet_requirement_count"] = sum(
            state == "not_met" for state in qualification_dimensions.values()
        )

    decision_ids = (
        {criterion for criterion, _ in V10_DIMENSION_CRITERIA.values()} | {"SEN-BE-01"}
        if current_v10
        else set(advance_minimums) | negative_core
    )
    if current_v9 and target_stack in {
        "logistics_flexible_backend",
        "unclear",
    }:
        decision_ids.add("SEN-DOMAIN-01")
    if any(
        by_criterion.get(criterion, {}).get("confidence") == "low"
        for criterion in decision_ids
    ):
        _add_uncertainty(
            uncertainties,
            _uncertainty(
                "U06_BOUNDARY_CASE",
                "方向性判断依赖低置信度证据",
                "证据归类变化可能改变推进判断",
                "由责任人独立回看相关原文和证据强度",
            ),
        )

    probes = _normalize_probes(payload.get("interview_probes"), by_criterion)
    if target_stack == "unclear" and not uncertainties:
        _add_uncertainty(
            uncertainties,
            _uncertainty(
                "U06_BOUNDARY_CASE",
                "语言匹配、转语言经历或学习证据存在边界",
                "确认语言适配能力后，可能改变四项条件中的不符合数量",
                "由责任人独立回看语言项目、迁移经历和学习交付证据",
            ),
        )

    if current_v10:
        dimensions = priority_profile["qualification_dimensions"]
        uncertain_count = sum(state == "unclear" for state in dimensions.values())
        unmet_count = priority_profile["unmet_requirement_count"]
        threshold_can_change = unmet_count < 2 <= unmet_count + uncertain_count
        if not threshold_can_change:
            uncertainties = [
                item for item in uncertainties if item["code"] == "U11_UNTRUSTED_CONTENT"
            ]

    if uncertainties:
        recommendation = "second_review"
    elif current_v10:
        recommendation = (
            "do_not_advance_pending_human"
            if priority_profile["unmet_requirement_count"] >= 2
            else "advance_pending_human"
        )
    elif target_stack == "no_qualifying_go":
        recommendation = "do_not_advance_pending_human"
    elif all(
        _supported(by_criterion.get(criterion, {}), minimum)
        for criterion, minimum in advance_minimums.items()
    ):
        recommendation = "advance_pending_human"
    else:
        direct_failure = any(
            by_criterion.get(criterion, {}).get("state") == "directly_not_met"
            for criterion in direct_critical | {"SEN-EXP-01", "SEN-ADM-01"}
        )
        core_gaps = sum(
            by_criterion.get(criterion, {}).get("state") == "not_evidenced"
            and by_criterion.get(criterion, {}).get("strength") in {"E0", "E1"}
            for criterion in negative_core
        )
        strong_adjacent = (
            _supported(level, "E3")
            and _supported(by_criterion.get("SEN-ARCH-01", {}), "E2")
            and _supported(by_criterion.get("SEN-DATA-01", {}), "E2")
        )
        if direct_failure or (core_gaps >= 2 and not strong_adjacent):
            recommendation = "do_not_advance_pending_human"
        else:
            _add_uncertainty(
                uncertainties,
                _uncertainty(
                    "U06_BOUNDARY_CASE",
                    "核心证据尚未达到直接推进或明确否定门槛",
                    "补充证据可能改变当前筛选方向",
                    "由责任人独立复核核心证据，必要时在技术面追问",
                ),
            )
            recommendation = "second_review"

    if current_v10 and recommendation == "advance_pending_human":
        for name, (criterion, _) in V10_DIMENSION_CRITERIA.items():
            if priority_profile["qualification_dimensions"][name] == "not_met":
                _ensure_probe(probes, criterion)
        if priority_profile["qualification_dimensions"]["language_learning"] == "not_met":
            _ensure_probe(probes, "SEN-BE-01")
    if recommendation == "advance_pending_human" and not _supported(
        by_criterion.get("SEN-AI-01", {}), "E1"
    ):
        _ensure_probe(probes, "SEN-AI-01")
    if (
        (current_v8_or_v9 or current_v10)
        and recommendation == "advance_pending_human"
        and not _supported(by_criterion.get("SEN-FE-01", {}), "E2")
    ):
        _ensure_probe(probes, "SEN-FE-01")

    strongest = [
        _trim(f"{item.get('criterion_id')}：{item.get('rationale')}", 120)
        for item in sorted(
            (
                item
                for item in evidence
                if isinstance(item, dict) and _supported(item, "E2")
            ),
            key=lambda item: STRENGTH_RANK.get(item.get("strength"), -1),
            reverse=True,
        )[:3]
    ]
    gaps: list[str] = []
    for text in [
        *(item["description"] for item in uncertainties),
        *(
            _trim(f"{item.get('criterion_id')}：{item.get('rationale')}", 120)
            for item in evidence
            if isinstance(item, dict)
            and item.get("state") in {"not_evidenced", "directly_not_met"}
        ),
    ]:
        value = _trim(text, 120)
        if value and value not in gaps:
            gaps.append(value)
        if len(gaps) == 3:
            break

    if recommendation == "second_review":
        rationale = "存在决策相关不确定性，需按原因码完成人工二审后再决定。"
        next_action = "回看原始简历并按原因码完成二审。"
    elif recommendation == "advance_pending_human":
        if current_v10:
            rationale = f"四项新筛选条件中有 {priority_profile['unmet_requirement_count']} 项不符合，未达到两项暂不推进阈值；学历满足但无物流经验时，高含金量项目可保留推进资格。"
        elif target_stack == "logistics_flexible_backend":
            rationale = "物流背景与非 Go 后端项目证据满足放宽路径，资深全栈核心证据达到推进标准，结论待人工一审核验。"
        else:
            rationale = "Go 硬门槛及资深全栈核心证据达到推进标准，结论待人工一审核验。"
        next_action = "由招聘责任人核对原文位置和证据强度并完成人工一审。"
    else:
        if current_v10:
            rationale = f"学历、物流经验、高含金量项目、语言转换与学习证据四项中有 {priority_profile['unmet_requirement_count']} 项不符合，达到暂不推进阈值，结论待人工一审核验。"
        elif target_stack == "no_qualifying_go":
            rationale = "Go 硬门槛或核心证据未达到岗位要求，且未命中物流背景放宽路径，结论待人工一审核验。"
        else:
            rationale = "核心证据未达到岗位要求，结论待人工一审核验。"
        next_action = "由招聘责任人核对硬门槛和关键缺口并完成人工一审。"

    record: dict[str, Any] = {
        "schema_version": "1.2",
        "screening_record_id": screening_record_id,
        "candidate_id": candidate_id,
        "role": "senior-fullstack-engineer",
        "jd_version": jd_version,
        "rubric_version": rubric_version,
        "screening_status": "non_final",
        "model_recommendation": recommendation,
        "recommendation_rationale": rationale,
        "priority_profile": priority_profile,
        "recruiter_summary": {
            "strongest_matches": strongest,
            "critical_gaps": gaps,
            "human_next_action": next_action,
        },
        "evidence": evidence,
        "uncertainties": uncertainties,
        "interview_probes": probes,
        "sensitive_attributes_used": False,
        "human_review": _review(uncertainties),
        "automation_actions": [],
    }
    if candidate_name:
        record["candidate_name"] = candidate_name
    return record

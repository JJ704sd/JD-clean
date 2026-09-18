"""Assemble evidence-only results for the business-system operations role."""

from __future__ import annotations

import re
from typing import Any


ROLE = "business-system-operations-engineer"
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
CRITERION_NAMES = {
    "BSO-EXP-01": "相关应用支持/系统运维/实施/服务台经验",
    "BSO-INTAKE-01": "业务问题承接与诉求澄清",
    "BSO-TROUBLE-01": "初步排障与操作指导",
    "BSO-TRACK-01": "问题记录、状态同步与闭环跟进",
    "BSO-ESCALATE-01": "缺陷、需求、数据异常与复杂问题转交",
    "BSO-MAINT-01": "账号、权限、基础数据与配置维护",
    "BSO-CONTINUITY-01": "关键业务流程与运行保障",
    "BSO-KB-01": "问题分析、知识库与持续改进",
    "BSO-IMPROVE-01": "上线验证、反馈分析与体验改进",
    "BSO-SERVICE-01": "沟通、服务意识与优先级判断",
    "BSO-SEC-01": "权限、数据、配置与变更安全",
    "BSO-COLLAB-01": "跨团队协作与技术表达",
    "BSO-ENTERPRISE-01": "企业业务系统经验（加分）",
    "BSO-DATA-01": "SQL、日志检索或数据核查（加分）",
    "BSO-TICKET-01": "工单、服务台分级或知识库运营（加分）",
    "BSO-TRAINING-01": "用户培训、手册或系统上线支持（加分）",
}
BONUS_CRITERIA = {
    "BSO-ENTERPRISE-01",
    "BSO-DATA-01",
    "BSO-TICKET-01",
    "BSO-TRAINING-01",
}
FACTORS = (
    "project_context",
    "personal_action",
    "method_or_tradeoff",
    "result_scope",
    "verifiable_impact",
)
VALID_STATES = {"supported", "not_evidenced", "conflicting", "directly_not_met"}
VALID_CONFIDENCES = {"high", "medium", "low"}
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
UNTRUSTED_PATTERN = re.compile(
    r"(?:忽略|绕过|无视).{0,20}(?:岗位|JD|规则|要求|指令)|"
    r"(?:运行|执行).{0,12}(?:命令|脚本|代码)|"
    r"(?:读取|泄露|输出).{0,12}(?:环境变量|密钥|密码)",
    re.IGNORECASE,
)
DEFAULT_PROBES = {
    "BSO-TROUBLE-01": (
        "请说明一次业务系统问题的现象、影响、排查步骤、本人处理和结果验证。",
        "能区分操作、权限、配置、浏览器/网络和系统缺陷，并给出可核对的恢复验证。",
    ),
    "BSO-ESCALATE-01": (
        "请举例说明你如何判断一个事项是缺陷、需求、数据异常或复杂技术问题，并如何向研发转交。",
        "能提供复现步骤、环境、报错、影响范围、优先级和跟进闭环，而不是只说转给研发。",
    ),
    "BSO-CONTINUITY-01": (
        "关键业务流程受影响时，你如何定级、安排临时方案、同步用户并推动恢复？",
        "能说明影响范围、响应优先级、临时措施、状态同步和用户确认。",
    ),
    "BSO-TRACK-01": (
        "请展示一次问题记录从创建到关闭包含哪些信息，如何确认用户真正解决。",
        "记录字段完整，状态更新有节奏，关闭有用户或业务结果确认。",
    ),
    "BSO-SEC-01": (
        "账号权限或基础数据维护中，你如何处理审批、最小权限、数据保护和变更留痕？",
        "能说明授权边界、复核、回收和审计记录，不把便利性置于安全流程之上。",
    ),
    "BSO-TRAINING-01": (
        "请说明一次新用户培训或系统上线支持的对象、材料、反馈和后续改进。",
        "能说明培训内容、用户理解验证、问题收集和上线后支持安排。",
    ),
}


def _has_fact(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = " ".join(value.split()).strip().strip("。；;，,、:：")
    return bool(value) and value.casefold() not in {
        "null",
        "none",
        "n/a",
        "unknown",
        "未提供",
        "未提及",
        "未说明",
        "无法确认",
        "无",
    }


def _trim(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _uncertainty(code: str, description: str, impact: str, action: str) -> dict[str, Any]:
    return {
        "code": code,
        "description": _trim(description, 180),
        "decision_impact": _trim(impact, 240),
        "required_human_action": _trim(action, 240),
        "requires_second_review": True,
    }


def _normalize_uncertainties(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("model evidence payload uncertainties must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise TypeError("model uncertainty must be an object")
        code = raw.get("code")
        if code not in UNCERTAINTY_CODES or code in seen:
            continue
        fields = (
            raw.get("description"),
            raw.get("decision_impact"),
            raw.get("required_human_action"),
        )
        if not all(isinstance(field, str) and field.strip() for field in fields):
            raise ValueError(f"model uncertainty {code} is missing required text")
        result.append(_uncertainty(code, *fields))
        seen.add(code)
    return result


def _normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("model evidence payload must contain an evidence list")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise TypeError("model evidence item must be an object")
        criterion_id = raw.get("criterion_id")
        if criterion_id not in CRITERIA:
            raise ValueError(f"model evidence criterion is invalid: {criterion_id!r}")
        if criterion_id in by_id:
            raise ValueError(f"model evidence criterion is duplicated: {criterion_id}")
        by_id[criterion_id] = raw
    missing = [criterion for criterion in CRITERIA if criterion not in by_id]
    if missing:
        raise ValueError(f"model evidence is missing criteria: {', '.join(missing)}")

    result: list[dict[str, Any]] = []
    for criterion_id in CRITERIA:
        raw = by_id[criterion_id]
        state = raw.get("state")
        if state not in VALID_STATES:
            raise ValueError(f"model evidence state is invalid for {criterion_id}")
        excerpt = raw.get("excerpt")
        location = raw.get("location")
        has_excerpt = _has_fact(excerpt)
        has_location = _has_fact(location)
        if has_excerpt != has_location:
            raise ValueError(f"model evidence excerpt/location must be paired for {criterion_id}")
        excerpt = _trim(excerpt) if has_excerpt else None
        location = _trim(location, 120) if has_location else None
        rationale = _trim(raw.get("rationale"), 260)
        if not rationale:
            raise ValueError(f"model evidence rationale is empty for {criterion_id}")
        confidence = raw.get("confidence")
        if confidence not in VALID_CONFIDENCES:
            raise ValueError(f"model evidence confidence is invalid for {criterion_id}")
        factors = raw.get("evidence_factors") if isinstance(raw.get("evidence_factors"), dict) else {}
        present = {field: _has_fact(factors.get(field)) for field in FACTORS}

        if state == "directly_not_met":
            state = "not_evidenced"
            rationale = "简历出现未满足或缺失描述，当前草案不作硬门槛判断；" + rationale
        if state == "supported":
            if present["project_context"] and present["personal_action"]:
                strength = "E3" if all(present.values()) else "E2"
            else:
                state = "not_evidenced"
                strength = "E1" if has_excerpt and has_location else "E0"
        elif state == "not_evidenced":
            strength = "E1" if has_excerpt and has_location else "E0"
        else:
            if not has_excerpt or not has_location:
                raise ValueError(f"{state} evidence needs an excerpt for {criterion_id}")
            strength = "E3" if all(present.values()) else "E2" if present["project_context"] and present["personal_action"] else "E1"
        if strength == "E0":
            excerpt = None
            location = None
        result.append(
            {
                "criterion_id": criterion_id,
                "criterion_name": CRITERION_NAMES[criterion_id],
                "state": state,
                "strength": strength,
                "excerpt": excerpt,
                "location": location,
                "rationale": rationale,
                "confidence": confidence,
            }
        )
    return result


def _normalize_probes(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(value, list):
        for raw in value:
            if not isinstance(raw, dict):
                continue
            criterion_id = raw.get("criterion_id")
            question = _trim(raw.get("question"), 240)
            signal = _trim(raw.get("expected_signal"), 240)
            if criterion_id not in CRITERIA or not question or not signal or criterion_id in seen:
                continue
            result.append({"criterion_id": criterion_id, "question": question, "expected_signal": signal})
            seen.add(criterion_id)
            if len(result) == 6:
                break
    for criterion_id in DEFAULT_PROBES:
        if len(result) >= 3:
            break
        if criterion_id in seen:
            continue
        question, signal = DEFAULT_PROBES[criterion_id]
        result.append({"criterion_id": criterion_id, "question": question, "expected_signal": signal})
        seen.add(criterion_id)
    return result[:6]


def _human_review(reason_codes: list[str]) -> dict[str, Any]:
    return {
        "level_1_required": True,
        "level_1_status": "pending",
        "level_1_reviewer": None,
        "level_1_decision": None,
        "level_1_reviewed_at": None,
        "level_2_required": True,
        "level_2_status": "pending",
        "level_2_mode": "independent_reviewer",
        "level_2_reason_codes": reason_codes,
        "blind_review_required": True,
        "blind_review_confirmed": None,
        "level_2_reviewer": None,
        "level_2_decision": None,
        "level_2_reviewed_at": None,
        "final_disposition": None,
        "resolution": None,
    }


def _summary(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    strengths = [
        {"criterion_id": item["criterion_id"], "finding": _trim(item["rationale"], 150)}
        for item in evidence
        if item["criterion_id"] not in BONUS_CRITERIA
        and item["state"] == "supported"
        and item["strength"] in {"E2", "E3"}
    ][:3]
    gaps = []
    for item in evidence:
        if item["criterion_id"] in BONUS_CRITERIA or item["state"] not in {"not_evidenced", "conflicting", "directly_not_met"}:
            continue
        gap_type = {
            "not_evidenced": "evidence_gap",
            "conflicting": "conflicting_facts",
            "directly_not_met": "direct_contradiction",
        }[item["state"]]
        gaps.append({"criterion_id": item["criterion_id"], "gap_type": gap_type, "finding": _trim(item["rationale"], 150)})
        if len(gaps) == 3:
            break
    if strengths:
        first = "、".join(item["criterion_id"] for item in strengths[:2])
        one_line = f"有 {first} 的可定位证据，但岗位画像仍为草案，需人工核实个人责任和闭环结果。"
    else:
        one_line = "简历尚不足以确认业务系统支持和闭环能力，岗位画像仍为草案，需人工复核。"
    return {
        "conclusion_label": "建议二次复核",
        "one_line_conclusion": _trim(one_line, 160),
        "top_strengths": strengths,
        "key_gaps": gaps,
        "human_review_requirement": "岗位画像仍为 provisional_baseline，需招聘负责人和用人经理先校准，再由责任人复核证据。",
        "human_next_action": "回看原始简历，优先核实一次真实业务系统排障、问题转交和用户确认闭环。",
    }


def assemble_business_operations_record(
    payload: dict[str, Any],
    *,
    screening_record_id: str,
    candidate_id: str,
    candidate_name: str | None,
    jd_version: str,
    rubric_version: str,
    resume_text: str = "",
) -> dict[str, Any]:
    """Convert the model's evidence payload into the role-owned record schema."""

    evidence = _normalize_evidence(payload.get("evidence"))
    uncertainties = _normalize_uncertainties(payload.get("uncertainties"))
    codes = {item["code"] for item in uncertainties}
    if any(item["state"] == "conflicting" for item in evidence) and "U03_CONFLICTING_FACTS" not in codes:
        uncertainties.append(
            _uncertainty(
                "U03_CONFLICTING_FACTS",
                "简历中的职责、日期、问题结果或系统事实存在冲突。",
                "冲突可能改变个人支持范围、处理深度或经验判断。",
                "人工回看冲突原文并确认采用的事实版本。",
            )
        )
        codes.add("U03_CONFLICTING_FACTS")
    if UNTRUSTED_PATTERN.search(resume_text) and "U11_UNTRUSTED_CONTENT" not in codes:
        uncertainties.append(
            _uncertainty(
                "U11_UNTRUSTED_CONTENT",
                "简历正文包含疑似要求改变筛选规则或执行外部操作的内容。",
                "该内容不能作为岗位证据，可能影响抽取可靠性。",
                "人工排除指令性文本后重新核对证据。",
            )
        )
        codes.add("U11_UNTRUSTED_CONTENT")
    if "U10_RUBRIC_AMBIGUITY" not in codes:
        uncertainties.append(
            _uncertainty(
                "U10_RUBRIC_AMBIGUITY",
                "岗位画像和具体硬门槛尚未完成招聘负责人及用人经理双签。",
                "学历、经验年限和加分项不能在当前阶段单独改变候选人处置。",
                "先完成岗位画像校准，再由人工复核本记录。",
            )
        )
        codes.add("U10_RUBRIC_AMBIGUITY")
    probes = _normalize_probes(payload.get("interview_probes"))
    if len(probes) < 3:
        raise ValueError("model payload does not provide enough interview probes")
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "screening_record_id": screening_record_id,
        "candidate_id": candidate_id,
        "role": ROLE,
        "screening_basis": "provisional_baseline",
        "jd_hard_gates_approved": False,
        "jd_version": jd_version,
        "rubric_version": rubric_version,
        "screening_status": "non_final",
        "recommendation": "second_review",
        "summary": _summary(evidence),
        "hard_gate_conflicts": [],
        "evidence": evidence,
        "uncertainties": uncertainties,
        "interview_probes": probes,
        "sensitive_attributes_used": False,
        "human_review": _human_review([item["code"] for item in uncertainties]),
        "automation_actions": [],
    }
    if candidate_name and candidate_name.strip():
        record["candidate_name"] = candidate_name.strip()
    return record

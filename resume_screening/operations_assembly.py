"""Assemble the operations/DevOps role's evidence-only model payload."""

from __future__ import annotations

import re
from typing import Any


V5_RUBRIC_VERSION = "operations-devops-rubric-2026-09-14-v5"
CURRENT_RUBRIC_VERSION = "operations-devops-rubric-2026-09-17-v1"
V5_OPS_CRITERIA = (
    "OPS-EDU-01",
    "OPS-EXP-01",
    "OPS-LINUX-01",
    "OPS-ENV-01",
    "OPS-DB-01",
    "OPS-DELIVERY-01",
    "OPS-OBS-01",
    "OPS-SEC-DR-01",
    "OPS-MODEL-01",
    "OPS-RAG-01",
    "OPS-AI-GOV-01",
    "OPS-AUTO-COST-01",
    "OPS-COLLAB-01",
    "OPS-OWN-01",
    "OPS-SCALE-01",
    "OPS-AI-BONUS-01",
    "OPS-OUTSOURCE-01",
)
CURRENT_OPS_CRITERIA = V5_OPS_CRITERIA + (
    "OPS-OFFICE-IT-01",
    "OPS-OFFICE-NET-01",
    "OPS-DOMAIN-01",
)
OPS_CRITERIA = CURRENT_OPS_CRITERIA

OPS_CRITERION_NAMES = {
    "OPS-EDU-01": "学历与专业",
    "OPS-EXP-01": "相关经验年限",
    "OPS-LINUX-01": "Linux 与网络诊断",
    "OPS-ENV-01": "环境、资源与云平台",
    "OPS-DB-01": "PostgreSQL、Redis 与消息队列",
    "OPS-DELIVERY-01": "容器、CI/CD 与变更",
    "OPS-OBS-01": "可观测、SLO 与事故闭环",
    "OPS-SEC-DR-01": "安全、凭据、审计与灾备",
    "OPS-MODEL-01": "模型 API 与网关运维",
    "OPS-RAG-01": "向量检索、RAG 与 Agent 运行链路",
    "OPS-AI-GOV-01": "AI 成本、效果和版本变更",
    "OPS-AUTO-COST-01": "自动化、IaC 与资源成本",
    "OPS-COLLAB-01": "协作、文档与复盘",
    "OPS-OWN-01": "项目独立承担与核心责任",
    "OPS-SCALE-01": "运维项目复杂度与业务/用户规模",
    "OPS-AI-BONUS-01": "AI 深度使用与 AI 产品经验",
    "OPS-OUTSOURCE-01": "外包/派遣经历排除信号（非计分）",
    "OPS-OFFICE-IT-01": "公司电脑与办公 IT 管理",
    "OPS-OFFICE-NET-01": "办公网络与 VPN 管理",
    "OPS-DOMAIN-01": "物流、供应链或跨境行业经验",
}

E3_FACTORS = (
    "project_context",
    "personal_action",
    "method_or_tradeoff",
    "result_scope",
    "verifiable_impact",
)
STRENGTH_RANK = {"E0": 0, "E1": 1, "E2": 2, "E3": 3}
VALID_STATES = {"supported", "not_evidenced", "conflicting", "directly_not_met"}
VALID_CONFIDENCES = {"high", "medium", "low"}
VALID_UNCERTAINTIES = {
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
UNTRUSTED_INSTRUCTION_PATTERN = re.compile(
    r"(?:忽略|绕过|无视).{0,20}(?:岗位|JD|规则|要求|指令)|"
    r"(?:运行|执行).{0,12}(?:命令|脚本|代码)|"
    r"(?:读取|泄露|输出).{0,12}(?:环境变量|密钥|密码)",
    re.IGNORECASE,
)

OPS_PROBES = {
    "OPS-EXP-01": (
        "请按任职时间说明哪些工作属于运维、DevOps、SRE 或平台工程职责，并解释并行时段如何计算。",
        "日期与职责边界清晰，重叠时段不重复累计。",
    ),
    "OPS-LINUX-01": (
        "请说明一次 Linux 或网络线上故障的指标、定位步骤、本人处置和验证结果。",
        "能给出故障基线、诊断依据、恢复动作和可核验结果。",
    ),
    "OPS-DB-01": (
        "PostgreSQL、Redis 或消息队列变更中的备份恢复验证、停机窗口和回退条件分别是什么？",
        "能说明切换前后校验、恢复路径和个人责任。",
    ),
    "OPS-OBS-01": (
        "请说明一次告警或事故复盘如何定义指标、响应分级并关闭后续行动。",
        "能说明 SLO/SLI 或等价口径、响应记录和闭环证据。",
    ),
    "OPS-MODEL-01": (
        "模型 API 或网关出现超时、限流或供应商故障时，你负责哪些降级、切换、追踪和密钥处理？",
        "能说明线上调用链治理、失败处置和验证结果。",
    ),
    "OPS-RAG-01": (
        "如果 RAG 检索质量突然下降，你会如何区分索引更新、向量召回、Prompt 变化和模型供应商问题？",
        "有分层追踪、回归集、指标和人工兜底思路。",
    ),
    "OPS-OWN-01": (
        "请说明一个你独立承担或作为核心成员负责的运维项目、关键决策、风险/回滚和验收结果。",
        "能清楚区分本人责任与团队成果，并说明取舍和结果。",
    ),
    "OPS-OFFICE-IT-01": (
        "请说明一次公司电脑或办公 IT 设备的配置、故障排查、备份恢复、重装或入离职交接，以及你本人负责的范围。",
        "能说明设备台账或交接边界、系统处理步骤、数据保护和验证结果。",
    ),
    "OPS-OFFICE-NET-01": (
        "请说明一次办公网络或 VPN 的搭建、权限配置或连接故障处理，以及你如何验证安全性和稳定性。",
        "能说明网络/远程接入拓扑、账号权限、定位步骤和恢复验证。",
    ),
}


def _has_fact(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split()).strip().strip("。；;，,、:：")
    return bool(normalized) and normalized.casefold() not in {
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
        "无法确认",
    }


def _trim(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _uncertainty(
    code: str, description: str, decision_impact: str, action: str
) -> dict[str, Any]:
    return {
        "code": code,
        "description": _trim(description, 180),
        "decision_impact": _trim(decision_impact, 240),
        "required_human_action": _trim(action, 240),
        "requires_second_review": True,
    }


def _add_uncertainty(
    items: list[dict[str, Any]], item: dict[str, Any]
) -> None:
    if item["code"] not in {entry.get("code") for entry in items}:
        items.append(item)


def _normalize_uncertainties(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("model evidence payload uncertainties must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise TypeError("model uncertainty must be an object")
        code = raw.get("code")
        if code not in VALID_UNCERTAINTIES:
            raise ValueError(f"model uncertainty code is invalid: {code!r}")
        if code in seen:
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


def _normalize_evidence(
    value: Any,
    *,
    criteria: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("model evidence payload must contain an evidence list")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise TypeError("model evidence item must be an object")
        criterion_id = raw.get("criterion_id")
        if criterion_id not in criteria:
            raise ValueError(f"model evidence criterion is invalid: {criterion_id!r}")
        if criterion_id in by_id:
            raise ValueError(f"model evidence criterion is duplicated: {criterion_id}")
        by_id[criterion_id] = raw
    missing = [criterion for criterion in criteria if criterion not in by_id]
    if missing:
        raise ValueError(f"model evidence is missing criteria: {', '.join(missing)}")

    result: list[dict[str, Any]] = []
    for criterion_id in criteria:
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
        rationale = _trim(raw.get("rationale"), 240)
        if not rationale:
            raise ValueError(f"model evidence rationale is empty for {criterion_id}")
        confidence = raw.get("confidence")
        if confidence not in VALID_CONFIDENCES:
            raise ValueError(f"model evidence confidence is invalid for {criterion_id}")

        factors = raw.get("evidence_factors")
        factors = factors if isinstance(factors, dict) else {}
        present = {field: _has_fact(factors.get(field)) for field in E3_FACTORS}
        if state == "supported":
            if criterion_id == "OPS-EDU-01":
                strength = "E1" if has_excerpt and has_location else "E0"
                if strength == "E0":
                    state = "not_evidenced"
            elif present["project_context"] and present["personal_action"]:
                strength = "E3" if all(present.values()) else "E2"
            else:
                state = "not_evidenced"
                strength = "E1" if has_excerpt and has_location else "E0"
        elif state == "not_evidenced":
            strength = "E1" if has_excerpt and has_location else "E0"
        elif state == "conflicting":
            if not has_excerpt or not has_location:
                raise ValueError(f"conflicting evidence needs an excerpt for {criterion_id}")
            strength = "E3" if all(present.values()) else "E2" if present["project_context"] and present["personal_action"] else "E1"
        else:
            if criterion_id in {"OPS-SCALE-01", "OPS-DOMAIN-01"}:
                state = "not_evidenced"
                strength = "E1" if has_excerpt and has_location else "E0"
            elif not has_excerpt or not has_location:
                raise ValueError(f"direct contradiction needs an excerpt for {criterion_id}")
            else:
                strength = "E3" if all(present.values()) else "E2"

        if strength == "E0":
            excerpt = None
            location = None
        result.append(
            {
                "criterion_id": criterion_id,
                "criterion_name": OPS_CRITERION_NAMES[criterion_id],
                "state": state,
                "strength": strength,
                "excerpt": excerpt,
                "location": location,
                "rationale": rationale,
                "confidence": confidence,
            }
        )
    return result


def _normalize_probes(
    value: Any,
    evidence: list[dict[str, Any]],
    *,
    criteria: tuple[str, ...],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if isinstance(value, list):
        for raw in value:
            if isinstance(raw, dict):
                criterion_id = raw.get("criterion_id")
                question = _trim(raw.get("question"), 220)
                signal = _trim(raw.get("expected_signal"), 220)
            elif isinstance(raw, str):
                criterion_id = "OPS-OBS-01"
                question = _trim(raw, 220)
                signal = "能够给出可核对的个人动作、技术依据和结果。"
            else:
                continue
            if criterion_id not in criteria or not question or not signal:
                continue
            key = (criterion_id, question)
            if key in seen:
                continue
            result.append(
                {
                    "criterion_id": criterion_id,
                    "question": question,
                    "expected_signal": signal,
                }
            )
            seen.add(key)
            if len(result) == 6:
                break

    by_id = {item["criterion_id"]: item for item in evidence}
    preferred = (
        "OPS-LINUX-01",
        "OPS-DB-01",
        "OPS-OBS-01",
        "OPS-MODEL-01",
        "OPS-RAG-01",
        "OPS-OWN-01",
        "OPS-OFFICE-IT-01",
        "OPS-OFFICE-NET-01",
    )
    for criterion_id in preferred:
        if len(result) >= 3:
            break
        if any(item["criterion_id"] == criterion_id for item in result):
            continue
        question, signal = OPS_PROBES[criterion_id]
        result.append(
            {
                "criterion_id": criterion_id,
                "question": question,
                "expected_signal": signal,
            }
        )
    if len(result) < 3:
        for criterion_id, item in by_id.items():
            if len(result) >= 3:
                break
            if any(existing["criterion_id"] == criterion_id for existing in result):
                continue
            result.append(
                {
                    "criterion_id": criterion_id,
                    "question": f"请补充说明“{item['criterion_name']}”中的个人动作、约束和结果。",
                    "expected_signal": "能够给出可核对的个人动作、技术依据和结果。",
                }
            )
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
        "independent_review_preferred": True,
        "independent_review_fallback_reason": None,
        "blind_review_required": True,
        "blind_review_confirmed": None,
        "level_2_reviewer": None,
        "level_2_decision": None,
        "level_2_reviewed_at": None,
        "final_disposition": None,
        "resolution": None,
    }


def _summary(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    strengths: list[dict[str, str]] = []
    for item in sorted(
        evidence,
        key=lambda current: STRENGTH_RANK.get(current.get("strength"), -1),
        reverse=True,
    ):
        if item["criterion_id"] in {"OPS-EDU-01", "OPS-EXP-01", "OPS-OUTSOURCE-01", "OPS-DOMAIN-01"}:
            continue
        if item["state"] == "supported" and item["strength"] in {"E2", "E3"}:
            strengths.append(
                {
                    "criterion_id": item["criterion_id"],
                    "finding": _trim(item["rationale"], 140),
                }
            )
        if len(strengths) == 3:
            break

    gaps: list[dict[str, str]] = []
    gap_types = {
        "not_evidenced": "evidence_gap",
        "conflicting": "conflicting_facts",
        "directly_not_met": "direct_contradiction",
    }
    for item in evidence:
        if item["state"] not in gap_types:
            continue
        if item["criterion_id"] in {"OPS-SCALE-01", "OPS-DOMAIN-01"} and item["state"] == "not_evidenced":
            continue
        gaps.append(
            {
                "criterion_id": item["criterion_id"],
                "gap_type": gap_types[item["state"]],
                "finding": _trim(item["rationale"], 140),
            }
        )
        if len(gaps) == 3:
            break

    if strengths:
        names = "、".join(item["criterion_id"] for item in strengths[:2])
        one_line = f"有 {names} 的可定位证据，但岗位画像尚未完成双角色校准。"
    else:
        one_line = "简历尚不足以确认核心运维能力，且岗位画像尚未完成双角色校准。"
    return {
        "conclusion_label": "建议二次复核",
        "one_line_conclusion": _trim(one_line, 160),
        "top_strengths": strengths,
        "key_gaps": gaps,
        "human_review_requirement": "岗位画像仍为 provisional_baseline，需先完成 JD 校准；之后由招聘责任人复核证据。",
        "human_next_action": "请招聘负责人和用人经理确认岗位画像及 hard gate 口径。",
    }


def assemble_operations_record(
    payload: dict[str, Any],
    *,
    screening_record_id: str,
    candidate_id: str,
    candidate_name: str | None,
    jd_version: str,
    rubric_version: str,
    resume_text: str = "",
) -> dict[str, Any]:
    """Convert an operations/DevOps evidence payload into its rubric schema."""

    criteria = V5_OPS_CRITERIA if rubric_version == V5_RUBRIC_VERSION else CURRENT_OPS_CRITERIA
    evidence = _normalize_evidence(payload.get("evidence"), criteria=criteria)
    uncertainties = _normalize_uncertainties(payload.get("uncertainties"))
    if any(item["state"] == "conflicting" for item in evidence):
        _add_uncertainty(
            uncertainties,
            _uncertainty(
                "U03_CONFLICTING_FACTS",
                "简历中的日期、职责、指标或技术事实存在冲突。",
                "冲突事实可能改变相关经验或个人责任的判断。",
                "由技术负责人回看冲突原文并确认采用的事实版本。",
            ),
        )
    if UNTRUSTED_INSTRUCTION_PATTERN.search(resume_text):
        _add_uncertainty(
            uncertainties,
            _uncertainty(
                "U11_UNTRUSTED_CONTENT",
                "简历正文包含疑似要求改变筛选规则或执行外部操作的内容。",
                "该内容不能作为岗位证据，且可能影响模型抽取可靠性。",
                "人工复核原文并排除指令性内容后再确认证据。",
            ),
        )
    _add_uncertainty(
        uncertainties,
        _uncertainty(
            "U10_RUBRIC_AMBIGUITY",
            "岗位画像及 hard gate 尚未由招聘负责人和用人经理双签批准。",
            "本次只能整理 provisional evidence，不能依据本科或外包规则形成不推进结论。",
            "由招聘负责人和用人经理确认岗位画像、必须项及缺失信息处理规则，并发布新版本。",
        ),
    )
    probes = _normalize_probes(payload.get("interview_probes"), evidence, criteria=criteria)
    return {
        "schema_version": "1.3" if rubric_version == V5_RUBRIC_VERSION else "1.4",
        "screening_record_id": screening_record_id,
        "candidate_id": candidate_id,
        "role": "operations-devops-engineer",
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
        **({"candidate_name": candidate_name} if candidate_name else {}),
    }

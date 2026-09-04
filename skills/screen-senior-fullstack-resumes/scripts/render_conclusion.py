#!/usr/bin/env python3
"""Render validated senior full-stack screening records as concise recruiter conclusions."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROLE_LABEL = "高级全栈工程师"
CRITERION_LABELS = {
    "SEN-EXP-01": "经验与全栈职责",
    "SEN-BE-01": "语言转换与学习交付",
    "SEN-ARCH-01": "BFF/微服务",
    "SEN-FE-01": "前端独立交付",
    "SEN-DATA-01": "数据与中间件",
    "SEN-AI-01": "AI 工程化",
    "SEN-DOMAIN-01": "物流领域",
    "SEN-LEVEL-01": "高含金量项目",
    "SEN-ADM-01": "学历/专业",
}
EVIDENCE_PRIORITY = {
    criterion: index
    for index, criterion in enumerate(
        (
            "SEN-BE-01",
            "SEN-ARCH-01",
            "SEN-DATA-01",
            "SEN-LEVEL-01",
            "SEN-EXP-01",
            "SEN-FE-01",
            "SEN-AI-01",
            "SEN-DOMAIN-01",
            "SEN-ADM-01",
        )
    )
}
STRENGTH_RANK = {"E0": 0, "E1": 1, "E2": 2, "E3": 3}
RECOMMENDATION_LABELS = {
    "advance_pending_human": "建议推进（待人工一审）",
    "second_review": "进入二审（非最终）",
    "do_not_advance_pending_human": "暂不推进（待人工一审）",
}
REVIEW_STATUS_LABELS = {
    "pending": "待完成",
    "completed": "已完成",
    "not_required": "不需要",
}
REVIEW_MODE_LABELS = {
    "source_fact_confirmation": "来源/事实确认",
    "same_owner_separate_pass": "同责任人分时盲审",
    "independent_reviewer": "独立复核",
    "not_required": "不需要",
}
STACK_PRIORITY_LABELS = {
    "go_present": "Go 已满足",
    "logistics_flexible_backend": "物流背景放宽（非 Go 后端）",
    "nodejs_only": "仅 Node.js（优先级较低）",
    "no_qualifying_go_or_nodejs": "不符合 Go/Node.js 主栈门槛",
    "no_qualifying_go": "不符合 Go 硬门槛",
    "language_transfer_supported": "转语言/转栈学习交付成立",
    "language_learning_not_evidenced": "语言转换与学习证据不足",
    "unclear": "Go 门槛待确认",
}


def _load_validator():
    path = Path(__file__).with_name("validate_screening_output.py")
    spec = importlib.util.spec_from_file_location("senior_screening_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


def _clean(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _clip(value: Any, limit: int) -> str:
    text = _clean(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _candidate_name(record: dict[str, Any]) -> str:
    return _clean(record.get("candidate_name") or "姓名未提供")


def _priority_labels(record: dict[str, Any]) -> tuple[str, str]:
    profile = record.get("priority_profile")
    if not isinstance(profile, dict):
        return "旧版记录未分类", "旧版记录未分类"
    stack = STACK_PRIORITY_LABELS.get(profile.get("target_stack"), "主栈分类无效")
    dimensions = profile.get("qualification_dimensions")
    if isinstance(dimensions, dict):
        names = {
            "education": "学历",
            "logistics": "物流",
            "valuable_project": "高含金量项目",
            "language_learning": "语言/学习",
        }
        states = {"met": "满足", "not_met": "不符合", "unclear": "待确认"}
        summary = "；".join(
            f"{names[key]}{states.get(dimensions.get(key), '无效')}" for key in names
        )
        return stack, f"{summary}；不符合 {profile.get('unmet_requirement_count', '?')} 项"
    signals: list[str] = []
    if profile.get("refactoring_experience") == "supported":
        signals.append("重构经验")
    elif profile.get("refactoring_experience") == "unclear":
        signals.append("重构经验待确认")
    if profile.get("logistics_experience") == "supported":
        signals.append("物流行业经验")
    elif profile.get("logistics_experience") == "unclear":
        signals.append("物流行业经验待确认")
    if not signals:
        signals.append("未提供重构或物流行业项目证据")
    return stack, "、".join(signals)


def _top_evidence(record: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    supported = [item for item in record["evidence"] if item.get("state") == "supported"]
    supported.sort(
        key=lambda item: (
            EVIDENCE_PRIORITY.get(item.get("criterion_id"), 999),
            -STRENGTH_RANK.get(item.get("strength"), -1),
        )
    )
    return supported[:limit]


def _gap_lines(record: dict[str, Any], limit: int = 3) -> list[str]:
    gaps: list[str] = []
    for item in record["uncertainties"]:
        gaps.append(f"{_clean(item['description'])}：{_clean(item['decision_impact'])}")
    summary_gaps = record["recruiter_summary"]["critical_gaps"]
    summary_gaps = summary_gaps[min(len(record["uncertainties"]), len(summary_gaps)) :]
    for item in summary_gaps:
        cleaned = _clean(item)
        if cleaned and all(cleaned not in existing and existing not in cleaned for existing in gaps):
            gaps.append(cleaned)
    return gaps[:limit]


def _review_line(record: dict[str, Any]) -> str:
    review = record["human_review"]
    l1 = REVIEW_STATUS_LABELS[review["level_1_status"]]
    if not review["level_2_required"]:
        return f"一审{l1}；二审不需要"
    l2 = REVIEW_STATUS_LABELS[review["level_2_status"]]
    mode = REVIEW_MODE_LABELS[review["level_2_mode"]]
    codes = "/".join(code.split("_", 1)[0] for code in review["level_2_reason_codes"])
    return f"一审{l1}；二审{l2}（{mode}，{codes}）"


def _validate(record: dict[str, Any], allow_human_finalized: bool) -> None:
    errors = VALIDATOR.validate_record(
        record, allow_human_finalized=allow_human_finalized
    )
    if errors:
        raise ValueError("; ".join(errors))


def render_single(
    record: dict[str, Any],
    *,
    allow_human_finalized: bool = False,
    include_summary: bool = True,
) -> str:
    _validate(record, allow_human_finalized)
    name = _candidate_name(record)
    candidate_id = _clean(record["candidate_id"])
    stack_priority, priority_signals = _priority_labels(record)
    lines = [
        f"### 初筛结论｜{name}（{candidate_id}）",
        "",
        "| 项目 | 结论 |",
        "|---|---|",
        f"| 候选人 | {name}（{candidate_id}） |",
        f"| 岗位 | {ROLE_LABEL} |",
        f"| 规则版本 | {_clean(record['rubric_version'])} |",
        f"| 语言路径 | {stack_priority} |",
        f"| 四项筛选 | {priority_signals} |",
        f"| 初筛建议（非最终） | {RECOMMENDATION_LABELS[record['model_recommendation']]} |",
        f"| 核心判断 | {_clip(record['recommendation_rationale'], 160)} |",
        f"| 人工复核 | {_review_line(record)} |",
        "",
        "匹配证据",
        "",
    ]
    evidence = _top_evidence(record)
    if evidence:
        for item in evidence:
            label = CRITERION_LABELS[item["criterion_id"]]
            lines.append(
                f"- {label}：{_clip(item['rationale'], 90)}（{_clip(item['location'], 50)}）"
            )
    else:
        lines.append("- 暂无达到简历阶段支持门槛的证据")

    lines.extend(["", "关键缺口 / 待确认", ""])
    gaps = _gap_lines(record)
    if gaps:
        lines.extend(f"- {_clip(gap, 150)}" for gap in gaps)
    else:
        lines.append("- 无影响当前建议的关键缺口")

    lines.extend(
        [
            "",
            "下一步",
            "",
            f"- {_clip(record['recruiter_summary']['human_next_action'], 180)}",
            "",
            "面试优先验证",
            "",
        ]
    )
    for index, probe in enumerate(record["interview_probes"][:3], start=1):
        lines.append(f"{index}. {_clip(probe['question'], 160)}")
    lines.extend(["", "以上为非最终初筛建议，须由招聘责任人确认。"])
    if include_summary:
        gaps = _gap_lines(record, 1)
        lines.extend(
            [
                "",
                "## 结论汇总表",
                "",
                "| 候选人姓名 | 候选人 ID | 岗位 | 模型建议 | 语言路径 | 四项筛选 | 核心判断 | 关键缺口/待确认 | 人工下一步 |",
                "|---|---|---|---|---|---|---|---|---|",
                "| "
                + " | ".join(
                    (
                        name,
                        candidate_id,
                        ROLE_LABEL,
                        RECOMMENDATION_LABELS[record["model_recommendation"]],
                        stack_priority,
                        priority_signals,
                        _clip(record["recommendation_rationale"], 80),
                        _clip(gaps[0], 70) if gaps else "无关键缺口",
                        _clip(record["recruiter_summary"]["human_next_action"], 70),
                    )
                )
                + " |",
            ]
        )
    return "\n".join(lines)


def render_batch(
    records: list[dict[str, Any]], *, allow_human_finalized: bool = False
) -> str:
    if not records:
        raise ValueError("batch must contain at least one record")
    for record in records:
        _validate(record, allow_human_finalized)
    candidate_ids = [record["candidate_id"] for record in records]
    screening_ids = [record["screening_record_id"] for record in records]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("batch candidate_id values must be unique")
    if len(screening_ids) != len(set(screening_ids)):
        raise ValueError("batch screening_record_id values must be unique")

    counts = {
        key: sum(record["model_recommendation"] == key for record in records)
        for key in RECOMMENDATION_LABELS
    }
    first = records[0]
    lines = [
        "## 批量初筛概览",
        "",
        f"- 岗位：{ROLE_LABEL}",
        f"- 规则版本：{_clean(first['rubric_version'])}",
        f"- 共 {len(records)} 份：建议推进 {counts['advance_pending_human']}，二审 {counts['second_review']}，暂不推进 {counts['do_not_advance_pending_human']}",
        "",
        "| 候选人姓名 | 候选人 ID | 初筛建议 | 语言路径 | 四项筛选 | 核心判断 | 最强证据 | 关键缺口/待确认 | 二审 | 人工下一步 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        stack_priority, priority_signals = _priority_labels(record)
        top = _top_evidence(record, 1)
        strongest = (
            f"{CRITERION_LABELS[top[0]['criterion_id']]}：{_clip(top[0]['rationale'], 45)}"
            if top
            else "暂无充分证据"
        )
        gaps = _gap_lines(record, 1)
        gap = _clip(gaps[0], 55) if gaps else "无关键缺口"
        review = record["human_review"]
        l2 = (
            f"需要（{'/'.join(code.split('_', 1)[0] for code in review['level_2_reason_codes'])}）"
            if review["level_2_required"]
            else "不需要"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    _candidate_name(record),
                    _clean(record["candidate_id"]),
                    RECOMMENDATION_LABELS[record["model_recommendation"]],
                    stack_priority,
                    priority_signals,
                    _clip(record["recommendation_rationale"], 55),
                    strongest,
                    gap,
                    l2,
                    _clip(record["recruiter_summary"]["human_next_action"], 55),
                )
            )
            + " |"
        )

    second_review_records = [
        record for record in records if record["model_recommendation"] == "second_review"
    ]
    lines.extend(["", "## 二审队列", ""])
    if second_review_records:
        for record in second_review_records:
            lines.append(
                render_single(
                    record,
                    allow_human_finalized=allow_human_finalized,
                    include_summary=False,
                )
            )
            lines.append("")
    else:
        lines.append("本批次没有需要二审的记录。")
    return "\n".join(lines).rstrip()


def _read_records(paths: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_path in paths:
        data = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            records.append(data)
        elif isinstance(data, list) and all(isinstance(item, dict) for item in data):
            records.extend(data)
        else:
            raise ValueError(f"{raw_path}: expected a JSON object or an array of objects")
    return records


def main(argv: list[str]) -> int:
    args = argv[1:]
    allow_human_finalized = False
    if args and args[0] == "--allow-human-finalized":
        allow_human_finalized = True
        args = args[1:]
    if not args:
        print(
            "usage: render_conclusion.py [--allow-human-finalized] <record.json> [...]",
            file=sys.stderr,
        )
        return 2
    try:
        records = _read_records(args)
        output = (
            render_single(records[0], allow_human_finalized=allow_human_finalized)
            if len(records) == 1
            else render_batch(records, allow_human_finalized=allow_human_finalized)
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"cannot render conclusion: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

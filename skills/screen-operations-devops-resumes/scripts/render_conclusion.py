#!/usr/bin/env python3
"""Render a validated operations/DevOps screening record as Markdown."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from validate_screening_output import (
    CRITERION_NAMES,
    SUMMARY_LABELS,
    RUBRIC_VERSION,
    read_json,
    validate_record,
)
from scoring import score_record

STATE_LABELS = {
    "supported": "有证据",
    "not_evidenced": "未提供证据",
    "conflicting": "来源冲突",
    "directly_not_met": "简历有明确反向陈述",
}
REVIEW_MODE_LABELS = {
    "independent_reviewer": "独立复核",
    "same_owner_separate_pass": "同一责任人分时盲审",
    "not_required": "不需要二审",
}


def cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", r"\|").replace("\r", " ").replace("\n", " ").strip()


def render(record: dict[str, Any]) -> str:
    name = record.get("candidate_name") or "姓名未提供"
    summary = record["summary"]
    recommendation = record["recommendation"]
    human = record["human_review"]
    review_text = (
        "一审待完成；二审待完成（"
        + REVIEW_MODE_LABELS[human["level_2_mode"]]
        + "；"
        + "、".join(human["level_2_reason_codes"])
        + "）"
        if human["level_2_required"]
        else "一审待完成；当前无需二审"
    )
    basis_label = "已批准" if record["screening_basis"] == "approved_jd" else "草稿/待校准"
    scorecard = score_record(record) if record["rubric_version"] == RUBRIC_VERSION else None
    lines = [
        f"### 初筛结论｜{cell(name)}（{cell(record['candidate_id'])}）",
        "",
        "| 项目 | 结论 |",
        "|---|---|",
        f"| 候选人 | {cell(name)}（{cell(record['candidate_id'])}） |",
        "| 岗位与规则 | 运维开发工程师；"
        + cell(record["jd_version"])
        + "；"
        + cell(record["rubric_version"])
        + f"；{basis_label} |",
        f"| 建议 | {SUMMARY_LABELS[recommendation]} |",
        f"| 核心判断 | {cell(summary['one_line_conclusion'])} |",
        f"| 人工复核 | {cell(review_text)} |",
        "",
    ]
    if scorecard is None:
        lines.extend(["历史 Rubric v1/v2/v3/v4/v5 不按当前权重追溯计算分数。", ""])
    else:
        lines.extend(
            [
                "内部证据覆盖分（非决策指标）",
                "",
                f"- {scorecard['score']}/100，等级 {scorecard['grade']}；评分版本 {scorecard['scoring_version']}。",
                "- 分数不决定推荐状态、hard gate 或复核要求；E1 的有限得分不代表能力达标。",
                "",
                "| 维度 | 权重 | 强度 | 加权得分 |",
                "|---|---:|---|---:|",
            ]
        )
        for item in record["evidence"]:
            criterion_id = item["criterion_id"]
            earned = scorecard["components"][criterion_id]
            lines.append(
                f"| {cell(CRITERION_NAMES[criterion_id])} | "
                f"{scorecard['weights'][criterion_id]}% | {item['strength']} | "
                f"{earned:g}/{scorecard['weights'][criterion_id]} |"
            )
    lines.extend(["", "最强匹配", ""])
    for item in summary["top_strengths"]:
        lines.append(f"- {cell(CRITERION_NAMES[item['criterion_id']])}：{cell(item['finding'])}")
    if not summary["top_strengths"]:
        lines.append("- 暂无可总结的强匹配证据")
    lines.extend(["", "关键缺口 / 待确认", ""])
    for item in summary["key_gaps"]:
        lines.append(f"- {cell(CRITERION_NAMES[item['criterion_id']])}：{cell(item['finding'])}")
    if not summary["key_gaps"]:
        lines.append("- 无影响当前建议的关键缺口")
    lines.extend(["", "下一步", "", f"- {cell(summary['human_next_action'])}", "", "证据矩阵", ""])
    lines.extend(
        [
            "| 维度 | 状态 | 强度 | 证据/理由 | 位置 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in record["evidence"]:
        evidence_text = item["rationale"]
        if item["state"] in {"supported", "conflicting", "directly_not_met"} and item["excerpt"]:
            evidence_text = item["excerpt"] + "；" + item["rationale"]
        lines.append(
            "| "
            + cell(CRITERION_NAMES[item["criterion_id"]])
            + " | "
            + STATE_LABELS[item["state"]]
            + " | "
            + item["strength"]
            + " | "
            + cell(evidence_text)
            + " | "
            + cell(item["location"])
            + " |"
        )
    if record["hard_gate_conflicts"]:
        lines.extend(["", "已批准 hard gate 的直接冲突", ""])
        for item in record["hard_gate_conflicts"]:
            lines.append(
                f"- {cell(CRITERION_NAMES[item['criterion_id']])}："
                f"{cell(item['excerpt'])}（{cell(item['location'])}）"
            )
    if record["uncertainties"]:
        lines.extend(["", "待复核事项", ""])
        for item in record["uncertainties"]:
            lines.append(f"- {item['code']}：{cell(item['description'])}")
    lines.extend(["", "面试优先验证", ""])
    for index, item in enumerate(record["interview_probes"], start=1):
        lines.append(f"{index}. {cell(item['question'])}")
    lines.extend(
        [
            "",
            "| 候选人 | ID | 建议 | 核心判断 | 人工下一步 |",
            "|---|---|---|---|---|",
            f"| {cell(name)} | {cell(record['candidate_id'])} | {SUMMARY_LABELS[recommendation]} | "
            f"{cell(summary['one_line_conclusion'])} | {cell(summary['human_next_action'])} |",
            "",
            "以上为非最终简历初筛建议，须由招聘责任人确认。",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--jd-profile", type=Path)
    args = parser.parse_args(argv)
    try:
        record = read_json(args.record)
        profile = read_json(args.jd_profile) if args.jd_profile else None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    errors = validate_record(record, profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(render(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

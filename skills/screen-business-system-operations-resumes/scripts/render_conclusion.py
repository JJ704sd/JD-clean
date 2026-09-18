#!/usr/bin/env python3
"""Render a validated business-system operations screening record."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scoring import score_record
from validate_screening_output import CRITERION_NAMES, SUMMARY_LABELS, validate_record

STATE_LABELS = {
    "supported": "有证据",
    "not_evidenced": "未提供充分证据",
    "conflicting": "事实冲突",
    "directly_not_met": "存在直接反证（待核实）",
}


def cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def render(record: dict[str, Any]) -> str:
    name = record.get("candidate_name") or "姓名未提供"
    summary = record["summary"]
    score = score_record(record).as_dict()
    lines = [
        f"### 初筛结论｜{cell(name)}（{cell(record['candidate_id'])}）",
        "",
        "| 项目 | 结论 |",
        "|---|---|",
        f"| 岗位与规则 | 业务系统运维工程师；{cell(record['jd_version'])} / {cell(record['rubric_version'])}；岗位画像草案 |",
        f"| 建议 | {cell(SUMMARY_LABELS[record['recommendation']])} |",
        f"| 核心判断 | {cell(summary['one_line_conclusion'])} |",
        f"| 人工复核 | {cell(summary['human_review_requirement'])} |",
        "",
        "内部证据覆盖分（仅供校准）",
        "",
        "| 总分 | 等级 | 评分版本 |",
        "|---:|---|---|",
        f"| {score['score']} | {score['grade']} | {score['scoring_version']} |",
        "",
        "最强匹配",
        "",
    ]
    strengths = summary.get("top_strengths", [])
    if strengths:
        for item in strengths:
            evidence = next(
                current for current in record["evidence"] if current["criterion_id"] == item["criterion_id"]
            )
            lines.append(
                f"- {cell(CRITERION_NAMES[item['criterion_id']])}：{cell(item['finding'])}（{cell(evidence['location'])}）"
            )
    else:
        lines.append("- 暂无可直接确认的核心能力证据")
    lines.extend(["", "关键缺口 / 待确认", ""])
    gaps = summary.get("key_gaps", [])
    if gaps:
        for item in gaps:
            lines.append(f"- {cell(CRITERION_NAMES[item['criterion_id']])}：{cell(item['finding'])}")
    else:
        lines.append("- 暂无影响当前建议的关键缺口")
    lines.extend(["", "下一步", "", f"- {cell(summary['human_next_action'])}", "", "证据矩阵", ""])
    lines.extend([
        "| 维度 | 状态 | 强度 | 证据/理由 | 位置 |",
        "|---|---|---|---|---|",
    ])
    for item in record["evidence"]:
        evidence_text = item["rationale"]
        if item["excerpt"]:
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
    lines.extend(["", "面试优先验证", ""])
    for index, item in enumerate(record["interview_probes"], start=1):
        lines.append(f"{index}. {cell(item['question'])}")
    lines.extend(["", "以上为非最终简历初筛建议，须由招聘责任人确认。"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--jd-profile", type=Path)
    args = parser.parse_args(argv)
    try:
        record = json.loads(args.record.read_text(encoding="utf-8-sig"))
        profile = (
            json.loads(args.jd_profile.read_text(encoding="utf-8-sig"))
            if args.jd_profile
            else None
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    errors = validate_record(record, profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(render(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

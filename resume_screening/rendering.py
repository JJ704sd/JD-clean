"""Render a compact human-readable conclusion from a validated record and scorecard."""

from __future__ import annotations

from typing import Any

ROLE_LABELS = {
    "ai-product-manager": "AI 产品经理",
    "senior-fullstack-engineer": "资深全栈工程师",
    "fullstack-development-intern": "全栈开发实习生",
}
RECOMMENDATION_LABELS = {
    "advance_pending_human": "建议推进（待人工一审）",
    "second_review": "进入二审（非最终）",
    "do_not_advance_pending_human": "暂不推进（待人工确认）",
}


def _recommendation(record: dict[str, Any]) -> str:
    return record.get("model_recommendation", record.get("recommendation", ""))


def _summary(record: dict[str, Any]) -> tuple[str, list[str], list[str], str]:
    if record["role"] == "ai-product-manager":
        summary = record["summary"]
        strengths = [item["finding"] for item in summary.get("top_strengths", [])]
        gaps = [item["finding"] for item in summary.get("key_gaps", [])]
        return summary["one_line_conclusion"], strengths, gaps, summary["next_step"]
    summary = record["recruiter_summary"]
    return (
        record["recommendation_rationale"],
        summary.get("strongest_matches", []),
        summary.get("critical_gaps", []),
        summary["human_next_action"],
    )


def render_conclusion(record: dict[str, Any], scorecard: dict[str, Any]) -> str:
    rationale, strengths, gaps, next_step = _summary(record)
    recommendation = _recommendation(record)
    lines = [
        f"# 简历初筛结论｜{record.get('candidate_name', '姓名未提供')}（{record['candidate_id']}）",
        "",
        "| 项目 | 结果 |",
        "|---|---|",
        f"| 岗位 | {ROLE_LABELS[record['role']]} |",
        f"| 规则版本 | {record['rubric_version']} |",
        f"| 证据匹配分 | {scorecard['score']}/100 |",
        f"| 证据档位 | {scorecard['grade']} |",
        f"| 模型建议 | {RECOMMENDATION_LABELS[recommendation]} |",
        f"| 评级原因 | {' '.join(str(rationale).split())} |",
        "",
        "## 最强匹配",
        "",
    ]
    lines.extend(f"- {item}" for item in strengths[:3])
    if not strengths:
        lines.append("- 暂无达到项目级证据门槛的匹配项")
    lines.extend(("", "## 关键缺口与待确认", ""))
    lines.extend(f"- {item}" for item in gaps[:3])
    if not gaps:
        lines.append("- 无影响当前建议的关键缺口")
    lines.extend(("", "## 人工下一步", "", f"- {next_step}", ""))
    lines.append(
        "以上结果为非最终招聘辅助建议，必须由招聘责任人核对原始简历并完成人工一审。"
    )
    return "\n".join(lines) + "\n"

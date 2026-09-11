#!/usr/bin/env python3
"""Prepare a redacted resume Markdown file with an explicit parser choice."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from resume_screening.cleaning import (  # noqa: E402
    DOCUMENT_PARSERS,
    clean_resume,
)


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="提取、质量检查并脱敏简历，输出供筛选使用的 Markdown。"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-name")
    parser.add_argument(
        "--parser",
        choices=sorted(DOCUMENT_PARSERS),
        default="local",
        help="local 完全本地处理；mineru-flash 会把原文件发送到 MinerU 服务端",
    )
    parser.add_argument(
        "--allow-external-processing",
        action="store_true",
        help="确认允许把当前简历发送到 MinerU；使用 mineru-flash 时必须显式提供",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    args = argument_parser().parse_args(argv)
    if args.parser == "mineru-flash" and not args.allow_external_processing:
        print(
            "错误：MinerU 会把文档发送到外部服务；请在获得授权后添加 "
            "--allow-external-processing。",
            file=sys.stderr,
        )
        return 2
    try:
        result = clean_resume(
            args.source,
            candidate_id=args.candidate_id,
            candidate_name=args.candidate_name,
            document_parser=args.parser,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    destination = args.output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(result.markdown, encoding="utf-8")
    print(
        f"已生成 {destination}；解析器={result.extraction_engine}；"
        f"页数={result.page_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

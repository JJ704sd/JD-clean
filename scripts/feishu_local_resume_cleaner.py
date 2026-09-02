"""Local-only resume cleaner.

This script deliberately has no Feishu API, Base, screening-queue, or model
side effects.  It reuses the repository's existing PDF extraction and
redaction/normalisation pipeline, then saves the structured
``resume.feishu.md`` artifact locally.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Make direct execution from ``scripts\...py`` behave like the existing
# executable entry point, without requiring installation of this repository.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from resume_screening.cleaning import ResumeQualityError
from resume_screening.feishu_monitor import (
    MonitorError,
    SourceChanged,
    now_utc,
    pdf_key,
    prepare_markdown,
    sha256,
)


SCRIPT_VERSION = "feishu-local-resume-cleaner-v1"
DEFAULT_JOB_PREFIX = "全栈工程师_深圳 15-25K"
DEFAULT_SOURCE_DIRECTORY = Path.home() / "Downloads"
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "outputs" / "feishu-local-resumes"
DEFAULT_STATE_PATH = PROJECT_ROOT / "var" / "feishu-local-resume-cleaner" / "state.json"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "outputs" / "feishu-local-resumes" / "local-clean-report.json"
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "var" / "feishu-local-resume-cleaner" / "history.jsonl"


@dataclass(frozen=True)
class LocalCleanerConfig:
    source_directory: Path
    output_directory: Path
    state_path: Path
    report_path: Path
    history_path: Path
    job_prefix: str = DEFAULT_JOB_PREFIX
    today_only: bool = True
    watch: bool = False
    interval_seconds: int = 300
    retry_failed: bool = False


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _default_source_directory() -> Path:
    configured = os.environ.get("FEISHU_PDF_DIR", "").strip()
    return _resolved(Path(configured)) if configured else _resolved(DEFAULT_SOURCE_DIRECTORY)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # A damaged local state must not make the cleaner touch remote systems;
        # starting a fresh local state is the safest recovery.
        return default


def _new_state() -> dict[str, Any]:
    return {"version": 1, "entries": {}, "observations": {}}


def _normalise_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _new_state()
    state = _new_state()
    state["entries"] = value.get("entries") if isinstance(value.get("entries"), dict) else {}
    state["observations"] = (
        value.get("observations") if isinstance(value.get("observations"), dict) else {}
    )
    return state


def _path_key(path: Path) -> str:
    return str(_resolved(path)).casefold()


def _file_signature(path: Path) -> list[int]:
    stat = path.stat()
    return [int(stat.st_size), int(stat.st_mtime_ns)]


def _is_today(path: Path) -> bool:
    return datetime.fromtimestamp(path.stat().st_mtime).date() == date.today()


def _iter_pdf_files(source_directory: Path, today_only: bool) -> list[Path]:
    if not source_directory.is_dir():
        raise MonitorError(f"PDF source directory does not exist: {source_directory}")

    files: list[Path] = []
    for path in source_directory.iterdir():
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            continue
        if today_only and not _is_today(path):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.name.casefold())


def _is_stable(path: Path, state: dict[str, Any], watch: bool) -> bool:
    """Require two identical observations in watch mode before reading a PDF."""

    if not watch:
        return True

    key = _path_key(path)
    signature = _file_signature(path)
    previous = state["observations"].get(key)
    if isinstance(previous, dict) and previous.get("signature") == signature:
        stable_cycles = int(previous.get("stable_cycles", 1)) + 1
    else:
        stable_cycles = 1
    state["observations"][key] = {
        "signature": signature,
        "stable_cycles": stable_cycles,
        "observed_at": now_utc(),
    }
    return stable_cycles >= 2


def _diagnostic(error: BaseException) -> str:
    if isinstance(error, (MonitorError, ResumeQualityError, SourceChanged, OSError)):
        return str(error)[:500]
    return f"{type(error).__name__}: {error}"[:500]


def _success_entry(item: dict[str, Any]) -> dict[str, Any]:
    structured_markdown = item.get("structured_markdown") or item.get("markdown_path", "")
    cleaned_markdown = item.get("cleaned_markdown") or item.get("cleaned_markdown_path", "")
    output_directory = item.get("output_directory", "")
    if not output_directory and structured_markdown:
        output_directory = str(Path(structured_markdown).parent)
    return {
        "status": "success",
        "candidate_id": item["candidate_id"],
        "candidate_name": item["candidate_name"],
        "source_file": item["source_file"],
        "source_sha256": item["source_sha256"],
        "output_directory": output_directory,
        "structured_markdown": structured_markdown,
        "cleaned_markdown": cleaned_markdown,
        "markdown_path": item.get("markdown_path", structured_markdown),
        "cleaned_markdown_path": item.get("cleaned_markdown_path", cleaned_markdown),
        "processed_at": item.get("processed_at", now_utc()),
    }


def run_cycle(config: LocalCleanerConfig) -> dict[str, Any]:
    """Process one local snapshot and persist a local report/state."""

    started_at = now_utc()
    state = _normalise_state(_load_json(config.state_path, _new_state()))
    try:
        pdf_files = _iter_pdf_files(config.source_directory, config.today_only)
    except Exception as error:
        report = {
            "script_version": SCRIPT_VERSION,
            "cycle_status": "input_unavailable",
            "started_at": started_at,
            "finished_at": now_utc(),
            "today_only": config.today_only,
            "job_prefix": config.job_prefix,
            "pdf_total": 0,
            "relevant_pdf_total": 0,
            "items": [],
            "summary": {"input_unavailable": 1},
            "output_directory": str(config.output_directory),
            "state_path": str(config.state_path),
            "report_path": str(config.report_path),
            "history_path": str(config.history_path),
            "error": _diagnostic(error),
            "external_writes": False,
            "model_calls": 0,
        }
        _write_json(config.report_path, report)
        _write_json(config.state_path, state)
        return report

    relevant_files = [path for path in pdf_files if pdf_key(path, config.job_prefix)]
    items: list[dict[str, Any]] = []
    summary = Counter()

    for path in relevant_files:
        candidate_name = pdf_key(path, config.job_prefix)
        item: dict[str, Any] = {
            "source_file": str(_resolved(path)),
            "candidate_name": candidate_name,
            "status": "pending",
        }

        try:
            if not _is_stable(path, state, config.watch):
                item["status"] = "waiting_for_stable_file"
                item["error"] = "watch: file must be unchanged across two polls"
                summary[item["status"]] += 1
                items.append(item)
                continue

            source_sha256 = sha256(path)
            candidate_id = f"feishu-{source_sha256[:12]}"
            item.update({"candidate_id": candidate_id, "source_sha256": source_sha256})

            previous = state["entries"].get(source_sha256)
            if isinstance(previous, dict) and previous.get("status") == "success":
                structured = Path(
                    str(previous.get("structured_markdown") or previous.get("markdown_path", ""))
                )
                if structured.is_file():
                    item.update(previous)
                    item["status"] = "already_processed"
                    summary[item["status"]] += 1
                    items.append(item)
                    continue

            if (
                isinstance(previous, dict)
                and previous.get("status") == "clean_failed"
                and not config.retry_failed
            ):
                item["status"] = "clean_failed"
                item["error"] = str(previous.get("error", "previous cleaning failure"))[:500]
                summary[item["status"]] += 1
                items.append(item)
                continue

            result = prepare_markdown(
                path,
                candidate_id=candidate_id,
                candidate_name=candidate_name,
                output_directory=config.output_directory,
            )
            item.update(result)
            # ``prepare_markdown`` is an existing public helper whose native
            # keys are markdown_path/cleaned_markdown_path.  Keep the local
            # state names explicit while preserving the helper's metadata.
            item["structured_markdown"] = result.get("markdown_path") or result.get(
                "structured_markdown", ""
            )
            item["cleaned_markdown"] = result.get("cleaned_markdown_path") or result.get(
                "cleaned_markdown", ""
            )
            item["status"] = "success"
            item["processed_at"] = now_utc()
            state["entries"][source_sha256] = _success_entry(item)
            summary["success"] += 1
        except Exception as error:
            item["status"] = "clean_failed"
            item["error"] = _diagnostic(error)
            if item.get("source_sha256"):
                state["entries"][item["source_sha256"]] = {
                    "status": "clean_failed",
                    "candidate_id": item.get("candidate_id", ""),
                    "candidate_name": candidate_name,
                    "source_file": item["source_file"],
                    "source_sha256": item["source_sha256"],
                    "error": item["error"],
                    "failed_at": now_utc(),
                }
            summary[item["status"]] += 1
        items.append(item)

    finished_at = now_utc()
    report = {
        "script_version": SCRIPT_VERSION,
        "cycle_status": "completed",
        "started_at": started_at,
        "finished_at": finished_at,
        "today_only": config.today_only,
        "job_prefix": config.job_prefix,
        "pdf_total": len(pdf_files),
        "relevant_pdf_total": len(relevant_files),
        "items": items,
        "summary": dict(summary),
        "output_directory": str(config.output_directory),
        "state_path": str(config.state_path),
        "report_path": str(config.report_path),
        "history_path": str(config.history_path),
        "external_writes": False,
        "feishu_imports": 0,
        "base_writebacks": 0,
        "screening_queue_handoffs": 0,
        "model_calls": 0,
    }
    _write_json(config.report_path, report)
    _write_json(config.state_path, state)
    history_line = json.dumps(
        {
            "finished_at": finished_at,
            "cycle_status": report["cycle_status"],
            "pdf_total": report["pdf_total"],
            "relevant_pdf_total": report["relevant_pdf_total"],
            "summary": report["summary"],
        },
        ensure_ascii=False,
    )
    config.history_path.parent.mkdir(parents=True, exist_ok=True)
    with config.history_path.open("a", encoding="utf-8", newline="\n") as history:
        history.write(history_line + "\n")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local-only PDF resume cleaner; no Feishu, Base, queue, or model calls."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="process one local snapshot (default)")
    mode.add_argument("--watch", action="store_true", help="poll the local directory continuously")
    date_mode = parser.add_mutually_exclusive_group()
    date_mode.add_argument("--today-only", dest="today_only", action="store_true")
    date_mode.add_argument("--all-dates", dest="today_only", action="store_false")
    parser.set_defaults(today_only=True)
    parser.add_argument("--source-dir", type=Path, default=None, help="local PDF directory")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--job-prefix", default=DEFAULT_JOB_PREFIX)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="retry locally failed cleanings; has no remote side effect",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> LocalCleanerConfig:
    if args.interval_seconds < 1:
        raise ValueError("--interval-seconds must be at least 1")
    source = args.source_dir if args.source_dir is not None else _default_source_directory()
    return LocalCleanerConfig(
        source_directory=_resolved(source),
        output_directory=_resolved(args.output_dir),
        state_path=_resolved(args.state_file),
        report_path=_resolved(args.report_file),
        history_path=_resolved(args.history_file),
        job_prefix=args.job_prefix,
        today_only=bool(args.today_only),
        watch=bool(args.watch),
        interval_seconds=args.interval_seconds,
        retry_failed=bool(args.retry_failed),
    )


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = _config_from_args(args)
    except (OSError, ValueError) as error:
        print(json.dumps({"cycle_status": "invalid_configuration", "error": str(error)}, ensure_ascii=False))
        return 2

    if not config.watch:
        report = run_cycle(config)
        print(
            json.dumps(
                {
                    "cycle_status": report["cycle_status"],
                    "pdf_total": report["pdf_total"],
                    "relevant_pdf_total": report["relevant_pdf_total"],
                    "summary": report.get("summary", {}),
                    "external_writes": report["external_writes"],
                    "feishu_imports": report.get("feishu_imports", 0),
                    "base_writebacks": report.get("base_writebacks", 0),
                    "screening_queue_handoffs": report.get("screening_queue_handoffs", 0),
                    "model_calls": report.get("model_calls", 0),
                    "report_path": report["report_path"],
                    "state_path": report["state_path"],
                    "history_path": report["history_path"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["cycle_status"] == "completed" else 1

    try:
        while True:
            report = run_cycle(config)
            print(
                json.dumps(
                    {
                        "cycle_status": report["cycle_status"],
                        "pdf_total": report["pdf_total"],
                        "relevant_pdf_total": report["relevant_pdf_total"],
                        "summary": report.get("summary", {}),
                        "report_path": report["report_path"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(config.interval_seconds)
    except KeyboardInterrupt:
        print("local resume cleaner stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

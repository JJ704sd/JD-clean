"""Publish cleaned local resumes as readable Feishu documents.

The publisher is intentionally narrower than the Base monitor:

    local PDF -> cleaned resume.feishu.md -> one drive +import -> docs readback
    -> optional screening queue handoff -> local Markdown link index

It never reads or writes Base and never calls a model.  With ``--screening``,
read-back-complete documents are handed to the existing local screening queue;
the separate worker owns the MiniMax call, validation, deterministic scoring,
and human-review routing.  ``--dry-run`` is the default.  An apply run makes
exactly one ``drive +import`` attempt per new source hash; an uncertain outcome
is kept as ``import_pending`` and is never replayed automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from resume_screening.cleaning import ResumeQualityError
from resume_screening.feishu_monitor import (
    CliResponse,
    LarkCLI,
    MonitorError,
    SourceChanged,
    find_string,
    find_ticket,
    find_url,
    is_transient,
    markdown_quality_issues,
    now_utc,
    pdf_key,
    prepare_markdown,
    relative_to_root,
    safe_name,
    sha256,
)
from resume_screening.feishu_screening import (
    DEFAULT_SCREENING_MODEL,
    DEFAULT_SCREENING_ROLE,
    ScreeningHandoffError,
    ScreeningQueueBridge,
)
from resume_screening.queue import TaskStore
from resume_screening.versions import ROLE_VERSIONS, contract_matches


SCRIPT_VERSION = "feishu-online-resume-publisher-v3"
STATE_VERSION = 1
DEFAULT_JOB_PREFIX = "全栈工程师_深圳 15-25K"
DEFAULT_SOURCE_DIRECTORY = Path.home() / "Downloads"
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "outputs" / "feishu-online-resumes"
DEFAULT_STATE_PATH = PROJECT_ROOT / "var" / "feishu-online-resume-publisher" / "state.json"
DEFAULT_REPORT_PATH = DEFAULT_OUTPUT_DIRECTORY / "online-publish-report.json"
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "var" / "feishu-online-resume-publisher" / "history.jsonl"
DEFAULT_INDEX_NAME = "resume-index.md"
DEFAULT_SCREENING_DATABASE = PROJECT_ROOT / "var" / "screening-v8.sqlite3"
DEFAULT_SCREENING_OUTPUT_DIRECTORY = PROJECT_ROOT / "outputs"
DEFAULT_SCREENING_MIN_SCORE = 70
SCREENING_INDEX_MODES = ("shortlist", "all-scored")
DEFAULT_SCREENING_INDEX_MODE = "shortlist"


@dataclass(frozen=True)
class PublisherConfig:
    source_directory: Path
    output_directory: Path
    state_path: Path
    report_path: Path
    history_path: Path
    index_path: Path
    folder_token: str | None = None
    cli_executable: str = ""
    job_prefix: str = DEFAULT_JOB_PREFIX
    today_only: bool = True
    dry_run: bool = True
    watch: bool = False
    interval_seconds: int = 300
    task_timeout_seconds: int = 180
    task_poll_seconds: float = 2.0
    screening_enabled: bool = False
    screening_database: Path = DEFAULT_SCREENING_DATABASE
    screening_output_directory: Path = DEFAULT_SCREENING_OUTPUT_DIRECTORY
    screening_role: str = DEFAULT_SCREENING_ROLE
    screening_model: str = DEFAULT_SCREENING_MODEL
    screening_min_score: int = DEFAULT_SCREENING_MIN_SCORE
    screening_index_mode: str = DEFAULT_SCREENING_INDEX_MODE


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _default_source_directory() -> Path:
    configured = os.environ.get("FEISHU_PDF_DIR", "").strip()
    return _resolved(Path(configured)) if configured else _resolved(DEFAULT_SOURCE_DIRECTORY)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _new_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "entries": {},
        "observations": {},
        "last_dry_run_fingerprint": None,
    }


def _normalise_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _new_state()
    state = _new_state()
    state["entries"] = value.get("entries") if isinstance(value.get("entries"), dict) else {}
    state["observations"] = (
        value.get("observations") if isinstance(value.get("observations"), dict) else {}
    )
    state["last_dry_run_fingerprint"] = value.get("last_dry_run_fingerprint")
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
    files = []
    for path in source_directory.iterdir():
        if path.is_file() and path.suffix.casefold() == ".pdf":
            if not today_only or _is_today(path):
                files.append(path)
    return sorted(files, key=lambda item: item.name.casefold())


def _stable_for_cycle(path: Path, state: dict[str, Any], watch: bool) -> bool:
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
        "updated_at": now_utc(),
    }
    return stable_cycles >= 2


def _diagnostic(error: BaseException) -> str:
    if isinstance(error, (MonitorError, ResumeQualityError, SourceChanged, OSError)):
        return str(error)[:500]
    return f"{type(error).__name__}: {error}"[:500]


def _config_fingerprint(config: PublisherConfig) -> str:
    value = {
        "source_directory": str(config.source_directory),
        "output_directory": str(config.output_directory),
        "job_prefix": config.job_prefix,
        "today_only": config.today_only,
        "index_path": str(config.index_path),
        "screening_enabled": config.screening_enabled,
        "screening_database": str(config.screening_database),
        "screening_output_directory": str(config.screening_output_directory),
        "screening_role": config.screening_role,
        "screening_model": config.screening_model,
        "screening_min_score": config.screening_min_score,
        "screening_index_mode": config.screening_index_mode,
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _document_content(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    document = data.get("document") if isinstance(data, dict) else None
    content = document.get("content") if isinstance(document, dict) else None
    return content if isinstance(content, str) else None


class OnlineFeishuImporter:
    """One-shot import plus readback; it never retries ``drive +import``."""

    def __init__(self, config: PublisherConfig, cli: LarkCLI | None = None):
        self.config = config
        self.cli = cli or LarkCLI(config.cli_executable)

    def _fetch_document(self, url: str) -> dict[str, Any]:
        fetched = self.cli.run(
            [
                "docs",
                "+fetch",
                "--doc",
                url,
                "--doc-format",
                "markdown",
                "--as",
                "user",
                "--format",
                "json",
            ],
            timeout=90,
        )
        if not fetched.ok:
            return {
                "status": "import_failed",
                "doc_url": url,
                "readback_nonempty": False,
                "error": "document_readback_failed: " + fetched.diagnostic,
            }
        content = _document_content(fetched.payload)
        if not content or not content.strip():
            return {
                "status": "import_failed",
                "doc_url": url,
                "readback_nonempty": False,
                "error": "document_readback_failed: empty document body",
            }
        issues = markdown_quality_issues(content)
        if issues:
            return {
                "status": "import_failed",
                "doc_url": url,
                "readback_nonempty": False,
                "readback_chars": len(content.strip()),
                "error": "document_readback_failed: " + "; ".join(issues),
            }
        return {
            "status": "success",
            "doc_url": url,
            "readback_nonempty": True,
            "readback_chars": len(content.strip()),
        }

    def _poll_ticket(self, ticket: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.task_timeout_seconds
        while time.monotonic() < deadline:
            response = self.cli.run(
                [
                    "drive",
                    "+task_result",
                    "--scenario",
                    "import",
                    "--ticket",
                    ticket,
                    "--as",
                    "user",
                    "--format",
                    "json",
                ],
                timeout=min(60.0, float(self.config.task_timeout_seconds)),
            )
            url = find_url(response.payload)
            if response.ok and url:
                return self._fetch_document(url)
            if response.ok:
                state = find_string(response.payload, {"status", "state"})
                if state and state.casefold() in {"failed", "error", "canceled", "cancelled"}:
                    return {
                        "status": "import_failed",
                        "ticket": ticket,
                        "error": "import task state: " + state,
                    }
            elif not is_transient(response.diagnostic):
                return {
                    "status": "import_failed",
                    "ticket": ticket,
                    "error": "import task result failed: " + response.diagnostic,
                }
            time.sleep(min(self.config.task_poll_seconds, max(0.1, deadline - time.monotonic())))
        return {
            "status": "import_pending",
            "ticket": ticket,
            "import_outcome_uncertain": True,
            "error": "import task did not return a document URL before timeout; confirm the target folder manually",
        }

    def import_and_readback(self, markdown_path: Path, display_name: str) -> dict[str, Any]:
        args = [
            "drive",
            "+import",
            "--file",
            relative_to_root(markdown_path),
            "--type",
            "docx",
            "--name",
            safe_name(display_name),
            "--as",
            "user",
            "--format",
            "json",
        ]
        if self.config.folder_token:
            args.extend(("--folder-token", self.config.folder_token))

        # This is deliberately the only drive +import invocation for this
        # source hash.  The installed CLI has no idempotency key.
        response = self.cli.run(args, timeout=120)
        url = find_url(response.payload)
        ticket = find_ticket(response.payload)
        if url:
            result = self._fetch_document(url)
            result["ticket"] = ticket
            return result
        if ticket:
            return self._poll_ticket(ticket)
        if not response.ok and is_transient(response.diagnostic):
            return {
                "status": "import_pending",
                "import_outcome_uncertain": True,
                "error": "drive +import transient failure without URL/ticket; confirm the target folder manually: "
                + response.diagnostic,
            }
        if not response.ok:
            return {"status": "import_failed", "error": response.diagnostic}
        return {
            "status": "import_pending",
            "import_outcome_uncertain": True,
            "error": "drive +import returned no URL/ticket; confirm the target folder manually",
        }


def _entry_for_item(item: dict[str, Any], *, status: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    result = result or {}
    return {
        "status": status,
        "source_sha256": item.get("source_sha256"),
        "source_file": item.get("source_file"),
        "candidate_name": item.get("candidate_name"),
        "candidate_id": item.get("candidate_id"),
        "markdown_path": item.get("markdown_path"),
        "cleaned_markdown_path": item.get("cleaned_markdown_path"),
        "doc_url": result.get("doc_url") or item.get("doc_url"),
        "ticket": result.get("ticket") or item.get("ticket"),
        "readback_nonempty": result.get("readback_nonempty", item.get("readback_nonempty")),
        "readback_chars": result.get("readback_chars", item.get("readback_chars")),
        "import_outcome_uncertain": bool(
            result.get("import_outcome_uncertain", item.get("import_outcome_uncertain", False))
        ),
        "screening": result.get("screening") or item.get("screening"),
        "error": result.get("error") or item.get("error"),
        "updated_at": now_utc(),
    }


def _item_from_entry(item: dict[str, Any], entry: dict[str, Any], reason: str) -> dict[str, Any]:
    item.update(
        {
            key: entry.get(key)
            for key in (
                "candidate_id",
                "markdown_path",
                "cleaned_markdown_path",
                "doc_url",
                "ticket",
                "readback_nonempty",
                "readback_chars",
                "import_outcome_uncertain",
                "screening",
            )
            if entry.get(key) is not None
        }
    )
    item["error"] = reason
    return item


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _write_index(path: Path, rows: list[tuple[str, str]], *, dry_run: bool) -> None:
    lines = ["候选人文档 · 在线简历"]
    if rows:
        lines.append("")
        lines.extend(f"• {_markdown_cell(name)}  简历：{url}" for name, url in rows)
    _atomic_write(path, "\n".join(lines) + "\n")


def _valid_screening_score(scorecard: Any) -> int | None:
    if not isinstance(scorecard, dict):
        return None
    value = scorecard.get("score")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        score = value
    elif isinstance(value, float) and value.is_integer():
        score = int(value)
    else:
        return None
    return score if 0 <= score <= 100 else None


def _screened_index_rows(
    config: PublisherConfig,
    published_items: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, str]], str | None]:
    """Return current documents with a completed, valid score.

    The publisher owns the Feishu URL, while the worker owns the score.  Join
    those two local records by source hash; never infer a document URL from a
    candidate ID or treat a queued/manual-review task as a passing result.
    """

    if not published_items or not config.screening_database.is_file():
        return [], None
    try:
        results = TaskStore(config.screening_database).successful_results()
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return [], _diagnostic(error)

    qualified_hashes: set[str] = set()
    for task, envelope in results:
        if task.source_sha256 not in published_items:
            continue
        if task.model != config.screening_model:
            continue
        if not contract_matches(
            role=task.role,
            jd_version=task.jd_version,
            rubric_version=task.rubric_version,
            parser_version=task.parser_version,
            scoring_version=task.scoring_version,
            prompt_version=task.prompt_version,
        ):
            continue
        scorecard = envelope.get("scorecard") if isinstance(envelope, dict) else None
        score = _valid_screening_score(scorecard)
        if score is None:
            continue
        if config.screening_index_mode == "shortlist":
            if score < config.screening_min_score:
                continue
            # A high evidence score must not bypass the role-owned hard gate or
            # a required second review.  The default index is a shortlist, not
            # a final hire decision, so only the Python-derived positive
            # recommendation enters.
            if scorecard.get("review_status") != "advance_pending_human":
                continue
        qualified_hashes.add(task.source_sha256)

    rows: list[tuple[str, str]] = []
    for source_hash, item in published_items.items():
        if source_hash not in qualified_hashes:
            continue
        name = item.get("candidate_name")
        url = item.get("doc_url")
        if isinstance(name, str) and isinstance(url, str) and name.strip() and url.strip():
            rows.append((name, url))
    return rows, None


def _base_item(path: Path, candidate_name: str) -> dict[str, Any]:
    return {
        "file_name": path.name,
        "source_file": str(_resolved(path)),
        "candidate_name": candidate_name,
        "candidate_id": None,
        "source_sha256": None,
        "status": "pending",
        "markdown_path": None,
        "cleaned_markdown_path": None,
        "doc_url": None,
        "ticket": None,
        "readback_nonempty": None,
        "readback_chars": None,
        "import_outcome_uncertain": False,
        "screening": None,
        "error": None,
    }


def _report_shell(config: PublisherConfig, *, started_at: str) -> dict[str, Any]:
    return {
        "script_version": SCRIPT_VERSION,
        "cycle_status": "completed",
        "mode": "watch" if config.watch else "once",
        "dry_run": config.dry_run,
        "started_at": started_at,
        "finished_at": None,
        "source_directory": str(config.source_directory),
        "today_only": config.today_only,
        "job_prefix": config.job_prefix,
        "screening": {
            "enabled": config.screening_enabled,
            "database": str(config.screening_database),
            "output_directory": str(config.screening_output_directory),
            "role": config.screening_role,
            "model": config.screening_model,
            "min_score": config.screening_min_score,
            "index_mode": config.screening_index_mode,
            "index_qualified": 0,
        },
        "preflight": {"ok": False, "external_system": "not consulted"},
        "items": [],
        "summary": {},
        "external_writes": False,
        "feishu_imports": 0,
        "document_readbacks": 0,
        "base_writebacks": 0,
        "screening_queue_handoffs": 0,
        "screening_queue_failures": 0,
        "model_calls": 0,
        "manual_confirmation_required": False,
        "index_path": str(config.index_path),
        "state_path": str(config.state_path),
        "report_path": str(config.report_path),
        "history_path": str(config.history_path),
    }


def _screening_handoff(
    config: PublisherConfig,
    item: dict[str, Any],
    bridge: ScreeningQueueBridge | None,
) -> tuple[dict[str, Any], ScreeningQueueBridge | None]:
    if not config.screening_enabled:
        return {"status": "disabled", "error": "screening handoff is disabled"}, bridge
    if config.dry_run:
        return {
            "status": "blocked",
            "error_code": "DRY_RUN",
            "error": "dry-run does not enqueue AI screening tasks",
        }, bridge
    try:
        if bridge is None:
            bridge = ScreeningQueueBridge(
                database=config.screening_database,
                output_directory=config.screening_output_directory,
                role=config.screening_role,
                model=config.screening_model,
            )
        return (
            bridge.enqueue_after_readback(
                source_path=str(item["source_file"]),
                source_sha256=str(item["source_sha256"]),
                candidate_id=str(item["candidate_id"]),
                candidate_name=item.get("candidate_name"),
                document_url=item.get("doc_url"),
                readback_nonempty=item.get("readback_nonempty"),
                readback_chars=item.get("readback_chars"),
            ),
            bridge,
        )
    except ScreeningHandoffError as error:
        return {
            "status": "failed",
            "error_code": "AI_ACTION_FAILED",
            "error": _diagnostic(error),
        }, bridge


def _save_report_and_history(config: PublisherConfig, report: dict[str, Any]) -> None:
    _write_json(config.report_path, report)
    history_line = {
        "finished_at": report["finished_at"],
        "cycle_status": report["cycle_status"],
        "dry_run": report["dry_run"],
        "summary": report["summary"],
        "screening_queue_handoffs": report["screening_queue_handoffs"],
        "screening_queue_failures": report["screening_queue_failures"],
        "manual_confirmation_required": report["manual_confirmation_required"],
    }
    config.history_path.parent.mkdir(parents=True, exist_ok=True)
    with config.history_path.open("a", encoding="utf-8", newline="\n") as history:
        history.write(json.dumps(history_line, ensure_ascii=False) + "\n")


def run_cycle(config: PublisherConfig, importer: OnlineFeishuImporter | None = None) -> dict[str, Any]:
    started_at = now_utc()
    state = _normalise_state(_load_json(config.state_path, _new_state()))
    report = _report_shell(config, started_at=started_at)
    fingerprint = _config_fingerprint(config)

    if not config.dry_run and state.get("last_dry_run_fingerprint") != fingerprint:
        report["cycle_status"] = "dry_run_gate_required"
        report["summary"] = {"dry_run_gate_required": 1}
        report["error"] = "run the same configuration once with --dry-run before --apply"
        report["finished_at"] = now_utc()
        _write_json(config.state_path, state)
        _save_report_and_history(config, report)
        return report

    try:
        pdf_files = _iter_pdf_files(config.source_directory, config.today_only)
    except Exception as error:
        report["cycle_status"] = "input_unavailable"
        report["preflight"] = {"ok": False, "external_system": "not consulted"}
        report["summary"] = {"input_unavailable": 1}
        report["error"] = _diagnostic(error)
        report["finished_at"] = now_utc()
        _write_json(config.state_path, state)
        _save_report_and_history(config, report)
        return report

    relevant_files = [path for path in pdf_files if pdf_key(path, config.job_prefix)]
    report["preflight"] = {
        "ok": True,
        "external_system": "not consulted" if config.dry_run else "drive/docs only",
        "base_consulted": False,
        "model_consulted": False,
    }
    counts = Counter()
    rows: list[tuple[str, str]] = []
    published_items: dict[str, dict[str, Any]] = {}
    importer = importer or (OnlineFeishuImporter(config) if not config.dry_run else None)
    screening_bridge: ScreeningQueueBridge | None = None

    def record_screening_handoff(item: dict[str, Any]) -> None:
        nonlocal screening_bridge
        if not config.screening_enabled:
            return
        screening, screening_bridge = _screening_handoff(
            config,
            item,
            screening_bridge,
        )
        item["screening"] = screening
        screening_status = screening.get("status")
        if screening_status not in {"disabled", "blocked", "failed"}:
            report["screening_queue_handoffs"] += 1
        elif screening_status == "failed":
            report["screening_queue_failures"] += 1

    for path in relevant_files:
        candidate_name = pdf_key(path, config.job_prefix)
        item = _base_item(path, candidate_name)
        try:
            if not _stable_for_cycle(path, state, config.watch):
                item["status"] = "waiting_for_stable_file"
                item["error"] = "watch: file must be unchanged across two polls"
                counts[item["status"]] += 1
                report["items"].append(item)
                continue

            source_hash = sha256(path)
            item["source_sha256"] = source_hash
            item["candidate_id"] = f"feishu-{source_hash[:12]}"
            previous = state["entries"].get(source_hash)
            previous_status = previous.get("status") if isinstance(previous, dict) else None

            if previous_status == "success":
                if previous.get("doc_url") and previous.get("readback_nonempty"):
                    item = _item_from_entry(item, previous, "existing online document retained")
                    item["status"] = "already_published"
                    rows.append((candidate_name, str(item["doc_url"])))
                    published_items[source_hash] = item
                    record_screening_handoff(item)
                    state["entries"][source_hash] = _entry_for_item(item, status="success")
                    counts[item["status"]] += 1
                    report["items"].append(item)
                    continue
                # A success entry without both proof fields is unsafe to replay.
                item = _item_from_entry(item, previous, "previous outcome lacks URL/readback proof; manual confirmation required")
                item["status"] = "import_pending"
                item["import_outcome_uncertain"] = True
                report["manual_confirmation_required"] = True
                counts[item["status"]] += 1
                report["items"].append(item)
                continue

            if previous_status == "import_pending":
                item = _item_from_entry(item, previous, "previous import_pending retained; confirm target folder manually before any action")
                item["status"] = "import_pending"
                item["import_outcome_uncertain"] = True
                report["manual_confirmation_required"] = True
                counts[item["status"]] += 1
                report["items"].append(item)
                continue

            if previous_status == "import_failed":
                item = _item_from_entry(item, previous, "previous import_failed retained; no automatic re-import")
                item["status"] = "import_failed"
                counts[item["status"]] += 1
                report["items"].append(item)
                continue

            prepared = prepare_markdown(
                path,
                candidate_id=str(item["candidate_id"]),
                candidate_name=candidate_name,
                output_directory=config.output_directory,
            )
            item["markdown_path"] = str(Path(prepared["markdown_path"]).resolve())
            item["cleaned_markdown_path"] = str(Path(prepared["cleaned_markdown_path"]).resolve())
            item["markdown_chars"] = prepared.get("markdown_chars")
            item["page_count"] = prepared.get("page_count")
            item["used_ocr"] = prepared.get("used_ocr")

            if config.dry_run:
                item["status"] = "prepared"
                item["error"] = "dry-run: Feishu import not executed"
                record_screening_handoff(item)
                state["entries"][source_hash] = _entry_for_item(item, status="prepared")
                counts[item["status"]] += 1
                report["items"].append(item)
                continue

            # Persist before the external write.  If the process disappears
            # after Feishu accepts the request, the next cycle cannot replay it.
            state["entries"][source_hash] = _entry_for_item(
                item,
                status="import_pending",
                result={
                    "import_outcome_uncertain": True,
                    "error": "import started; awaiting one drive +import result",
                },
            )
            _write_json(config.state_path, state)
            assert importer is not None
            result = importer.import_and_readback(
                Path(item["markdown_path"]),
                f"{candidate_name}-简历",
            )
            item.update({key: result.get(key) for key in ("doc_url", "ticket", "readback_nonempty", "readback_chars", "import_outcome_uncertain", "error")})
            item["status"] = str(result.get("status") or "import_failed")
            state["entries"][source_hash] = _entry_for_item(item, status=item["status"], result=result)
            if item["status"] == "success" and item.get("doc_url") and item.get("readback_nonempty"):
                report["feishu_imports"] += 1
                report["document_readbacks"] += 1
                report["external_writes"] = True
                rows.append((candidate_name, str(item["doc_url"])))
                published_items[source_hash] = item
                record_screening_handoff(item)
                state["entries"][source_hash] = _entry_for_item(item, status="success")
            if item["status"] == "import_pending":
                report["manual_confirmation_required"] = True
            counts[item["status"]] += 1
        except Exception as error:
            item["status"] = "clean_failed"
            item["error"] = _diagnostic(error)
            if item.get("source_sha256"):
                state["entries"][item["source_sha256"]] = _entry_for_item(item, status="clean_failed")
            counts[item["status"]] += 1
        report["items"].append(item)

    if config.dry_run:
        state["last_dry_run_fingerprint"] = fingerprint
    _write_json(config.state_path, state)
    if config.screening_enabled:
        rows, index_error = _screened_index_rows(config, published_items)
        report["screening"]["index_qualified"] = len(rows)
        if index_error:
            report["screening"]["index_error"] = index_error
    _write_index(config.index_path, rows, dry_run=config.dry_run)
    report["summary"] = {
        "pdf_total": len(pdf_files),
        "relevant_pdf_total": len(relevant_files),
        **dict(counts),
    }
    report["finished_at"] = now_utc()
    _save_report_and_history(config, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish cleaned PDF resumes as Feishu documents and write a local link index."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one cycle (default)")
    mode.add_argument("--watch", action="store_true", help="poll the local PDF directory")
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--dry-run", dest="dry_run", action="store_true", help="clean locally; do not import")
    run_mode.add_argument("--apply", dest="dry_run", action="store_false", help="import to Feishu after a dry-run gate")
    screening_mode = parser.add_mutually_exclusive_group()
    screening_mode.add_argument(
        "--screening",
        dest="screening_enabled",
        action="store_true",
        help="enqueue read-back-complete documents for MiniMax-M3 screening",
    )
    screening_mode.add_argument(
        "--no-screening",
        dest="screening_enabled",
        action="store_false",
        help="do not enqueue documents for AI screening (default)",
    )
    parser.set_defaults(dry_run=True, today_only=True, screening_enabled=False)
    date_mode = parser.add_mutually_exclusive_group()
    date_mode.add_argument("--today-only", dest="today_only", action="store_true")
    date_mode.add_argument("--all-dates", dest="today_only", action="store_false")
    parser.add_argument("--source-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--report-file", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--history-file", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--index-file", type=Path, default=None)
    parser.add_argument("--folder-token", default=None, help="optional; otherwise FEISHU_DOC_FOLDER_TOKEN is used")
    parser.add_argument("--cli", default="", help="optional lark-cli executable")
    parser.add_argument("--job-prefix", default=DEFAULT_JOB_PREFIX)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--task-timeout-seconds", type=int, default=180)
    parser.add_argument("--task-poll-seconds", type=float, default=2.0)
    parser.add_argument("--screening-database", type=Path, default=None)
    parser.add_argument("--screening-output", type=Path, default=None)
    parser.add_argument("--screening-role", choices=sorted(ROLE_VERSIONS), default=None)
    parser.add_argument("--screening-model", default=None)
    parser.add_argument(
        "--screening-min-score",
        type=int,
        default=None,
        help="only show scored resumes at or above this score (0-100)",
    )
    parser.add_argument(
        "--screening-index-mode",
        choices=SCREENING_INDEX_MODES,
        default=None,
        help="shortlist only, or all completed scored resumes for review",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> PublisherConfig:
    if args.interval_seconds < 1 or args.task_timeout_seconds < 1 or args.task_poll_seconds <= 0:
        raise ValueError("interval and task timing values must be positive")
    source = args.source_dir if args.source_dir is not None else _default_source_directory()
    output = _resolved(args.output_dir)
    index = _resolved(args.index_file) if args.index_file is not None else output / DEFAULT_INDEX_NAME
    folder_token = args.folder_token or os.environ.get("FEISHU_DOC_FOLDER_TOKEN", "").strip() or None
    screening_database = _resolved(
        Path(
            args.screening_database
            or os.environ.get("FEISHU_SCREENING_DATABASE", "").strip()
            or DEFAULT_SCREENING_DATABASE
        )
    )
    screening_output_directory = _resolved(
        Path(
            args.screening_output
            or os.environ.get("FEISHU_SCREENING_OUTPUT_DIR", "").strip()
            or DEFAULT_SCREENING_OUTPUT_DIRECTORY
        )
    )
    screening_role = (
        args.screening_role
        or os.environ.get("FEISHU_SCREENING_ROLE", "").strip()
        or DEFAULT_SCREENING_ROLE
    )
    screening_model = (
        args.screening_model
        or os.environ.get("FEISHU_SCREENING_MODEL", "").strip()
        or DEFAULT_SCREENING_MODEL
    )
    configured_min_score = args.screening_min_score
    if configured_min_score is None:
        raw_min_score = os.environ.get("FEISHU_SCREENING_MIN_SCORE", "").strip()
        if raw_min_score:
            try:
                configured_min_score = int(raw_min_score)
            except ValueError as error:
                raise ValueError(
                    "screening minimum score must be an integer from 0 to 100"
                ) from error
        else:
            configured_min_score = DEFAULT_SCREENING_MIN_SCORE
    if not 0 <= configured_min_score <= 100:
        raise ValueError("screening minimum score must be from 0 to 100")
    screening_index_mode = (
        args.screening_index_mode
        or os.environ.get("FEISHU_SCREENING_INDEX_MODE", "").strip()
        or DEFAULT_SCREENING_INDEX_MODE
    )
    if screening_index_mode not in SCREENING_INDEX_MODES:
        raise ValueError(
            "screening index mode must be one of: "
            + ", ".join(SCREENING_INDEX_MODES)
        )
    if args.screening_enabled and screening_role not in ROLE_VERSIONS:
        raise ValueError(f"unsupported screening role: {screening_role!r}")
    if args.screening_enabled and not screening_model.strip():
        raise ValueError("screening model cannot be empty")
    return PublisherConfig(
        source_directory=_resolved(source),
        output_directory=output,
        state_path=_resolved(args.state_file),
        report_path=_resolved(args.report_file),
        history_path=_resolved(args.history_file),
        index_path=index,
        folder_token=folder_token,
        cli_executable=args.cli,
        job_prefix=args.job_prefix,
        today_only=bool(args.today_only),
        dry_run=bool(args.dry_run),
        watch=bool(args.watch),
        interval_seconds=args.interval_seconds,
        task_timeout_seconds=args.task_timeout_seconds,
        task_poll_seconds=args.task_poll_seconds,
        screening_enabled=bool(args.screening_enabled),
        screening_database=screening_database,
        screening_output_directory=screening_output_directory,
        screening_role=screening_role,
        screening_model=screening_model,
        screening_min_score=configured_min_score,
        screening_index_mode=screening_index_mode,
    )


def _print_summary(report: dict[str, Any]) -> None:
    print(
        json.dumps(
            {
                "cycle_status": report["cycle_status"],
                "dry_run": report["dry_run"],
                "pdf_total": report["summary"].get("pdf_total", 0),
                "relevant_pdf_total": report["summary"].get("relevant_pdf_total", 0),
                "summary": report["summary"],
                "manual_confirmation_required": report["manual_confirmation_required"],
                "feishu_imports": report["feishu_imports"],
                "document_readbacks": report["document_readbacks"],
                "base_writebacks": report["base_writebacks"],
                "screening_queue_handoffs": report["screening_queue_handoffs"],
                "screening_queue_failures": report["screening_queue_failures"],
                "model_calls": report["model_calls"],
                "index_path": report["index_path"],
                "report_path": report["report_path"],
                "state_path": report["state_path"],
                "history_path": report["history_path"],
            },
            ensure_ascii=False,
        )
    )


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = _config_from_args(args)
    except ValueError as error:
        print(json.dumps({"cycle_status": "invalid_configuration", "error": str(error)}, ensure_ascii=False))
        return 2

    if not config.watch:
        report = run_cycle(config)
        _print_summary(report)
        return 0 if report["cycle_status"] == "completed" else 1

    try:
        while True:
            report = run_cycle(config)
            _print_summary(report)
            time.sleep(config.interval_seconds)
    except KeyboardInterrupt:
        print("online resume publisher stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

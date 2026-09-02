"""Idempotent PDF-to-Feishu monitoring loop.

The monitor deliberately keeps local parsing and Feishu side effects separate:
every cycle reads the Base schema and complete view first, records a dry-run
preflight gate, and only then can an apply cycle import documents.  Base
writeback is additionally gated on the configured processing columns being
present and type-compatible.  When explicitly enabled, a successful document
readback is handed to the existing local screening queue; this module never
calls the model or makes a business decision itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from .cleaning import (
    PARSER_VERSION,
    ResumeQualityError,
    _strip_opaque_platform_tokens,
    clean_resume,
    redact_for_model,
)
from .feishu_screening import (
    DEFAULT_SCREENING_MODEL,
    DEFAULT_SCREENING_ROLE,
    ScreeningHandoffError,
    ScreeningQueueBridge,
)
from .queue import sanitize_diagnostic
from .versions import ROLE_VERSIONS
from .watch import is_ignored_watch_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_VERSION = "feishu-resume-monitor-v2"
STATE_VERSION = 1
MAX_TRANSIENT_ATTEMPTS = 3
UNCERTAIN_IMPORT_ERROR = (
    "import outcome unresolved: drive +import returned a transient error without "
    "a document URL or task ticket; check the destination folder before retrying"
)
DEFAULT_TABLE_ID = "tblmINC6Tc4YjHlH"
DEFAULT_VIEW_ID = "vewpkLcqHy"
DEFAULT_ROW_KEY_COLUMN = "姓名"
DEFAULT_LINK_COLUMN = "简历文档链接"
DEFAULT_STATUS_COLUMN = "处理状态"
DEFAULT_ERROR_COLUMN = "错误信息"
DEFAULT_PROCESSED_AT_COLUMN = "处理时间"
DEFAULT_SOURCE_HASH_COLUMN = "源 PDF 哈希"
DEFAULT_AI_COLUMN = "岗位匹配度"
DEFAULT_JOB_PREFIX = "全栈工程师_深圳 15-25K"
DEFAULT_SCREENING_DATABASE = PROJECT_ROOT / "var" / "screening-v8.sqlite3"
DEFAULT_SCREENING_OUTPUT_DIRECTORY = PROJECT_ROOT / "outputs"
PDF_KEY_RULE = rf"^【{re.escape(DEFAULT_JOB_PREFIX)}】(?P<name>.+?)\s+(?:10年以上|\d+年)\.pdf$"
OPAQUE_SCAN_RE = re.compile(r"[A-Za-z0-9_-]{40,}~~")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[\w-]+", re.IGNORECASE | re.UNICODE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9](?:[\s-]?\d){9}(?!\d)")
IDENTITY_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)"
)
TEMPORARY_SUFFIXES = {".crdownload", ".part", ".tmp", ".temp", ".swp"}
REQUIRED_HEADINGS = (
    "## 基本信息",
    "## 个人简介",
    "## 教育经历",
    "## 工作经历",
    "## 项目经历",
    "## 技能",
    "## 证书与语言能力",
    "## 其他信息",
)
SECTION_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("个人简介", ("个人简介", "个人优势", "自我评价", "自我介绍", "核心优势", "个人概况")),
    ("教育经历", ("教育经历", "教育背景", "学历背景", "教育情况", "Education")),
    ("工作经历", ("工作经历", "工作经验", "职业经历", "任职经历", "Work Experience")),
    ("项目经历", ("项目经历", "项目经验", "项目实践", "Projects", "项目")),
    ("技能", ("专业技能", "个人技能", "技能特长", "技能", "技术栈", "核心技能", "Skills")),
    ("证书与语言能力", ("证书与语言能力", "资格证书", "证书", "语言能力", "证书/语言", "Certifications")),
    ("其他信息", ("其他信息", "其他", "博客", "开源", "个人作品", "Additional Information")),
]


class MonitorError(RuntimeError):
    """A non-retryable monitor configuration or preflight error."""


class ImportLockBusy(MonitorError):
    """Another process currently owns the same Feishu destination lock."""


class SourceChanged(MonitorError):
    """A PDF changed while it was being hashed or read."""


@dataclass(frozen=True)
class MonitorConfig:
    base_token: str
    table_id: str
    view_id: str
    pdf_directory: Path
    output_directory: Path
    state_path: Path
    report_path: Path
    history_path: Path
    records_path: Path
    lock_path: Path
    row_key_column: str = DEFAULT_ROW_KEY_COLUMN
    job_prefix: str = DEFAULT_JOB_PREFIX
    link_column: str = DEFAULT_LINK_COLUMN
    status_column: str = DEFAULT_STATUS_COLUMN
    error_column: str = DEFAULT_ERROR_COLUMN
    processed_at_column: str = DEFAULT_PROCESSED_AT_COLUMN
    source_hash_column: str = DEFAULT_SOURCE_HASH_COLUMN
    optional_ai_column: str = DEFAULT_AI_COLUMN
    folder_token: str | None = None
    cli_executable: str = ""
    dry_run: bool = True
    watch: bool = False
    interval_seconds: float = 300.0
    task_timeout_seconds: float = 180.0
    task_poll_seconds: float = 2.0
    max_files: int | None = None
    retry_failed: bool = False
    screening_enabled: bool = False
    screening_database: Path = DEFAULT_SCREENING_DATABASE
    screening_output_directory: Path = DEFAULT_SCREENING_OUTPUT_DIRECTORY
    screening_role: str = DEFAULT_SCREENING_ROLE
    screening_model: str = DEFAULT_SCREENING_MODEL

    @property
    def required_columns(self) -> tuple[str, ...]:
        return (
            self.link_column,
            self.status_column,
            self.error_column,
            self.processed_at_column,
            self.source_hash_column,
        )


@dataclass(frozen=True)
class CliResponse:
    returncode: int
    payload: Any
    stderr: str = ""
    stdout: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and isinstance(self.payload, dict) and self.payload.get("ok") is True

    @property
    def diagnostic(self) -> str:
        return cli_error_message(self.payload, self.stderr)


@dataclass
class PreflightContext:
    ok: bool
    report: dict[str, Any]
    records: list[dict[str, Any]]
    by_key: dict[str, list[dict[str, Any]]]
    fields: dict[str, dict[str, Any]]
    fingerprint: str | None
    error: str | None = None

    @property
    def writeback_allowed(self) -> bool:
        return bool(self.report.get("writeback_allowed"))


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def now_local() -> str:
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).strip()


def pdf_key_rule(job_prefix: str = DEFAULT_JOB_PREFIX) -> str:
    return rf"^【{re.escape(job_prefix)}】(?P<name>.+?)\s+(?:10年以上|\d+年)\.pdf$"


def pdf_key(path: Path, job_prefix: str = DEFAULT_JOB_PREFIX) -> str:
    match = re.fullmatch(pdf_key_rule(job_prefix), path.name)
    return unicodedata.normalize("NFKC", match.group("name")).strip() if match else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        before = path.stat()
    except OSError as exc:
        raise SourceChanged(f"cannot stat source: {path.name}") from exc
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        after = path.stat()
    except OSError as exc:
        raise SourceChanged(f"source disappeared during hashing: {path.name}") from exc
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SourceChanged(f"source changed during hashing: {path.name}")
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"state/report JSON cannot be read: {path.name}") from exc


def default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "entries": {}, "observations": {}, "last_dry_run_preflight": None}


def load_state(path: Path) -> dict[str, Any]:
    value = load_json(path, default_state())
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise MonitorError("monitor state version is unsupported; move the state file before restarting")
    if not isinstance(value.get("entries"), dict) or not isinstance(value.get("observations"), dict):
        raise MonitorError("monitor state shape is invalid")
    return value


def normalize_bool(value: str, *, variable: str) -> bool:
    lowered = value.strip().casefold()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{variable} must be true/false")


def env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def safe_name(value: str) -> str:
    return re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip() or "candidate"


def relative_to_root(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError as exc:
        raise MonitorError(f"CLI artifact path must stay under project root: {path}") from exc


def iter_dicts(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def find_string(value: Any, keys: set[str], predicate: Any = None) -> str | None:
    wanted = {item.casefold() for item in keys}
    for key, child in iter_dicts(value):
        if key.casefold() in wanted and isinstance(child, str):
            if predicate is None or predicate(child):
                return child
    return None


def find_url(value: Any) -> str | None:
    keys = {"url", "web_url", "document_url", "doc_url", "file_url", "documentUrl", "webUrl"}
    return find_string(value, keys, lambda item: item.startswith("https://"))


def find_ticket(value: Any) -> str | None:
    return find_string(value, {"ticket", "task_ticket", "taskTicket"})


def cli_error_message(value: Any, stderr: str = "") -> str:
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            parts = [str(error.get(key)) for key in ("type", "subtype", "code", "message") if error.get(key) is not None]
            if parts:
                return sanitize_diagnostic(": ".join(parts))[:500]
        message = value.get("message") or value.get("msg")
        if message:
            return sanitize_diagnostic(str(message))[:500]
    return sanitize_diagnostic(stderr or "CLI returned a non-success response")[:500]


def is_transient(message: str) -> bool:
    lowered = message.casefold()
    return any(
        token in lowered
        for token in (
            "timeout",
            "timed out",
            "rate limit",
            "too many requests",
            "temporarily",
            "connection reset",
            "connection refused",
            "429",
            "500",
            "502",
            "503",
            "504",
            "1254291",
        )
    )


class LarkCLI:
    """Small JSON-only adapter; it never exposes raw CLI output in reports."""

    def __init__(self, executable: str = ""):
        self.executable = executable or ("lark-cli.cmd" if os.name == "nt" else "lark-cli")

    def run(self, args: Sequence[str], *, timeout: float = 90.0) -> CliResponse:
        command = [self.executable, *args]
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            return CliResponse(127, {"ok": False, "error": {"message": "lark-cli not found"}}, "lark-cli not found")
        except subprocess.TimeoutExpired:
            return CliResponse(124, {"ok": False, "error": {"message": "CLI timeout"}}, "CLI timeout")
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        try:
            payload = json.loads(stdout) if stdout else {"ok": False, "error": {"message": stderr or "empty CLI response"}}
        except json.JSONDecodeError:
            payload = {"ok": False, "error": {"message": "non-JSON CLI response"}}
        return CliResponse(result.returncode, payload, stderr, stdout)


class DestinationLock:
    """Cross-process exclusive lock for a single Feishu folder destination."""

    def __init__(self, path: Path, *, blocking: bool = False):
        self.path = path
        self.blocking = blocking
        self.handle: Any = None

    def __enter__(self) -> "DestinationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.seek(0, 2) == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if not self.blocking:
                        raise
                    time.sleep(0.1)
        except (OSError, ImportError) as exc:
            self.handle.close()
            self.handle = None
            raise ImportLockBusy("same-folder import lock is held by another monitor") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MonitorError(f"record artifact cannot be read: {path.name}") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MonitorError(f"record artifact contains invalid JSON: {path.name}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return ",".join(cell_text(item) for item in value if cell_text(item))
    if isinstance(value, dict):
        for key in ("text", "name", "value", "id"):
            if key in value:
                return cell_text(value[key])
    return str(value).strip()


def state_doc_url(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    value = cell_text(entry.get("doc_url")) or cell_text(entry.get("imported_doc_url"))
    return value or None


def field_options(field: dict[str, Any]) -> set[str]:
    options = field.get("options")
    if not isinstance(options, list):
        return set()
    return {cell_text(item.get("name") if isinstance(item, dict) else item) for item in options}


def writeback_diagnostics(config: MonitorConfig, fields: dict[str, dict[str, Any]]) -> list[str]:
    diagnostics: list[str] = []
    expected = {
        config.link_column: {"text", "url"},
        config.status_column: {"text", "select"},
        config.error_column: {"text"},
        config.processed_at_column: {"datetime"},
        config.source_hash_column: {"text"},
    }
    for name, types in expected.items():
        field = fields.get(name)
        if field is None:
            diagnostics.append(f"missing column: {name}")
            continue
        field_type = str(field.get("type") or "").casefold()
        if field_type not in types:
            diagnostics.append(f"unsupported type for {name}: {field_type or 'unknown'}")
        if name == config.status_column and field_type == "select" and "success" not in field_options(field):
            diagnostics.append("status select does not contain success")
    return diagnostics


def build_writeback_updates(
    config: MonitorConfig,
    fields: dict[str, dict[str, Any]],
    *,
    document_url: str,
    source_hash: str,
    processed_at: str,
) -> dict[str, Any]:
    status_field = fields[config.status_column]
    status_value: Any = ["success"] if str(status_field.get("type")).casefold() == "select" else "success"
    return {
        config.link_column: document_url,
        config.status_column: status_value,
        config.error_column: "",
        config.processed_at_column: processed_at,
        config.source_hash_column: source_hash,
    }


def marker_regex() -> re.Pattern[str]:
    labels: list[str] = []
    for _, names in SECTION_PATTERNS:
        labels.extend(re.escape(name) for name in names)
    labels.sort(key=len, reverse=True)
    return re.compile(r"(?im)^\s*(?:#+\s*)?(" + "|".join(labels) + r")\s*$")


def map_marker(label: str) -> str:
    folded = normalize(label).casefold()
    for section, names in SECTION_PATTERNS:
        if any(folded == normalize(name).casefold() for name in names):
            return section
    return "其他信息"


def normalize_body(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        if re.fullmatch(r"\s*##\s*第\s*\d+\s*页\s*", raw):
            continue
        line = _strip_opaque_platform_tokens(raw)
        line = line.replace("\x00", "")
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line or line in {"~", "-", "—", "_"}:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def structured_markdown(cleaned: Any, candidate_name: str) -> str:
    frontmatter = (
        "---\n"
        f"candidate_id: {cleaned.candidate_id}\n"
        f"source_sha256: {cleaned.source_sha256}\n"
        f"parser_version: {cleaned.parser_version}\n"
        f"generated_at: {now_utc()}\n"
        f"used_ocr: {str(cleaned.used_ocr).lower()}\n"
        f"page_count: {cleaned.page_count}\n"
        "document_format: structured_resume_markdown\n"
        "privacy: pii_redacted\n"
        "---\n\n"
    )
    body = cleaned.markdown.split("---\n\n", 1)[-1]
    body = redact_for_model(_strip_opaque_platform_tokens(body), candidate_name=candidate_name)
    body = normalize_body(body)
    markers = list(marker_regex().finditer(body))
    sections: dict[str, list[str]] = {name: [] for name, _ in SECTION_PATTERNS}
    sections["基本信息"] = []
    if markers:
        sections["基本信息"] = [body[: markers[0].start()]]
        for index, marker in enumerate(markers):
            section = map_marker(marker.group(1))
            start = marker.end()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
            sections.setdefault(section, []).append(body[start:end])
    else:
        sections["基本信息"] = [body]
    output: list[str] = [
        "# 简历",
        "",
        "> 本文由本地 PDF 文本层提取/OCR 回退后脱敏整理；未提及信息统一标为“未提及”。",
        "",
    ]
    for section in ("基本信息", "个人简介", "教育经历", "工作经历", "项目经历", "技能", "证书与语言能力", "其他信息"):
        value = normalize_body("\n\n".join(part.strip() for part in sections.get(section, []) if part.strip())) or "未提及"
        output.extend((f"## {section}", "", value, ""))
    return frontmatter + "\n".join(output).rstrip() + "\n"


def markdown_quality_issues(content: str) -> list[str]:
    issues: list[str] = []
    missing = [heading for heading in REQUIRED_HEADINGS if heading not in content]
    if missing:
        issues.append("missing headings: " + ",".join(missing))
    if not content.strip():
        issues.append("empty document")
    if EMAIL_RE.search(content):
        issues.append("raw email detected")
    if PHONE_RE.search(content):
        issues.append("raw phone detected")
    if IDENTITY_RE.search(content):
        issues.append("raw identity number detected")
    if OPAQUE_SCAN_RE.search(content):
        issues.append("opaque platform token detected")
    return issues


def prepare_markdown(path: Path, candidate_id: str, candidate_name: str, output_directory: Path) -> dict[str, Any]:
    cleaned = clean_resume(
        path,
        candidate_id=candidate_id,
        candidate_name=candidate_name,
        ocr=True,
    )
    structured = structured_markdown(cleaned, candidate_name)
    issues = markdown_quality_issues(structured)
    if len(structured.strip()) < 200:
        issues.append("structured Markdown is too short")
    if issues:
        raise MonitorError("; ".join(issues))
    destination = output_directory / candidate_id
    atomic_write(destination / "resume.cleaned.md", cleaned.markdown)
    atomic_write(destination / "resume.feishu.md", structured)
    return {
        "candidate_id": candidate_id,
        "markdown_path": str((destination / "resume.feishu.md").resolve()),
        "cleaned_markdown_path": str((destination / "resume.cleaned.md").resolve()),
        "markdown_chars": len(structured),
        "page_count": cleaned.page_count,
        "used_ocr": cleaned.used_ocr,
        "parser_version": PARSER_VERSION,
    }


def record_snapshot_path(config: MonitorConfig) -> Path:
    return config.records_path.resolve()


def preflight_fingerprint(config: MonitorConfig, fields: dict[str, dict[str, Any]], records: list[dict[str, Any]], missing: list[str], type_errors: list[str]) -> str:
    field_summary = []
    for name, field in sorted(fields.items()):
        field_summary.append(
            {
                "id": field.get("id"),
                "name": name,
                "options": sorted(field_options(field)),
                "type": field.get("type"),
            }
        )
    payload = {
        "table_id": config.table_id,
        "view_id": config.view_id,
        "job_prefix": config.job_prefix,
        "pdf_directory": str(config.pdf_directory),
        "row_key_column": config.row_key_column,
        "required_columns": config.required_columns,
        "screening": {
            "enabled": config.screening_enabled,
            "database": str(config.screening_database),
            "output_directory": str(config.screening_output_directory),
            "role": config.screening_role,
            "model": config.screening_model,
        },
        "fields": field_summary,
        "missing_columns": missing,
        "type_errors": type_errors,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def base_row_duplicate(config: MonitorConfig, row: dict[str, Any], source_hash: str) -> tuple[bool, str, str | None]:
    link = cell_text(row.get(config.link_column))
    if link:
        return True, "existing_link", link
    stored_hash = cell_text(row.get(config.source_hash_column)).casefold()
    if stored_hash and stored_hash == source_hash.casefold():
        return True, "existing_source_hash", None
    return False, "", None


def output_item_base(
    path: Path,
    source_hash: str | None = None,
    *,
    job_prefix: str = DEFAULT_JOB_PREFIX,
) -> dict[str, Any]:
    key = pdf_key(path, job_prefix)
    return {
        "file_name": path.name,
        "source_path": str(path.resolve()),
        "source_sha256": source_hash,
        "pdf_key": key,
        "candidate_name": key or None,
        "candidate_id": f"feishu-{source_hash[:12]}" if source_hash else None,
        "record_id": None,
        "match_count": 0,
        "status": "pending",
        "markdown_path": None,
        "cleaned_markdown_path": None,
        "imported_doc_url": None,
        "import_outcome_uncertain": False,
        "readback_nonempty": None,
        "readback_chars": None,
        "attempts": 0,
        "error": None,
        "writeback": None,
        "screening": None,
    }


def success_entry(item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_sha256": item["source_sha256"],
        "source_path": item["source_path"],
        "file_name": item["file_name"],
        "candidate_name": item.get("candidate_name"),
        "record_id": item.get("record_id"),
        "candidate_id": item.get("candidate_id"),
        "doc_url": result.get("url"),
        "markdown_path": item.get("markdown_path"),
        "cleaned_markdown_path": item.get("cleaned_markdown_path"),
        "status": result.get("status", "success"),
        "attempts": result.get("attempts", 0),
        "transient_attempts": result.get("transient_attempts", 0),
        "ticket": result.get("ticket"),
        "import_outcome_uncertain": bool(result.get("import_outcome_uncertain", False)),
        "readback_chars": result.get("readback_chars"),
        "base_written": bool(result.get("base_written", False)),
        "updated_at": now_utc(),
    }


def item_from_state(item: dict[str, Any], entry: dict[str, Any], *, reason: str) -> dict[str, Any]:
    item["status"] = "already_processed" if entry.get("status") == "success" else str(entry.get("status") or "pending")
    item["error"] = reason
    item["imported_doc_url"] = state_doc_url(entry)
    item["import_outcome_uncertain"] = bool(entry.get("import_outcome_uncertain", False))
    item["markdown_path"] = entry.get("markdown_path")
    item["cleaned_markdown_path"] = entry.get("cleaned_markdown_path")
    item["readback_nonempty"] = entry.get("readback_nonempty")
    item["readback_chars"] = entry.get("readback_chars")
    item["record_id"] = entry.get("record_id") or item.get("record_id")
    item["screening"] = entry.get("screening")
    return item


def unresolved_import_reason(entry: dict[str, Any]) -> str:
    reason = "previous import outcome is unresolved; check the destination folder manually before retrying"
    previous_error = cell_text(entry.get("error"))
    if previous_error:
        reason += "; previous error: " + sanitize_diagnostic(previous_error)
    return reason


def compare_writeback(config: MonitorConfig, values: Sequence[Any], expected: dict[str, Any]) -> bool:
    ordered = [config.link_column, config.status_column, config.error_column, config.processed_at_column, config.source_hash_column]
    if len(values) < len(ordered):
        return False
    actual = dict(zip(ordered, values))
    if cell_text(actual.get(config.link_column)) != expected[config.link_column]:
        return False
    if cell_text(actual.get(config.status_column)).casefold() != "success":
        return False
    if cell_text(actual.get(config.source_hash_column)).casefold() != expected[config.source_hash_column].casefold():
        return False
    if not cell_text(actual.get(config.processed_at_column)):
        return False
    return not cell_text(actual.get(config.error_column))


def ignored_field_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() == "ignored_fields":
                if isinstance(child, list):
                    names.update(cell_text(item) for item in child)
                elif child:
                    names.add(cell_text(child))
            names.update(ignored_field_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(ignored_field_names(child))
    return {name for name in names if name}


def build_config(args: argparse.Namespace) -> MonitorConfig:
    dry_run = args.dry_run
    if dry_run is None:
        raw = env_first("DRY_RUN")
        dry_run = normalize_bool(raw, variable="DRY_RUN") if raw is not None else True
    screening_flag = getattr(args, "screening_enabled", None)
    if screening_flag is None:
        raw_screening = env_first("FEISHU_SCREENING_ENABLED")
        screening_enabled = normalize_bool(raw_screening, variable="FEISHU_SCREENING_ENABLED") if raw_screening is not None else False
    else:
        screening_enabled = bool(screening_flag)
    pdf_directory = Path(args.pdf_dir or env_first("FEISHU_PDF_DIR") or (Path.home() / "Downloads")).expanduser().resolve()
    output_directory = Path(args.output_dir or env_first("FEISHU_MONITOR_OUTPUT_DIR") or (PROJECT_ROOT / "outputs" / "feishu-resume-monitor")).expanduser().resolve()
    state_path = Path(args.state_file or env_first("FEISHU_MONITOR_STATE_FILE") or (PROJECT_ROOT / "var" / "feishu-resume-monitor" / "state.json")).expanduser().resolve()
    report_path = Path(args.report_file or env_first("FEISHU_MONITOR_REPORT_FILE") or (output_directory / "batch-report.json")).expanduser().resolve()
    history_path = Path(args.history_file or env_first("FEISHU_MONITOR_HISTORY_FILE") or (output_directory / "batch-history.ndjson")).expanduser().resolve()
    records_path = Path(args.records_file or env_first("FEISHU_MONITOR_RECORDS_FILE") or (PROJECT_ROOT / "var" / "feishu-resume-monitor" / "records.ndjson")).expanduser().resolve()
    folder_token = args.folder_token or env_first("FEISHU_DOC_FOLDER_TOKEN")
    lock_key = hashlib.sha256((folder_token or "root").encode("utf-8")).hexdigest()[:16]
    lock_path = Path(args.lock_file or env_first("FEISHU_MONITOR_LOCK_FILE") or (PROJECT_ROOT / "var" / "feishu-resume-monitor" / f"{lock_key}.import.lock")).expanduser().resolve()
    screening_database = Path(getattr(args, "screening_database", None) or env_first("FEISHU_SCREENING_DATABASE") or DEFAULT_SCREENING_DATABASE).expanduser().resolve()
    screening_output_directory = Path(getattr(args, "screening_output", None) or env_first("FEISHU_SCREENING_OUTPUT_DIR") or DEFAULT_SCREENING_OUTPUT_DIRECTORY).expanduser().resolve()
    screening_role = getattr(args, "screening_role", None) or env_first("FEISHU_SCREENING_ROLE") or DEFAULT_SCREENING_ROLE
    screening_model = getattr(args, "screening_model", None) or env_first("FEISHU_SCREENING_MODEL") or DEFAULT_SCREENING_MODEL
    base_token = args.base_token or env_first("FEISHU_BASE_TOKEN") or ""
    config = MonitorConfig(
        base_token=base_token,
        table_id=args.table_id or env_first("FEISHU_TABLE_ID") or DEFAULT_TABLE_ID,
        view_id=args.view_id or env_first("FEISHU_VIEW_ID") or DEFAULT_VIEW_ID,
        pdf_directory=pdf_directory,
        output_directory=output_directory,
        state_path=state_path,
        report_path=report_path,
        history_path=history_path,
        records_path=records_path,
        lock_path=lock_path,
        row_key_column=args.row_key_column or env_first("FEISHU_ROW_KEY_COLUMN", "ROW_KEY_COLUMN") or DEFAULT_ROW_KEY_COLUMN,
        job_prefix=args.job_prefix or env_first("FEISHU_JOB_PREFIX") or DEFAULT_JOB_PREFIX,
        link_column=args.link_column or env_first("FEISHU_LINK_COLUMN", "LINK_COLUMN") or DEFAULT_LINK_COLUMN,
        status_column=args.status_column or env_first("FEISHU_STATUS_COLUMN", "STATUS_COLUMN") or DEFAULT_STATUS_COLUMN,
        error_column=args.error_column or env_first("FEISHU_ERROR_COLUMN", "ERROR_COLUMN") or DEFAULT_ERROR_COLUMN,
        processed_at_column=args.processed_at_column or env_first("FEISHU_PROCESSED_AT_COLUMN", "PROCESSED_AT_COLUMN") or DEFAULT_PROCESSED_AT_COLUMN,
        source_hash_column=args.source_hash_column or env_first("FEISHU_SOURCE_HASH_COLUMN", "SOURCE_HASH_COLUMN") or DEFAULT_SOURCE_HASH_COLUMN,
        optional_ai_column=args.ai_column or env_first("FEISHU_AI_COLUMN") or DEFAULT_AI_COLUMN,
        folder_token=folder_token,
        cli_executable=args.cli or env_first("LARK_CLI") or "",
        dry_run=bool(dry_run),
        watch=bool(args.watch),
        interval_seconds=args.interval_seconds,
        task_timeout_seconds=args.task_timeout_seconds,
        task_poll_seconds=args.task_poll_seconds,
        max_files=args.max_files,
        retry_failed=bool(args.retry_failed),
        screening_enabled=screening_enabled,
        screening_database=screening_database,
        screening_output_directory=screening_output_directory,
        screening_role=screening_role,
        screening_model=screening_model,
    )
    if not config.base_token:
        raise ValueError("FEISHU_BASE_TOKEN is required; do not place it in the script or report")
    if config.interval_seconds <= 0 or config.task_timeout_seconds <= 0 or config.task_poll_seconds <= 0:
        raise ValueError("interval and task timing values must be positive")
    if config.max_files is not None and config.max_files < 1:
        raise ValueError("--max-files must be positive")
    if config.screening_enabled and config.screening_role not in ROLE_VERSIONS:
        raise ValueError(f"unsupported screening role: {config.screening_role!r}")
    return config


class FeishuResumeMonitor:
    def __init__(self, config: MonitorConfig, cli: LarkCLI | None = None):
        self.config = config
        self.cli = cli or LarkCLI(config.cli_executable)
        self._screening_queue: ScreeningQueueBridge | None = None

    def _screening_bridge(self) -> ScreeningQueueBridge:
        if not self.config.screening_enabled:
            raise ScreeningHandoffError("screening handoff is disabled")
        if self._screening_queue is None:
            self._screening_queue = ScreeningQueueBridge(
                database=self.config.screening_database,
                output_directory=self.config.screening_output_directory,
                role=self.config.screening_role,
                model=self.config.screening_model,
            )
        return self._screening_queue

    def _screening_handoff(self, item: dict[str, Any]) -> dict[str, Any]:
        if not self.config.screening_enabled:
            return {"status": "disabled", "error": "screening handoff is disabled"}
        if self.config.dry_run:
            return {
                "status": "blocked",
                "error_code": "DRY_RUN",
                "error": "dry-run does not enqueue AI screening tasks",
            }
        try:
            return self._screening_bridge().enqueue_after_readback(
                source_path=str(item["source_path"]),
                source_sha256=str(item["source_sha256"]),
                candidate_id=str(item["candidate_id"]),
                candidate_name=item.get("candidate_name"),
                document_url=item.get("imported_doc_url"),
                readback_nonempty=item.get("readback_nonempty"),
                readback_chars=item.get("readback_chars"),
            )
        except ScreeningHandoffError as exc:
            return {
                "status": "failed",
                "error_code": "AI_ACTION_FAILED",
                "error": sanitize_diagnostic(str(exc)),
            }

    def _refresh_screening_status(self, screening: Any) -> dict[str, Any] | None:
        if not isinstance(screening, dict):
            return None
        task_id = screening.get("task_id")
        if task_id is None or not self.config.screening_enabled:
            return screening
        try:
            return self._screening_bridge().task_status(int(task_id))
        except ScreeningHandoffError as exc:
            return {
                **screening,
                "status": "failed",
                "error_code": "AI_ACTION_FAILED",
                "error": sanitize_diagnostic(str(exc)),
            }

    def _screening_for_item(
        self, item: dict[str, Any], previous: Any = None
    ) -> dict[str, Any]:
        if isinstance(previous, dict) and previous.get("task_id") is not None:
            current = self._refresh_screening_status(previous)
            if current is not None:
                return current
        return self._screening_handoff(item)

    @staticmethod
    def _note_screening_handoff(
        report: dict[str, Any], screening: dict[str, Any]
    ) -> None:
        if screening.get("status") not in {"disabled", "blocked", "failed"}:
            report["execution"]["screening_handoffs"] += 1

    def _base_args(self, command: str, *extra: str, format_name: str = "json") -> list[str]:
        return ["base", command, *extra, "--base-token", self.config.base_token, "--as", "user", "--format", format_name]

    def preflight(self) -> PreflightContext:
        if not self.config.pdf_directory.is_dir():
            report = {
                "ok": False,
                "error": "PDF input directory is unavailable",
                "pdf_directory": str(self.config.pdf_directory),
                "writeback_allowed": False,
                "retryable": True,
            }
            return PreflightContext(False, report, [], {}, {}, None, report["error"])
        table = self.cli.run(self._base_args("+table-get", "--table-id", self.config.table_id))
        if not table.ok:
            error = "table-get failed: " + table.diagnostic
            return PreflightContext(False, {"ok": False, "error": error, "writeback_allowed": False, "retryable": is_transient(error)}, [], {}, {}, None, error)
        fields_response = self.cli.run(self._base_args("+field-list", "--table-id", self.config.table_id))
        if not fields_response.ok:
            error = "field-list failed: " + fields_response.diagnostic
            return PreflightContext(False, {"ok": False, "error": error, "writeback_allowed": False, "retryable": is_transient(error)}, [], {}, {}, None, error)
        view = self.cli.run(self._base_args("+view-get", "--table-id", self.config.table_id, "--view-id", self.config.view_id))
        if not view.ok:
            error = "view-get failed: " + view.diagnostic
            return PreflightContext(False, {"ok": False, "error": error, "writeback_allowed": False, "retryable": is_transient(error)}, [], {}, {}, None, error)
        raw_fields = ((fields_response.payload.get("data") or {}).get("fields") if isinstance(fields_response.payload, dict) else None)
        if not isinstance(raw_fields, list):
            error = "field-list returned no field schema"
            return PreflightContext(False, {"ok": False, "error": error, "writeback_allowed": False, "retryable": False}, [], {}, {}, None, error)
        fields = {str(field.get("name")): field for field in raw_fields if isinstance(field, dict) and field.get("name")}
        missing = [name for name in self.config.required_columns if name not in fields]
        row_key_missing = self.config.row_key_column not in fields
        type_errors = writeback_diagnostics(self.config, fields)
        if row_key_missing:
            type_errors.append(f"missing row key column: {self.config.row_key_column}")
        record_path = record_snapshot_path(self.config)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_args = self._base_args(
            "+record-list",
            "--table-id",
            self.config.table_id,
            "--view-id",
            self.config.view_id,
            "--output",
            relative_to_root(record_path),
            "--overwrite",
            "--limit",
            "2000",
            format_name="ndjson",
        )
        records_response = self.cli.run(record_args, timeout=120)
        # With --output/ndjson the CLI writes the artifact and may leave
        # stdout empty instead of returning a JSON envelope. The exit code and
        # fresh artifact are the authoritative result for this read path.
        if records_response.returncode != 0 or not record_path.exists():
            error = "record-list failed: " + records_response.diagnostic
            return PreflightContext(False, {"ok": False, "error": error, "writeback_allowed": False, "retryable": is_transient(error)}, [], {}, fields, None, error)
        try:
            records = read_ndjson(record_path)
        except MonitorError as exc:
            error = str(exc)
            return PreflightContext(False, {"ok": False, "error": error, "writeback_allowed": False, "retryable": False}, [], {}, fields, None, error)
        manifest = record_path.with_name(record_path.stem + ".manifest.json")
        manifest_value = load_json(manifest, {}) if manifest.exists() else {}
        if isinstance(manifest_value, dict) and "base_token" in manifest_value:
            # The CLI manifest is useful for completeness but must not persist
            # the Base credential in the monitor's local artifacts.
            manifest_value.pop("base_token", None)
            write_json(manifest, manifest_value)
        has_more = bool(manifest_value.get("has_more")) if isinstance(manifest_value, dict) else False
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in records:
            key = normalize(cell_text(row.get(self.config.row_key_column)))
            if key:
                by_key.setdefault(key, []).append(row)
        fingerprint = preflight_fingerprint(self.config, fields, records, missing, type_errors)
        report = {
            "ok": not has_more,
            "table_read": True,
            "view_read": True,
            "fields_read": True,
            "records_read": len(records),
            "has_more": has_more,
            "required_columns": list(self.config.required_columns),
            "missing_columns": missing,
            "row_key_column": self.config.row_key_column,
            "row_key_present": not row_key_missing,
            "unique_nonempty_row_keys": len(by_key) == sum(len(values) == 1 for values in by_key.values()),
            "optional_ai_column_present": self.config.optional_ai_column in fields,
            "writeback_allowed": not missing and not type_errors and not row_key_missing and not has_more,
            "operation_mode": "writeback_enabled" if not missing and not type_errors and not row_key_missing and not has_more else "document_only",
            "writeback_diagnostics": type_errors,
            "records_artifact": str(record_path),
            "fingerprint": fingerprint,
        }
        if has_more:
            report["error"] = "record-list returned an incomplete view"
        return PreflightContext(not has_more, report, records, by_key, fields, fingerprint, report.get("error"))

    def enumerate_pdfs(self) -> list[Path]:
        return sorted(
            [
                path
                for path in self.config.pdf_directory.iterdir()
                if path.is_file()
                and not is_ignored_watch_file(path)
                and path.suffix.casefold() == ".pdf"
                and pdf_key(path, self.config.job_prefix)
            ],
            key=lambda path: path.name,
        )

    def stable_for_cycle(self, path: Path, state: dict[str, Any]) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        signature = [stat.st_size, stat.st_mtime_ns]
        key = str(path.resolve()).casefold()
        previous = state["observations"].get(key)
        cycles = int(previous.get("stable_cycles", 0)) + 1 if isinstance(previous, dict) and previous.get("signature") == signature else 1
        state["observations"][key] = {"signature": signature, "stable_cycles": cycles, "updated_at": now_utc()}
        return not self.config.watch or cycles >= 2

    def _poll_ticket(self, ticket: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.task_timeout_seconds
        transient_attempts = 0
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
                timeout=min(60.0, self.config.task_timeout_seconds),
            )
            if response.ok:
                url = find_url(response.payload)
                if url:
                    return {"status": "ok", "url": url, "transient_attempts": transient_attempts}
                state = find_string(response.payload, {"status", "state"})
                if state and state.casefold() in {"failed", "error", "canceled", "cancelled"}:
                    return {"status": "failed", "error": "import task state: " + state, "transient_attempts": transient_attempts}
            else:
                error = response.diagnostic
                if not is_transient(error):
                    return {"status": "failed", "error": error, "transient_attempts": transient_attempts}
                transient_attempts += 1
                if transient_attempts >= MAX_TRANSIENT_ATTEMPTS:
                    return {"status": "pending", "error": error, "transient_attempts": transient_attempts}
            time.sleep(min(self.config.task_poll_seconds, max(0.1, deadline - time.monotonic())))
        return {"status": "pending", "error": "import task did not return a document URL before timeout", "transient_attempts": transient_attempts}

    def _run_cli_with_transient_retries(
        self, args: Sequence[str], *, timeout: float
    ) -> tuple[CliResponse, int]:
        transient_retries = 0
        response: CliResponse | None = None
        for attempt in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
            response = self.cli.run(args, timeout=timeout)
            if response.ok:
                return response, transient_retries
            if not is_transient(response.diagnostic) or attempt == MAX_TRANSIENT_ATTEMPTS:
                return response, transient_retries
            transient_retries += 1
            time.sleep(2 * attempt)
        assert response is not None
        return response, transient_retries

    def _fetch_document(
        self,
        url: str,
        *,
        attempts: int,
        transient_attempts: int,
        ticket: str | None = None,
    ) -> dict[str, Any]:
        fetched, fetch_retries = self._run_cli_with_transient_retries(
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
        total_transient_attempts = transient_attempts + fetch_retries
        if not fetched.ok:
            return {"status": "import_failed", "attempts": attempts, "transient_attempts": total_transient_attempts, "url": url, "ticket": ticket, "error": "document_readback_failed: " + fetched.diagnostic}
        content = ((fetched.payload.get("data") or {}).get("document") or {}).get("content") if isinstance(fetched.payload, dict) else None
        if not isinstance(content, str) or not content.strip():
            return {"status": "import_failed", "attempts": attempts, "transient_attempts": total_transient_attempts, "url": url, "ticket": ticket, "error": "document_readback_failed: empty document body"}
        issues = markdown_quality_issues(content)
        if issues:
            return {"status": "import_failed", "attempts": attempts, "transient_attempts": total_transient_attempts, "url": url, "ticket": ticket, "error": "document_readback_failed: " + "; ".join(issues)}
        return {"status": "success", "attempts": attempts, "transient_attempts": total_transient_attempts, "ticket": ticket, "url": url, "readback_nonempty": True, "readback_chars": len(content.strip())}

    def _import_unlocked(self, markdown_path: Path, display_name: str) -> dict[str, Any]:
        args = [
            "drive",
            "+import",
            "--file",
            relative_to_root(markdown_path),
            "--type",
            "docx",
            "--name",
            display_name,
            "--as",
            "user",
            "--format",
            "json",
        ]
        if self.config.folder_token:
            args.extend(("--folder-token", self.config.folder_token))
        response: CliResponse | None = None
        attempts = 0
        transient_attempts = 0
        for attempts in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
            response = self.cli.run(args, timeout=120)
            if response.ok:
                break
            error = response.diagnostic
            if not is_transient(error) or attempts == MAX_TRANSIENT_ATTEMPTS:
                return {
                    "status": "import_failed",
                    "attempts": attempts,
                    "transient_attempts": transient_attempts,
                    "error": error,
                }
            # The installed drive +import command has no idempotency key or
            # request identifier. A transient response without a URL/ticket
            # may mean the request was accepted, so replaying it is unsafe.
            if not find_url(response.payload) and not find_ticket(response.payload):
                return {
                    "status": "import_pending",
                    "attempts": attempts,
                    "transient_attempts": transient_attempts,
                    "import_outcome_uncertain": True,
                    "error": f"{UNCERTAIN_IMPORT_ERROR}: {error}",
                }
            break
        assert response is not None
        url = find_url(response.payload)
        ticket = find_ticket(response.payload)
        if not url:
            if not ticket:
                return {
                    "status": "import_pending",
                    "attempts": attempts,
                    "transient_attempts": transient_attempts,
                    "import_outcome_uncertain": True,
                    "error": UNCERTAIN_IMPORT_ERROR,
                }
            polled = self._poll_ticket(ticket)
            if polled.get("status") == "pending":
                return {"status": "import_pending", "attempts": attempts, "transient_attempts": transient_attempts + int(polled.get("transient_attempts", 0)), "ticket": ticket, "error": polled.get("error")}
            if polled.get("status") != "ok":
                return {"status": "import_failed", "attempts": attempts, "transient_attempts": transient_attempts + int(polled.get("transient_attempts", 0)), "ticket": ticket, "error": polled.get("error")}
            url = str(polled["url"])
        return self._fetch_document(
            url,
            attempts=attempts,
            transient_attempts=transient_attempts,
            ticket=ticket,
        )

    def import_document(self, markdown_path: Path, display_name: str) -> dict[str, Any]:
        try:
            with DestinationLock(self.config.lock_path):
                return self._import_unlocked(markdown_path, display_name)
        except ImportLockBusy as exc:
            return {"status": "import_pending", "attempts": 0, "error": str(exc)}

    def resume_import(self, ticket: str) -> dict[str, Any]:
        """Finish a persisted async task without creating a second document."""

        try:
            with DestinationLock(self.config.lock_path):
                polled = self._poll_ticket(ticket)
                if polled.get("status") == "pending":
                    return {
                        "status": "import_pending",
                        "attempts": 0,
                        "transient_attempts": polled.get("transient_attempts", 0),
                        "ticket": ticket,
                        "error": polled.get("error"),
                    }
                if polled.get("status") != "ok":
                    return {
                        "status": "import_failed",
                        "attempts": 0,
                        "transient_attempts": polled.get("transient_attempts", 0),
                        "ticket": ticket,
                        "error": polled.get("error"),
                    }
                return self._fetch_document(
                    str(polled["url"]),
                    attempts=0,
                    transient_attempts=int(polled.get("transient_attempts", 0)),
                    ticket=ticket,
                )
        except ImportLockBusy as exc:
            return {"status": "import_pending", "attempts": 0, "ticket": ticket, "error": str(exc)}

    def _record_import_result(
        self,
        preflight: PreflightContext,
        item: dict[str, Any],
        result: dict[str, Any],
        report: dict[str, Any],
        previous_entry: dict[str, Any] | None = None,
        *,
        count_new_import: bool,
    ) -> dict[str, Any]:
        item["status"] = result.get("status", "import_failed")
        item["attempts"] = result.get("attempts", 0)
        document_url = cell_text(result.get("url")) or state_doc_url(previous_entry)
        item["imported_doc_url"] = document_url
        item["readback_nonempty"] = result.get("readback_nonempty")
        item["readback_chars"] = result.get("readback_chars") or (previous_entry or {}).get("readback_chars")
        item["error"] = result.get("error")
        import_outcome_uncertain = bool(result.get("import_outcome_uncertain", False))
        if item["status"] == "import_pending" and not result.get("ticket"):
            import_outcome_uncertain = True
        elif item["status"] == "import_failed" and not document_url:
            import_outcome_uncertain = True
        item["import_outcome_uncertain"] = import_outcome_uncertain
        state_entry = {
            **(previous_entry or {}),
            **item,
            "ticket": result.get("ticket") or (previous_entry or {}).get("ticket"),
            "transient_attempts": result.get("transient_attempts", 0),
            "updated_at": now_utc(),
            "base_written": bool((previous_entry or {}).get("base_written", False)),
            "import_outcome_uncertain": import_outcome_uncertain,
        }
        state_entry["doc_url"] = document_url
        if item["status"] == "success" and item.get("imported_doc_url"):
            if count_new_import:
                report["execution"]["document_imports"] += 1
            item["screening"] = self._screening_for_item(
                item, (previous_entry or {}).get("screening")
            )
            self._note_screening_handoff(report, item["screening"])
            state_entry["screening"] = item["screening"]
            writeback = self.writeback(
                preflight,
                item,
                str(item["imported_doc_url"]),
                base_already_written=bool(state_entry.get("base_written")),
            ) if not self.config.dry_run else {"status": "blocked", "error": "dry-run"}
            item["writeback"] = writeback
            if writeback.get("status") == "verified" and writeback.get("written"):
                state_entry["base_written"] = True
                report["execution"]["base_writeback_performed"] = True
                report["execution"]["remote_writes"] += 1
            elif writeback.get("status") == "failed":
                item["status"] = "sheet_write_failed"
                item["error"] = writeback.get("error")
            state_entry["status"] = item["status"]
        return state_entry

    def writeback(self, preflight: PreflightContext, item: dict[str, Any], document_url: str, *, base_already_written: bool = False) -> dict[str, Any]:
        if not preflight.writeback_allowed:
            return {"status": "blocked", "error": "required Base processing fields are absent or not writable"}
        if base_already_written:
            return {"status": "verified", "written": False}
        processed_at = now_local()
        updates = build_writeback_updates(
            self.config,
            preflight.fields,
            document_url=document_url,
            source_hash=str(item["source_sha256"]),
            processed_at=processed_at,
        )
        payload = {"update_records": {str(item["record_id"]): updates}}
        response, update_retries = self._run_cli_with_transient_retries(
            self._base_args(
                "+record-batch-update",
                "--table-id",
                self.config.table_id,
                "--json",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
            timeout=90,
        )
        if not response.ok:
            return {
                "status": "failed",
                "written": False,
                "transient_attempts": update_retries,
                "error": response.diagnostic,
            }
        ignored = ignored_field_names(response.payload)
        if ignored.intersection(updates):
            return {"status": "failed", "written": False, "error": "Base update ignored configured fields"}
        field_ids = [str(preflight.fields[name].get("id") or name) for name in (self.config.link_column, self.config.status_column, self.config.error_column, self.config.processed_at_column, self.config.source_hash_column)]
        get_args = self._base_args(
            "+record-get",
            "--table-id",
            self.config.table_id,
            "--record-id",
            str(item["record_id"]),
        )
        for field_id in field_ids:
            get_args.extend(("--field-id", field_id))
        verified, verify_retries = self._run_cli_with_transient_retries(get_args, timeout=90)
        if not verified.ok:
            return {
                "status": "failed",
                "written": False,
                "transient_attempts": update_retries + verify_retries,
                "error": "Base write verification failed: " + verified.diagnostic,
            }
        data = (verified.payload.get("data") or {}) if isinstance(verified.payload, dict) else {}
        values = data.get("data") if isinstance(data, dict) else None
        first_row = values[0] if isinstance(values, list) and values and isinstance(values[0], list) else []
        if not compare_writeback(self.config, first_row, updates):
            return {"status": "failed", "written": False, "error": "Base write verification values did not match"}
        return {
            "status": "verified",
            "written": True,
            "processed_at": processed_at,
            "transient_attempts": update_retries + verify_retries,
        }

    def _gate_allows_apply(self, state: dict[str, Any], fingerprint: str | None) -> bool:
        last = state.get("last_dry_run_preflight")
        return isinstance(last, dict) and bool(fingerprint) and last.get("fingerprint") == fingerprint

    def _save_cycle(self, report: dict[str, Any], state: dict[str, Any]) -> None:
        write_json(self.config.state_path, state)
        write_json(self.config.report_path, report)

    def _run_cycle_locked(self) -> dict[str, Any]:
        started = now_utc()
        state = load_state(self.config.state_path)
        preflight = self.preflight()
        report: dict[str, Any] = {
            "report_version": REPORT_VERSION,
            "cycle_started_at": started,
            "cycle_finished_at": None,
            "mode": "watch" if self.config.watch else "once",
            "dry_run": self.config.dry_run,
            "target": {"table_id": self.config.table_id, "view_id": self.config.view_id, "row_key_column": self.config.row_key_column, "job_prefix": self.config.job_prefix},
            "input": {"pdf_directory": str(self.config.pdf_directory), "files_sorted_by": "file_name (Python Unicode lexicographic order)", "pdf_suffix_filter": ".pdf only", "job_prefix_filter": self.config.job_prefix},
            "pdf_key_rule": {"pattern": pdf_key_rule(self.config.job_prefix), "normalization": "NFKC + remove whitespace; exact match to Base row key; require exactly one record"},
            "preflight": preflight.report,
            "screening": {"enabled": self.config.screening_enabled, "handoff": "after_nonempty_document_readback" if self.config.screening_enabled else "disabled", "role": self.config.screening_role, "model": self.config.screening_model, "database": str(self.config.screening_database), "output_directory": str(self.config.screening_output_directory)},
            "execution": {"same_folder_imports_serial": True, "base_writeback_performed": False, "next_action_executed": False, "remote_writes": 0, "document_imports": 0, "screening_handoffs": 0, "writeback_mode": "writeback_enabled" if preflight.writeback_allowed else "document_only", "writeback_block_reason": None if preflight.writeback_allowed else "configured processing columns are missing or not writable"},
            "summary": {},
            "items": [],
            "state_file": str(self.config.state_path),
        }
        if not preflight.ok:
            report["cycle_status"] = "preflight_failed"
            report["summary"] = {"pdf_total": 0, "preflight_failed": 1, "base_records_written": 0}
            report["cycle_finished_at"] = now_utc()
            self._save_cycle(report, state)
            append_jsonl(self.config.history_path, {"cycle_finished_at": report["cycle_finished_at"], "cycle_status": report["cycle_status"], "summary": report["summary"]})
            return report
        if self.config.dry_run:
            state["last_dry_run_preflight"] = {"fingerprint": preflight.fingerprint, "completed_at": now_utc()}
        elif not self._gate_allows_apply(state, preflight.fingerprint):
            report["cycle_status"] = "preflight_gate_required"
            report["summary"] = {"pdf_total": 0, "preflight_gate_required": 1, "base_records_written": 0}
            report["cycle_finished_at"] = now_utc()
            self._save_cycle(report, state)
            append_jsonl(self.config.history_path, {"cycle_finished_at": report["cycle_finished_at"], "cycle_status": report["cycle_status"], "summary": report["summary"]})
            return report
        try:
            pdfs = self.enumerate_pdfs()
        except OSError as exc:
            report["cycle_status"] = "pdf_scan_failed"
            report["preflight"]["error"] = "PDF scan failed"
            report["summary"] = {"pdf_total": 0, "preflight_failed": 1, "base_records_written": 0}
            report["cycle_finished_at"] = now_utc()
            self._save_cycle(report, state)
            append_jsonl(self.config.history_path, {"cycle_finished_at": report["cycle_finished_at"], "cycle_status": report["cycle_status"], "summary": report["summary"]})
            return report
        work_count = 0
        for path in pdfs:
            item = output_item_base(path, job_prefix=self.config.job_prefix)
            if not self.stable_for_cycle(path, state):
                item["error"] = "waiting for two stable watch observations"
                report["items"].append(item)
                continue
            try:
                source_hash = sha256(path)
                item["source_sha256"] = source_hash
                item["candidate_id"] = f"feishu-{source_hash[:12]}"
            except SourceChanged as exc:
                item["status"] = "pending"
                item["error"] = str(exc)
                report["items"].append(item)
                continue
            key = normalize(item["pdf_key"])
            matches = preflight.by_key.get(key, []) if key else []
            item["match_count"] = len(matches)
            if len(matches) != 1:
                item["status"] = "ambiguous_match"
                item["error"] = "no unique Base record matched PDF_KEY_RULE" if not matches else "multiple Base records matched PDF_KEY_RULE"
                report["items"].append(item)
                continue
            row = matches[0]
            item["record_id"] = row.get("record_id")
            item["candidate_name"] = cell_text(row.get(self.config.row_key_column)) or item["pdf_key"]
            duplicate, reason, existing_url = base_row_duplicate(self.config, row, source_hash)
            entry = state["entries"].get(source_hash)
            if duplicate:
                item["status"] = "already_processed"
                item["error"] = reason
                item["imported_doc_url"] = existing_url
                if isinstance(entry, dict):
                    item["markdown_path"] = entry.get("markdown_path")
                    item["cleaned_markdown_path"] = entry.get("cleaned_markdown_path")
                    item["readback_nonempty"] = entry.get("readback_nonempty")
                    item["readback_chars"] = entry.get("readback_chars")
                    if not item.get("imported_doc_url"):
                        item["imported_doc_url"] = state_doc_url(entry)
                    item["screening"] = self._screening_for_item(
                        item, entry.get("screening")
                    )
                    self._note_screening_handoff(report, item["screening"])
                    entry["screening"] = item["screening"]
                elif self.config.screening_enabled:
                    item["screening"] = self._screening_handoff(item)
                    self._note_screening_handoff(report, item["screening"])
                report["items"].append(item)
                continue
            entry_doc_url = state_doc_url(entry)
            if (
                isinstance(entry, dict)
                and entry_doc_url
                and entry.get("status") in {"success", "sheet_write_failed"}
            ):
                if entry.get("status") == "sheet_write_failed" and not self.config.retry_failed:
                    item = item_from_state(
                        item,
                        entry,
                        reason="previous Base writeback failure retained; rerun with --retry-failed to retry",
                    )
                    report["items"].append(item)
                    continue
                state_reason = (
                    "local state already contains this source hash"
                    if entry.get("status") == "success"
                    else "existing document retained; retrying Base writeback"
                )
                item = item_from_state(item, entry, reason=state_reason)
                entry["doc_url"] = entry_doc_url
                item["screening"] = self._screening_for_item(
                    item, entry.get("screening")
                )
                self._note_screening_handoff(report, item["screening"])
                entry["screening"] = item["screening"]
                writeback = self.writeback(preflight, item, entry_doc_url, base_already_written=bool(entry.get("base_written"))) if not self.config.dry_run else {"status": "blocked", "error": "dry-run"}
                item["writeback"] = writeback
                if writeback.get("status") == "verified":
                    entry["base_written"] = True
                    entry["status"] = "success"
                    if writeback.get("written"):
                        report["execution"]["base_writeback_performed"] = True
                        report["execution"]["remote_writes"] += 1
                    item["status"] = "already_processed"
                    item["error"] = "existing document retained; Base writeback verified"
                elif writeback.get("status") == "failed":
                    entry["status"] = "sheet_write_failed"
                    item["status"] = "sheet_write_failed"
                    item["error"] = writeback.get("error")
                report["items"].append(item)
                continue
            if isinstance(entry, dict) and entry.get("status") == "import_pending" and entry.get("ticket"):
                result = self.resume_import(str(entry["ticket"]))
                state_entry = self._record_import_result(
                    preflight,
                    item,
                    result,
                    report,
                    previous_entry=entry,
                    count_new_import=False,
                )
                report["items"].append(item)
                state["entries"][source_hash] = state_entry
                self._save_cycle(report, state)
                continue
            if (
                isinstance(entry, dict)
                and entry.get("status") == "import_failed"
                and entry_doc_url
                and self.config.retry_failed
                and not self.config.dry_run
            ):
                result = self._fetch_document(
                    entry_doc_url,
                    attempts=0,
                    transient_attempts=0,
                    ticket=entry.get("ticket"),
                )
                state_entry = self._record_import_result(
                    preflight,
                    item,
                    result,
                    report,
                    previous_entry=entry,
                    count_new_import=False,
                )
                report["items"].append(item)
                state["entries"][source_hash] = state_entry
                self._save_cycle(report, state)
                continue
            if isinstance(entry, dict) and entry.get("status") in {"import_failed", "markdown_failed", "ocr_required", "sheet_write_failed"} and not self.config.retry_failed:
                item = item_from_state(item, entry, reason="previous failure retained; rerun with --retry-failed to retry")
                report["items"].append(item)
                continue
            if (
                isinstance(entry, dict)
                and not entry_doc_url
                and entry.get("status") in {"import_pending", "import_failed"}
                and not (entry.get("status") == "import_pending" and entry.get("ticket"))
            ):
                item = item_from_state(
                    item,
                    entry,
                    reason=unresolved_import_reason(entry),
                )
                entry["status"] = "import_pending"
                entry["import_outcome_uncertain"] = True
                item["status"] = "import_pending"
                item["import_outcome_uncertain"] = True
                report["items"].append(item)
                continue
            if self.config.max_files is not None and work_count >= self.config.max_files:
                item["error"] = "deferred by --max-files"
                report["items"].append(item)
                continue
            work_count += 1
            candidate_id = str(item["candidate_id"])
            try:
                prepared = prepare_markdown(path, candidate_id, str(item["candidate_name"]), self.config.output_directory)
                item.update({key: value for key, value in prepared.items() if key.endswith("path") or key in {"markdown_chars", "page_count", "used_ocr", "parser_version"}})
                item["markdown_path"] = str(Path(prepared["markdown_path"]).resolve())
                item["cleaned_markdown_path"] = str(Path(prepared["cleaned_markdown_path"]).resolve())
            except ResumeQualityError as exc:
                item["status"] = "ocr_required" if "OCR" in str(exc) or "扫描" in str(exc) else "markdown_failed"
                item["error"] = sanitize_diagnostic(str(exc))
                state["entries"][source_hash] = {**item, "status": item["status"], "updated_at": now_utc()}
                report["items"].append(item)
                self._save_cycle(report, state)
                continue
            except (MonitorError, OSError) as exc:
                item["status"] = "markdown_failed"
                item["error"] = sanitize_diagnostic(str(exc))
                state["entries"][source_hash] = {**item, "status": item["status"], "updated_at": now_utc()}
                report["items"].append(item)
                self._save_cycle(report, state)
                continue
            if self.config.dry_run:
                item["status"] = "pending"
                item["error"] = "dry-run: import and Base writeback not executed"
                state["entries"][source_hash] = {**item, "status": "prepared", "updated_at": now_utc()}
                report["items"].append(item)
                self._save_cycle(report, state)
                continue
            state["entries"][source_hash] = {
                **item,
                "status": "import_pending",
                "import_outcome_uncertain": True,
                "import_started_at": now_utc(),
                "updated_at": now_utc(),
            }
            self._save_cycle(report, state)
            result = self.import_document(Path(item["markdown_path"]), f"{safe_name(str(item['candidate_name']))}-简历")
            state_entry = self._record_import_result(
                preflight,
                item,
                result,
                report,
                count_new_import=True,
            )
            state["entries"][source_hash] = state_entry
            report["items"].append(item)
            self._save_cycle(report, state)
        counts = Counter(str(item.get("status")) for item in report["items"])
        screening_counts = Counter(
            str(item["screening"].get("status"))
            for item in report["items"]
            if isinstance(item.get("screening"), dict)
            and item["screening"].get("status")
        )
        report["cycle_status"] = "completed"
        report["summary"] = {
            "pdf_total": len(pdfs),
            "success": counts.get("success", 0),
            "already_processed": counts.get("already_processed", 0),
            "pending": counts.get("pending", 0),
            "ambiguous_match": counts.get("ambiguous_match", 0),
            "ocr_required": counts.get("ocr_required", 0),
            "markdown_failed": counts.get("markdown_failed", 0),
            "import_failed": counts.get("import_failed", 0),
            "import_pending": counts.get("import_pending", 0),
            "sheet_write_failed": counts.get("sheet_write_failed", 0),
            "ai_action_failed": screening_counts.get("failed", 0),
            "screening_queued": screening_counts.get("queued", 0),
            "screening_processing": screening_counts.get("processing", 0),
            "screening_succeeded": screening_counts.get("succeeded", 0),
            "screening_manual_review": screening_counts.get("manual_review", 0),
            "screening_retryable_failed": screening_counts.get("retryable_failed", 0),
            "base_records_written": report["execution"]["remote_writes"],
        }
        report["cycle_finished_at"] = now_utc()
        self._save_cycle(report, state)
        append_jsonl(self.config.history_path, {"cycle_finished_at": report["cycle_finished_at"], "cycle_status": report["cycle_status"], "summary": report["summary"]})
        return report

    def run_cycle(self) -> dict[str, Any]:
        # This lock is deliberately separate from lock_path, which protects an
        # individual import. It serializes the state/Base decision with that
        # import without nesting the same OS lock.
        cycle_lock_path = self.config.lock_path.with_name(self.config.lock_path.name + ".cycle")
        with DestinationLock(cycle_lock_path, blocking=True):
            return self._run_cycle_locked()

    def seed_from_report(self, report_path: Path) -> int:
        report = load_json(report_path, {})
        if not isinstance(report, dict) or not isinstance(report.get("items"), list):
            raise MonitorError("seed report does not contain an items array")
        state = load_state(self.config.state_path)
        seeded = 0
        for item in report["items"]:
            if not isinstance(item, dict) or item.get("status") != "success":
                continue
            source_hash = cell_text(item.get("source_sha256"))
            doc_url = cell_text(item.get("imported_doc_url"))
            if not source_hash or not doc_url:
                continue
            state["entries"][source_hash] = {
                "source_sha256": source_hash,
                "source_path": item.get("source_path"),
                "file_name": item.get("file_name"),
                "candidate_name": item.get("candidate_name"),
                "record_id": item.get("record_id"),
                "candidate_id": item.get("candidate_id"),
                "doc_url": doc_url,
                "markdown_path": str((PROJECT_ROOT / item["markdown_path"]).resolve()) if item.get("markdown_path") else None,
                "cleaned_markdown_path": str((PROJECT_ROOT / item["cleaned_markdown_path"]).resolve()) if item.get("cleaned_markdown_path") else None,
                "status": "success",
                "attempts": item.get("attempts", 0),
                "readback_nonempty": bool(item.get("readback_nonempty")) or bool(item.get("readback_chars")),
                "readback_chars": item.get("readback_chars"),
                "base_written": False,
                "seeded_from": str(report_path.resolve()),
                "updated_at": now_utc(),
            }
            seeded += 1
        write_json(self.config.state_path, state)
        return seeded


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu-resume-monitor")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one preflight/processing cycle and exit")
    mode.add_argument("--watch", action="store_true", help="repeat the cycle until interrupted")
    run_mode = parser.add_mutually_exclusive_group()
    run_mode.add_argument("--dry-run", dest="dry_run", action="store_true", help="preflight and prepare local Markdown only")
    run_mode.add_argument("--apply", dest="dry_run", action="store_false", help="allow imports; Base writeback remains schema-gated")
    parser.set_defaults(dry_run=None)
    screening_mode = parser.add_mutually_exclusive_group()
    screening_mode.add_argument("--screening", dest="screening_enabled", action="store_true", help="hand off read-back-complete documents to the existing AI screening queue")
    screening_mode.add_argument("--no-screening", dest="screening_enabled", action="store_false", help="do not hand off documents to the AI screening queue")
    parser.set_defaults(screening_enabled=None)
    parser.add_argument("--pdf-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--state-file")
    parser.add_argument("--report-file")
    parser.add_argument("--history-file")
    parser.add_argument("--records-file")
    parser.add_argument("--lock-file")
    parser.add_argument("--base-token")
    parser.add_argument("--table-id")
    parser.add_argument("--view-id")
    parser.add_argument("--job-prefix")
    parser.add_argument("--folder-token")
    parser.add_argument("--cli")
    parser.add_argument("--row-key-column")
    parser.add_argument("--link-column")
    parser.add_argument("--status-column")
    parser.add_argument("--error-column")
    parser.add_argument("--processed-at-column")
    parser.add_argument("--source-hash-column")
    parser.add_argument("--ai-column")
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--task-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--task-poll-seconds", type=float, default=2.0)
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--screening-database")
    parser.add_argument("--screening-output")
    parser.add_argument("--screening-role", choices=sorted(ROLE_VERSIONS))
    parser.add_argument("--screening-model")
    parser.add_argument("--seed-report", type=Path, help="seed local idempotency state from an existing successful batch report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    try:
        config = build_config(args)
        monitor = FeishuResumeMonitor(config)
        if args.seed_report:
            seeded = monitor.seed_from_report(args.seed_report.resolve())
            print(json.dumps({"seeded": seeded, "state_file": str(config.state_path)}, ensure_ascii=False, sort_keys=True))
        report = monitor.run_cycle()
        print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
        if not config.watch:
            return 0 if report.get("cycle_status") == "completed" else 2
        if report.get("cycle_status") == "preflight_failed" and not (report.get("preflight") or {}).get("retryable"):
            return 2
        while True:
            time.sleep(config.interval_seconds)
            report = monitor.run_cycle()
            print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True), flush=True)
            if report.get("cycle_status") == "preflight_failed" and not (report.get("preflight") or {}).get("retryable"):
                return 2
    except KeyboardInterrupt:
        print("monitor stopped")
        return 0
    except (MonitorError, ValueError, OSError) as exc:
        print(f"error: {sanitize_diagnostic(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

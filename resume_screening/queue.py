"""SQLite-backed idempotent task queue, worker lease, and health state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cleaning import PARSER_VERSION
from .versions import (
    ACTIVE_CONTRACTS,
    PROMPT_VERSION,
    SCORING_VERSION,
    contract_matches,
)

STALE_CONTRACT_VERSION = "STALE_CONTRACT_VERSION"
WATCH_EVENT_CODES = {
    "UNKNOWN_FILE_SKIPPED",
    "ROLE_MISMATCH_SKIPPED",
    "UNLABELED_FILE_SKIPPED",
    "UNSUPPORTED_FILE_SKIPPED",
    "WATCH_INPUT_UNAVAILABLE",
    "WATCH_SCAN_ERROR",
    "WATCH_HASH_ERROR",
    "WATCH_ENQUEUE_ERROR",
}
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[\w-]+", re.IGNORECASE | re.UNICODE
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?86[\s-]?)?1[3-9](?:[\s-]?\d){9}(?!\d)"
)
_LANDLINE_RE = re.compile(r"(?<!\d)0\d{2,3}[\s-]?\d{7,8}(?!\d)")
_AUTH_RE = re.compile(
    r"(?i)(bearer\s+|authorization\s*[:=]\s*bearer\s+)([^\s,;]+)"
)
_SECRET_RE = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|secret|password)\s*[:=]\s*[^\s,;]+"
)
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _safe_diagnostic(message: object) -> str:
    """Keep diagnostics useful while preventing accidental secret/PII output."""

    text = " ".join(str(message).split())
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _LANDLINE_RE.sub("[REDACTED_PHONE]", text)
    text = _AUTH_RE.sub(r"\1[REDACTED_TOKEN]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    configured_key = os.environ.get("MINIMAX_API_KEY")
    if configured_key:
        text = text.replace(configured_key, "[REDACTED_API_KEY]")
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}",
        "[REDACTED_API_KEY]",
        text,
    )
    text = _LONG_TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    return text[:1000]


sanitize_diagnostic = _safe_diagnostic


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Public alias used by the watch/enqueue boundary and tests that need to count
# full-file hashing.  The private name remains for compatibility with older
# callers in this repository.
file_sha256 = _file_sha256


@dataclass(frozen=True)
class TaskSpec:
    source_path: Path
    candidate_id: str
    role: str
    jd_version: str
    rubric_version: str
    candidate_name: str | None = None
    model: str = "MiniMax-M3"
    parser_version: str = PARSER_VERSION
    scoring_version: str = SCORING_VERSION
    prompt_version: str = PROMPT_VERSION
    source_sha256: str | None = None


@dataclass(frozen=True)
class TaskRecord:
    task_id: int
    task_key: str
    source_path: Path
    source_sha256: str
    candidate_id: str
    candidate_name: str | None
    role: str
    jd_version: str
    rubric_version: str
    model: str
    parser_version: str
    scoring_version: str
    prompt_version: str
    status: str
    model_completed: bool
    attempt_count: int
    error_code: str | None
    error_message: str | None


class WorkerLeaseError(RuntimeError):
    """Base error for the one-worker-per-database lease."""


class WorkerAlreadyRunningError(WorkerLeaseError):
    """Another live worker currently owns this database lease."""


class WorkerLeaseLostError(WorkerLeaseError):
    """The current process no longer owns the worker lease."""


def _contract_predicate(
    active_contracts: dict[str, tuple[str, str, str, str, str]],
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    parameters: list[str] = []
    for role, (jd, rubric, parser, scoring, prompt) in sorted(active_contracts.items()):
        clauses.append(
            "(role = ? AND jd_version = ? AND rubric_version = ? "
            "AND parser_version = ? AND scoring_version = ? AND prompt_version = ?)"
        )
        parameters.extend((role, jd, rubric, parser, scoring, prompt))
    return " OR ".join(clauses) or "0", parameters


def _contract_health_payload(
    active_contracts: dict[str, tuple[str, str, str, str, str]],
) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "scoring_version": SCORING_VERSION,
        "jd_versions": {role: values[0] for role, values in sorted(active_contracts.items())},
        "rubric_versions": {
            role: values[1] for role, values in sorted(active_contracts.items())
        },
    }


class TaskStore:
    def __init__(self, database: str | Path):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_key TEXT NOT NULL UNIQUE,
                    source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    candidate_name TEXT,
                    role TEXT NOT NULL,
                    jd_version TEXT NOT NULL,
                    rubric_version TEXT NOT NULL,
                    model TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    scoring_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued', 'processing', 'succeeded', 'manual_review', 'retryable_failed'
                    )),
                    model_completed INTEGER NOT NULL DEFAULT 0 CHECK(model_completed IN (0, 1)),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    api_response_id TEXT,
                    usage_json TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_id ON tasks(status, task_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_contract ON tasks(
                    role, jd_version, rubric_version, parser_version,
                    scoring_version, prompt_version, status
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('succeeded', 'error')),
                    error_code TEXT,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_time ON task_events(occurred_at);

                CREATE TABLE IF NOT EXISTS worker_leases (
                    lease_name TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    host TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('running', 'stopped')),
                    acquired_at TEXT NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_success_at TEXT,
                    pause_until TEXT,
                    pause_reason TEXT,
                    model_configured INTEGER NOT NULL DEFAULT 0 CHECK(model_configured IN (0, 1)),
                    lease_seconds INTEGER NOT NULL DEFAULT 300,
                    active_parser_version TEXT NOT NULL,
                    active_prompt_version TEXT NOT NULL,
                    active_scoring_version TEXT NOT NULL,
                    active_rubric_versions_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watch_counters (
                    code TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS human_reviews (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_row_key TEXT NOT NULL UNIQUE,
                    task_id INTEGER,
                    candidate_id TEXT,
                    role TEXT,
                    rubric_version TEXT,
                    model_recommendation TEXT,
                    human_conclusion TEXT NOT NULL,
                    reason_category TEXT NOT NULL,
                    criterion_id TEXT,
                    imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_human_reviews_task ON human_reviews(task_id);
                """
            )

    @staticmethod
    def _task_key(spec: TaskSpec, source_sha256: str) -> str:
        identity = {
            "source_sha256": source_sha256,
            "role": spec.role,
            "jd_version": spec.jd_version,
            "rubric_version": spec.rubric_version,
            "model": spec.model,
            "parser_version": spec.parser_version,
            "scoring_version": spec.scoring_version,
            "prompt_version": spec.prompt_version,
        }
        canonical = json.dumps(
            identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            task_key=row["task_key"],
            source_path=Path(row["source_path"]),
            source_sha256=row["source_sha256"],
            candidate_id=row["candidate_id"],
            candidate_name=row["candidate_name"],
            role=row["role"],
            jd_version=row["jd_version"],
            rubric_version=row["rubric_version"],
            model=row["model"],
            parser_version=row["parser_version"],
            scoring_version=row["scoring_version"],
            prompt_version=row["prompt_version"],
            status=row["status"],
            model_completed=bool(row["model_completed"]),
            attempt_count=row["attempt_count"],
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        task_id: int,
        event_type: str,
        error_code: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO task_events (task_id, event_type, error_code, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, event_type, error_code, _utc_now()),
        )

    def enqueue(self, spec: TaskSpec) -> TaskRecord:
        source = spec.source_path.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        source_sha256 = spec.source_sha256 or _file_sha256(source)
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        task_key = self._task_key(spec, source_sha256)
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO tasks (
                    task_key, source_path, source_sha256, candidate_id, candidate_name,
                    role, jd_version, rubric_version, model, parser_version,
                    scoring_version, prompt_version, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    task_key,
                    str(source),
                    source_sha256,
                    spec.candidate_id,
                    spec.candidate_name,
                    spec.role,
                    spec.jd_version,
                    spec.rubric_version,
                    spec.model,
                    spec.parser_version,
                    spec.scoring_version,
                    spec.prompt_version,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_key = ?", (task_key,)
            ).fetchone()
        assert row is not None
        return self._row(row)

    def get(self, task_id: int) -> TaskRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._row(row)

    def status_counts(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def contract_distribution(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT role, jd_version, rubric_version, parser_version,
                       scoring_version, prompt_version, status, COUNT(*) AS count
                FROM tasks
                GROUP BY role, jd_version, rubric_version, parser_version,
                         scoring_version, prompt_version, status
                ORDER BY count DESC, role, rubric_version, status
                """
            ).fetchall()
        return [dict(row) for row in rows]

    # Descriptive alias used by operational callers; the older name remains
    # useful to callers that only need the persisted contract histogram.
    queue_version_distribution = contract_distribution

    def mark_stale_contracts(
        self,
        active_contracts: dict[str, tuple[str, str, str, str, str]] | None = None,
        *,
        lease_id: str | None = None,
    ) -> int:
        contracts = active_contracts or ACTIVE_CONTRACTS
        predicate, parameters = _contract_predicate(contracts)
        message = (
            "task uses an older parser/prompt/scoring/rubric contract; "
            "re-enqueue the source to create a versioned replacement"
        )
        with self._connection() as connection:
            if lease_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if not self._lease_owned(connection, lease_id):
                    raise WorkerLeaseLostError("worker lease is no longer active")
            rows = connection.execute(
                f"""
                SELECT task_id FROM tasks
                WHERE status IN ('queued', 'processing', 'retryable_failed')
                  AND model_completed = 0 AND NOT ({predicate})
                """,
                parameters,
            ).fetchall()
            if not rows:
                return 0
            now = _utc_now()
            connection.execute(
                f"""
                UPDATE tasks SET status = 'manual_review',
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE status IN ('queued', 'processing', 'retryable_failed')
                  AND model_completed = 0 AND NOT ({predicate})
                """,
                [STALE_CONTRACT_VERSION, message, now, *parameters],
            )
            for row in rows:
                self._insert_event(
                    connection, row["task_id"], "error", STALE_CONTRACT_VERSION
                )
        return len(rows)

    def claim_next(
        self,
        *,
        active_contracts: dict[str, tuple[str, str, str, str, str]] | None = None,
        lease_id: str | None = None,
    ) -> TaskRecord | None:
        contracts = active_contracts or ACTIVE_CONTRACTS
        predicate, parameters = _contract_predicate(contracts)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if lease_id is not None and not self._lease_owned(connection, lease_id):
                raise WorkerLeaseLostError("worker lease is no longer active")
            row = connection.execute(
                f"""
                SELECT * FROM tasks
                WHERE status IN ('queued', 'retryable_failed') AND model_completed = 0
                ORDER BY CASE WHEN ({predicate}) THEN 0 ELSE 1 END,
                         CASE WHEN status = 'queued' THEN 0 ELSE 1 END,
                         task_id
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            now = _utc_now()
            connection.execute(
                """
                UPDATE tasks SET status = 'processing', claimed_at = ?, updated_at = ?
                WHERE task_id = ? AND status IN ('queued', 'retryable_failed')
                """,
                (now, now, row["task_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)
            ).fetchone()
        assert updated is not None
        return self._row(updated)

    def requeue_stale(
        self, *, older_than_seconds: int = 900, lease_id: str | None = None
    ) -> int:
        cutoff = (datetime.now(UTC) - timedelta(seconds=older_than_seconds)).isoformat()
        message = "worker stopped while provider completion state was unknown"
        with self._connection() as connection:
            if lease_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if not self._lease_owned(connection, lease_id):
                    raise WorkerLeaseLostError("worker lease is no longer active")
            rows = connection.execute(
                """
                SELECT task_id FROM tasks
                WHERE status = 'processing' AND model_completed = 0 AND claimed_at < ?
                """,
                (cutoff,),
            ).fetchall()
            if not rows:
                return 0
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'manual_review',
                    error_code = 'WORKER_INTERRUPTED_AMBIGUOUS',
                    error_message = ?, updated_at = ?
                WHERE status = 'processing' AND model_completed = 0 AND claimed_at < ?
                """,
                (message, _utc_now(), cutoff),
            )
            for row in rows:
                self._insert_event(
                    connection,
                    row["task_id"],
                    "error",
                    "WORKER_INTERRUPTED_AMBIGUOUS",
                )
        return cursor.rowcount

    def mark_manual_review(
        self,
        task_id: int,
        *,
        code: str,
        message: str,
        model_completed: bool = False,
        api_response_id: str | None = None,
        lease_id: str | None = None,
    ) -> None:
        safe_message = _safe_diagnostic(message)
        with self._connection() as connection:
            if lease_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if not self._lease_owned(connection, lease_id):
                    raise WorkerLeaseLostError("worker lease is no longer active")
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'manual_review', model_completed = ?,
                    api_response_id = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    int(model_completed),
                    api_response_id,
                    code,
                    safe_message,
                    _utc_now(),
                    task_id,
                ),
            )
            if cursor.rowcount:
                self._insert_event(connection, task_id, "error", code)

    def start_model_attempt(self, task_id: int, *, lease_id: str | None = None) -> None:
        with self._connection() as connection:
            if lease_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if not self._lease_owned(connection, lease_id):
                    raise WorkerLeaseLostError("worker lease is no longer active")
            cursor = connection.execute(
                """
                UPDATE tasks SET attempt_count = attempt_count + 1, updated_at = ?
                WHERE task_id = ? AND status = 'processing' AND model_completed = 0
                    AND attempt_count < 3
                """,
                (_utc_now(), task_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("model attempt limit reached or task is not processing")

    def mark_retryable_failure(
        self,
        task_id: int,
        *,
        code: str,
        message: str,
        lease_id: str | None = None,
    ) -> None:
        safe_message = _safe_diagnostic(message)
        with self._connection() as connection:
            if lease_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if not self._lease_owned(connection, lease_id):
                    raise WorkerLeaseLostError("worker lease is no longer active")
            cursor = connection.execute(
                """
                UPDATE tasks SET status = CASE
                        WHEN attempt_count >= 3 THEN 'manual_review'
                        ELSE 'retryable_failed'
                    END,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE task_id = ? AND model_completed = 0
                """,
                (code, safe_message, _utc_now(), task_id),
            )
            if cursor.rowcount:
                self._insert_event(connection, task_id, "error", code)

    def mark_succeeded(
        self,
        task_id: int,
        *,
        result: dict[str, Any],
        api_response_id: str | None,
        usage: dict[str, Any] | None,
        lease_id: str | None = None,
    ) -> None:
        with self._connection() as connection:
            if lease_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if not self._lease_owned(connection, lease_id):
                    raise WorkerLeaseLostError("worker lease is no longer active")
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'succeeded', model_completed = 1,
                    api_response_id = ?, usage_json = ?, result_json = ?,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE task_id = ? AND model_completed = 0
                """,
                (
                    api_response_id,
                    json.dumps(usage or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    _utc_now(),
                    task_id,
                ),
            )
            if cursor.rowcount:
                self._insert_event(connection, task_id, "succeeded")
        if cursor.rowcount != 1:
            raise RuntimeError("task already has a completed model response")

    def result(self, task_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row["result_json"]) if row["result_json"] else None

    def retry_failed(self, task_id: int | None = None) -> int:
        parameters: list[Any] = [_utc_now()]
        task_filter = ""
        if task_id is not None:
            task_filter = " AND task_id = ?"
            parameters.append(task_id)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks SET status = 'queued', attempt_count = 0,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE model_completed = 0
                    AND error_code IN ('MODEL_RETRYABLE', 'PROVIDER_AUTH_FAILED',
                                       'PROVIDER_RATE_LIMITED')
                    {task_filter}
                """,
                parameters,
            )
        return cursor.rowcount

    def successful_results(self) -> list[tuple[TaskRecord, dict[str, Any]]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status = 'succeeded' ORDER BY task_id"
            ).fetchall()
        return [(self._row(row), json.loads(row["result_json"])) for row in rows]

    def manual_review_tasks(self) -> list[TaskRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status = 'manual_review' ORDER BY task_id"
            ).fetchall()
        return [self._row(row) for row in rows]

    def record_watch_event(self, code: str, count: int = 1) -> None:
        if code not in WATCH_EVENT_CODES:
            raise ValueError(f"unsupported watch event code: {code}")
        if count < 1:
            return
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO watch_counters (code, count, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET count = count + excluded.count,
                    updated_at = excluded.updated_at
                """,
                (code, count, _utc_now()),
            )

    def watch_event_counts(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT code, count FROM watch_counters ORDER BY code"
            ).fetchall()
        return {row["code"]: row["count"] for row in rows}

    @staticmethod
    def _lease_owned(connection: sqlite3.Connection, worker_id: str) -> bool:
        row = connection.execute(
            "SELECT state, expires_at FROM worker_leases WHERE lease_name = 'screening-worker' "
            "AND worker_id = ?",
            (worker_id,),
        ).fetchone()
        if row is None or row["state"] != "running":
            return False
        expiry = _parse_datetime(row["expires_at"])
        return expiry is not None and expiry > datetime.now(UTC)

    def acquire_worker(
        self,
        *,
        active_contracts: dict[str, tuple[str, str, str, str, str]] | None = None,
        model_configured: bool = False,
        lease_seconds: int = 300,
        worker_id: str | None = None,
    ) -> str:
        if lease_seconds < 5:
            raise ValueError("lease_seconds must be at least 5")
        contracts = active_contracts or ACTIVE_CONTRACTS
        worker_id = worker_id or uuid.uuid4().hex
        now = datetime.now(UTC)
        now_text = now.isoformat()
        expires_text = (now + timedelta(seconds=lease_seconds)).isoformat()
        payload = json.dumps(
            {role: values[1] for role, values in sorted(contracts.items())},
            ensure_ascii=True,
            sort_keys=True,
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM worker_leases WHERE lease_name = 'screening-worker'"
            ).fetchone()
            if current is not None:
                expiry = _parse_datetime(current["expires_at"])
                if (
                    current["state"] == "running"
                    and current["worker_id"] != worker_id
                    and expiry is not None
                    and expiry > now
                ):
                    raise WorkerAlreadyRunningError(
                        "another worker already holds the screening database lease"
                    )
                connection.execute(
                    """
                    UPDATE worker_leases SET worker_id = ?, pid = ?, host = ?, state = 'running',
                        acquired_at = ?, last_heartbeat_at = ?, expires_at = ?,
                        pause_until = NULL, pause_reason = NULL, model_configured = ?,
                        lease_seconds = ?, active_parser_version = ?, active_prompt_version = ?,
                        active_scoring_version = ?, active_rubric_versions_json = ?
                    WHERE lease_name = 'screening-worker'
                    """,
                    (
                        worker_id,
                        os.getpid(),
                        socket.gethostname(),
                        now_text,
                        now_text,
                        expires_text,
                        int(model_configured),
                        lease_seconds,
                        PARSER_VERSION,
                        PROMPT_VERSION,
                        SCORING_VERSION,
                        payload,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO worker_leases (
                        lease_name, worker_id, pid, host, state, acquired_at,
                        last_heartbeat_at, expires_at, model_configured, lease_seconds,
                        active_parser_version, active_prompt_version,
                        active_scoring_version, active_rubric_versions_json
                    ) VALUES ('screening-worker', ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worker_id,
                        os.getpid(),
                        socket.gethostname(),
                        now_text,
                        now_text,
                        expires_text,
                        int(model_configured),
                        lease_seconds,
                        PARSER_VERSION,
                        PROMPT_VERSION,
                        SCORING_VERSION,
                        payload,
                    ),
                )
        return worker_id

    def heartbeat(self, worker_id: str, *, success: bool = False) -> None:
        now = datetime.now(UTC)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lease_seconds FROM worker_leases WHERE lease_name = 'screening-worker' "
                "AND worker_id = ? AND state = 'running'",
                (worker_id,),
            ).fetchone()
            if row is None:
                raise WorkerLeaseLostError("worker lease is no longer active")
            expires = (now + timedelta(seconds=max(5, row["lease_seconds"]))).isoformat()
            if success:
                connection.execute(
                    """
                    UPDATE worker_leases SET last_heartbeat_at = ?, expires_at = ?,
                        last_success_at = ?
                    WHERE lease_name = 'screening-worker' AND worker_id = ? AND state = 'running'
                    """,
                    (now.isoformat(), expires, now.isoformat(), worker_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE worker_leases SET last_heartbeat_at = ?, expires_at = ?
                    WHERE lease_name = 'screening-worker' AND worker_id = ? AND state = 'running'
                    """,
                    (now.isoformat(), expires, worker_id),
                )

    def set_worker_pause(
        self, worker_id: str, *, reason: str, pause_seconds: int
    ) -> None:
        if pause_seconds < 1:
            raise ValueError("pause_seconds must be positive")
        pause_until = (datetime.now(UTC) + timedelta(seconds=pause_seconds)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_leases SET pause_until = ?, pause_reason = ?
                WHERE lease_name = 'screening-worker' AND worker_id = ? AND state = 'running'
                """,
                (pause_until, reason[:120], worker_id),
            )
        if cursor.rowcount != 1:
            raise WorkerLeaseLostError("worker lease is no longer active")

    def clear_worker_pause(self, worker_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE worker_leases SET pause_until = NULL, pause_reason = NULL
                WHERE lease_name = 'screening-worker' AND worker_id = ? AND state = 'running'
                """,
                (worker_id,),
            )

    def release_worker(self, worker_id: str) -> None:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE worker_leases SET state = 'stopped', last_heartbeat_at = ?,
                    expires_at = ?, pause_until = NULL, pause_reason = NULL
                WHERE lease_name = 'screening-worker' AND worker_id = ?
                """,
                (now, now, worker_id),
            )

    def import_human_reviews(self, csv_path: str | Path) -> int:
        from .calibration import read_reviews_csv

        reviews = read_reviews_csv(csv_path)
        imported = 0
        with self._connection() as connection:
            for review in reviews:
                task_row = None
                if review["task_id"] is not None:
                    task_row = connection.execute(
                        "SELECT * FROM tasks WHERE task_id = ?", (review["task_id"],)
                    ).fetchone()
                    if task_row is None:
                        raise ValueError(f"人工结果引用了不存在的 task_id: {review['task_id']}")
                elif review["candidate_id"]:
                    task_row = connection.execute(
                        "SELECT * FROM tasks WHERE candidate_id = ? ORDER BY task_id DESC LIMIT 1",
                        (review["candidate_id"],),
                    ).fetchone()

                task_id = task_row["task_id"] if task_row is not None else review["task_id"]
                candidate_id = (
                    task_row["candidate_id"]
                    if task_row is not None
                    else review["candidate_id"]
                )
                role = task_row["role"] if task_row is not None else review["role"]
                rubric_version = (
                    task_row["rubric_version"]
                    if task_row is not None
                    else review["rubric_version"]
                )
                model_recommendation = review["model_recommendation"]
                if task_row is not None and model_recommendation == "unknown":
                    try:
                        result = json.loads(task_row["result_json"] or "{}")
                    except json.JSONDecodeError:
                        result = {}
                    record = result.get("screening_record", {})
                    model_recommendation = record.get(
                        "model_recommendation", result.get("recommendation")
                    )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO human_reviews (
                        source_row_key, task_id, candidate_id, role, rubric_version,
                        model_recommendation, human_conclusion, reason_category,
                        criterion_id, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review["source_row_key"],
                        task_id,
                        candidate_id,
                        role,
                        rubric_version,
                        model_recommendation,
                        review["human_conclusion"],
                        review["reason_category"],
                        review["criterion_id"],
                        _utc_now(),
                    ),
                )
                imported += int(cursor.rowcount == 1)
        return imported

    def calibration_report(self, *, minimum_sample_size: int = 10) -> dict[str, Any]:
        from .calibration import build_calibration_report

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT hr.*, t.result_json
                FROM human_reviews AS hr
                LEFT JOIN tasks AS t ON t.task_id = hr.task_id
                ORDER BY hr.review_id
                """
            ).fetchall()
        normalized: list[dict[str, Any]] = []
        for row in rows:
            result: dict[str, Any] = {}
            if row["result_json"]:
                try:
                    result = json.loads(row["result_json"])
                except json.JSONDecodeError:
                    result = {}
            normalized.append(
                {
                    "model_recommendation": row["model_recommendation"],
                    "human_conclusion": row["human_conclusion"],
                    "reason_category": row["reason_category"],
                    "criterion_id": row["criterion_id"],
                    "role": row["role"],
                    "rubric_version": row["rubric_version"],
                    "result": result,
                }
            )
        return build_calibration_report(
            normalized, minimum_sample_size=minimum_sample_size
        )

    def health_snapshot(
        self,
        *,
        active_contracts: dict[str, tuple[str, str, str, str, str]] | None = None,
        processing_threshold_seconds: int = 900,
        now: datetime | None = None,
        model_configured: bool | None = None,
    ) -> dict[str, Any]:
        if processing_threshold_seconds < 1:
            raise ValueError("processing_threshold_seconds must be positive")
        contracts = active_contracts or ACTIVE_CONTRACTS
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now - timedelta(seconds=24 * 60 * 60)
        with self._connection() as connection:
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
            lease = connection.execute(
                "SELECT * FROM worker_leases WHERE lease_name = 'screening-worker'"
            ).fetchone()
            error_rows = connection.execute(
                """
                SELECT error_code, COUNT(*) AS count FROM tasks
                WHERE error_code IS NOT NULL GROUP BY error_code ORDER BY error_code
                """
            ).fetchall()
            current_success_row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM tasks
                WHERE status = 'succeeded' AND updated_at >= ?
                """,
                (cutoff.isoformat(),),
            ).fetchone()
            current_error_row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM tasks
                WHERE error_code IS NOT NULL AND updated_at >= ?
                """,
                (cutoff.isoformat(),),
            ).fetchone()
            event_rows = connection.execute(
                """
                SELECT event_type, COUNT(*) AS count FROM task_events
                WHERE occurred_at >= ? GROUP BY event_type
                """,
                (cutoff.isoformat(),),
            ).fetchall()
            processing_rows = connection.execute(
                """
                SELECT task_id, role, claimed_at FROM tasks WHERE status = 'processing'
                """
            ).fetchall()
        counts = {
            status: 0
            for status in (
                "queued",
                "processing",
                "succeeded",
                "manual_review",
                "retryable_failed",
            )
        }
        counts.update({row["status"]: row["count"] for row in status_rows})

        event_counts = {row["event_type"]: row["count"] for row in event_rows}
        success_24h = max(
            int(current_success_row["count"]), int(event_counts.get("succeeded", 0))
        )
        errors_24h = max(
            int(current_error_row["count"]), int(event_counts.get("error", 0))
        )

        overdue: list[dict[str, Any]] = []
        processing_cutoff = now - timedelta(seconds=processing_threshold_seconds)
        for row in processing_rows:
            claimed = _parse_datetime(row["claimed_at"])
            if claimed is not None and claimed < processing_cutoff:
                overdue.append(
                    {
                        "task_id": row["task_id"],
                        "role": row["role"],
                        "claimed_at": row["claimed_at"],
                    }
                )

        last_success_at: str | None = None
        if lease is not None:
            last_success_at = lease["last_success_at"]
        if last_success_at is None:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT MAX(updated_at) AS value FROM tasks WHERE status = 'succeeded'"
                ).fetchone()
            last_success_at = row["value"] if row is not None else None

        holds_heartbeat = False
        worker_status = "never_started"
        last_heartbeat_at = None
        pause_until = None
        pause_reason = None
        if lease is not None:
            last_heartbeat_at = lease["last_heartbeat_at"]
            pause_until = lease["pause_until"]
            pause_reason = lease["pause_reason"]
            expiry = _parse_datetime(lease["expires_at"])
            heartbeat_at = _parse_datetime(lease["last_heartbeat_at"])
            heartbeat_fresh = heartbeat_at is not None and (
                heartbeat_at + timedelta(seconds=max(5, lease["lease_seconds"])) > now
            )
            holds_heartbeat = (
                lease["state"] == "running"
                and expiry is not None
                and expiry > now
                and heartbeat_fresh
            )
            if holds_heartbeat:
                worker_status = "running"
            elif lease["state"] == "stopped":
                worker_status = "stopped"
            else:
                worker_status = "stale"

        if model_configured is None:
            if lease is not None and lease["state"] == "running":
                model_configured = bool(lease["model_configured"])
            else:
                model_configured = bool(os.environ.get("MINIMAX_API_KEY"))
        pause_active = bool(
            pause_until and (_parse_datetime(pause_until) or now) > now
        )
        if not model_configured:
            model_status = "NOT_CONFIGURED"
        elif pause_active:
            model_status = "PAUSED"
        elif worker_status == "stale":
            model_status = "WORKER_STALE"
        elif holds_heartbeat:
            model_status = "READY"
        else:
            model_status = "IDLE"

        active_contract = _contract_health_payload(contracts)
        snapshot = {
            "worker_holds_heartbeat": holds_heartbeat,
            "holds_heartbeat": holds_heartbeat,
            "worker_status": worker_status,
            "last_heartbeat_at": last_heartbeat_at,
            "last_success_at": last_success_at,
            "active_contract": active_contract,
            "active_parser_version": active_contract["parser_version"],
            "active_prompt_version": active_contract["prompt_version"],
            "active_scoring_version": active_contract["scoring_version"],
            "active_rubric_versions": active_contract["rubric_versions"],
            "model_configured": bool(model_configured),
            "model_status": model_status,
            "pause_until": pause_until,
            "pause_reason": pause_reason,
            "queue": counts,
            **counts,
            "last_24h": {
                "succeeded": success_24h,
                "errors": errors_24h,
                "success_count": success_24h,
                "error_count": errors_24h,
            },
            "last_24h_success_count": success_24h,
            "last_24h_error_count": errors_24h,
            "error_code_counts": {
                row["error_code"]: row["count"] for row in error_rows
            },
            "processing_over_threshold": bool(overdue),
            "processing_over_threshold_count": len(overdue),
            "processing_over_threshold_tasks": overdue,
            "watch_skip_counts": self.watch_event_counts(),
            "contract_distribution": self.contract_distribution(),
        }
        return snapshot

    health = health_snapshot

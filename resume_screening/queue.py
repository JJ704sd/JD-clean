"""SQLite-backed idempotent task queue."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cleaning import PARSER_VERSION

SCORING_VERSION = "evidence-score-2026-09-01-v2"
PROMPT_VERSION = "resume-screening-prompt-2026-09-01-v4"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


class TaskStore:
    def __init__(self, database: str | Path):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
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

    def enqueue(self, spec: TaskSpec) -> TaskRecord:
        source = spec.source_path.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        source_sha256 = _file_sha256(source)
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

    def claim_next(self) -> TaskRecord | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('queued', 'retryable_failed') AND model_completed = 0
                ORDER BY task_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = _utc_now()
            connection.execute(
                """
                UPDATE tasks SET status = 'processing', claimed_at = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (now, now, row["task_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (row["task_id"],)
            ).fetchone()
            connection.commit()
        assert updated is not None
        return self._row(updated)

    def requeue_stale(self, *, older_than_seconds: int = 900) -> int:
        cutoff = (datetime.now(UTC) - timedelta(seconds=older_than_seconds)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = 'manual_review',
                    error_code = 'WORKER_INTERRUPTED_AMBIGUOUS',
                    error_message = 'worker stopped while provider completion state was unknown',
                    updated_at = ?
                WHERE status = 'processing' AND model_completed = 0 AND claimed_at < ?
                """,
                (_utc_now(), cutoff),
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
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE tasks SET status = 'manual_review', model_completed = ?,
                    api_response_id = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    int(model_completed),
                    api_response_id,
                    code,
                    message[:1000],
                    _utc_now(),
                    task_id,
                ),
            )

    def start_model_attempt(self, task_id: int) -> None:
        with self._connection() as connection:
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

    def mark_retryable_failure(self, task_id: int, *, code: str, message: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE tasks SET status = CASE
                        WHEN attempt_count >= 3 THEN 'manual_review'
                        ELSE 'retryable_failed'
                    END,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE task_id = ? AND model_completed = 0
                """,
                (code, message[:1000], _utc_now(), task_id),
            )

    def mark_succeeded(
        self,
        task_id: int,
        *,
        result: dict[str, Any],
        api_response_id: str | None,
        usage: dict[str, Any] | None,
    ) -> None:
        with self._connection() as connection:
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
                    AND error_code = 'MODEL_RETRYABLE'
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

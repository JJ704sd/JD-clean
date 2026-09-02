"""Handoff from the Feishu document gate to the existing screening queue.

The bridge intentionally does not call a model.  It publishes a validated,
read-back-complete source into :class:`TaskStore`; the existing screening
worker remains responsible for provider access, leases, retries, validation,
deterministic scoring, and human-review routing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .queue import TaskRecord, TaskSpec, TaskStore
from .versions import ROLE_VERSIONS

DEFAULT_SCREENING_ROLE = "senior-fullstack-engineer"
DEFAULT_SCREENING_MODEL = "MiniMax-M3"
READBACK_REQUIRED_CODE = "SCREENING_READBACK_REQUIRED"


class ScreeningHandoffError(RuntimeError):
    """The local screening queue could not accept a completed document gate."""


def _readback_is_verified(
    *,
    document_url: str | None,
    readback_nonempty: bool | None,
    readback_chars: int | None,
) -> bool:
    if not document_url:
        return False
    if readback_nonempty is True:
        return True
    try:
        return int(readback_chars or 0) > 0
    except (TypeError, ValueError):
        return False


class ScreeningQueueBridge:
    """Publish read-back-complete resumes to the existing SQLite queue.

    The public operation is idempotent because ``TaskStore.enqueue`` keys a
    task by source hash plus the active role/model/contract versions.  Calling
    it again for the same source therefore returns the existing task instead
    of creating another model request.
    """

    def __init__(
        self,
        *,
        database: str | Path,
        output_directory: str | Path,
        role: str = DEFAULT_SCREENING_ROLE,
        model: str = DEFAULT_SCREENING_MODEL,
        store: TaskStore | None = None,
    ) -> None:
        if role not in ROLE_VERSIONS:
            raise ValueError(f"unsupported screening role: {role!r}")
        if not model.strip():
            raise ValueError("screening model cannot be empty")
        self.database = Path(database).expanduser().resolve()
        self.output_directory = Path(output_directory).expanduser().resolve()
        self.role = role
        self.model = model
        try:
            self.store = store or TaskStore(self.database)
        except (OSError, sqlite3.Error) as exc:
            raise ScreeningHandoffError(f"screening queue unavailable: {exc}") from exc

    def _task_payload(self, task: TaskRecord) -> dict[str, Any]:
        return {
            "status": task.status,
            "queue_status": task.status,
            "task_id": task.task_id,
            "candidate_id": task.candidate_id,
            "role": task.role,
            "jd_version": task.jd_version,
            "rubric_version": task.rubric_version,
            "model": task.model,
            "attempt_count": task.attempt_count,
            "model_completed": task.model_completed,
            "error_code": task.error_code,
            "error": task.error_message,
            "database": str(self.database),
            "output_directory": str(self.output_directory),
        }

    def enqueue_after_readback(
        self,
        *,
        source_path: str | Path,
        source_sha256: str,
        candidate_id: str,
        candidate_name: str | None,
        document_url: str | None,
        readback_nonempty: bool | None,
        readback_chars: int | None,
    ) -> dict[str, Any]:
        """Enqueue only after the Feishu document has passed readback QA."""

        if not _readback_is_verified(
            document_url=document_url,
            readback_nonempty=readback_nonempty,
            readback_chars=readback_chars,
        ):
            return {
                "status": "blocked",
                "error_code": READBACK_REQUIRED_CODE,
                "error": "screening requires a non-empty Feishu document readback",
            }

        jd_version, rubric_version = ROLE_VERSIONS[self.role]
        try:
            task = self.store.enqueue(
                TaskSpec(
                    source_path=Path(source_path),
                    source_sha256=source_sha256,
                    candidate_id=candidate_id,
                    candidate_name=candidate_name,
                    role=self.role,
                    jd_version=jd_version,
                    rubric_version=rubric_version,
                    model=self.model,
                )
            )
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            raise ScreeningHandoffError(str(exc)) from exc
        return self._task_payload(task)

    def task_status(self, task_id: int) -> dict[str, Any]:
        """Return the current queue state for a previously handed-off task."""

        try:
            task = self.store.get(int(task_id))
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
            raise ScreeningHandoffError(f"screening task not found: {task_id}") from exc
        return self._task_payload(task)

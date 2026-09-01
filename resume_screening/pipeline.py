"""One-resume/one-completed-model-response screening orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

from .assembly import assemble_ai_product_manager_record, assemble_senior_record
from .cleaning import PARSER_VERSION, ResumeQualityError, clean_resume
from .contracts import validate_record
from .minimax import (
    AmbiguousModelError,
    ModelCallError,
    ModelResponse,
    RetryableModelError,
)
from .prompts import build_system_prompt
from .queue import PROMPT_VERSION, SCORING_VERSION, TaskRecord, TaskStore
from .rendering import render_conclusion
from .scoring import score_record


class ModelClient(Protocol):
    def analyze(self, **kwargs: object) -> ModelResponse: ...


def _safe_candidate_dir(candidate_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate_id).strip(".-")
    if not value:
        raise ValueError("candidate_id cannot be converted to a safe directory name")
    if value != candidate_id:
        suffix = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:8]
        value = f"{value}-{suffix}"
    return value[:120]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _parse_json_object(content: str) -> dict[str, Any]:
    value = content.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE
    )
    if fenced:
        value = fenced.group(1)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("model output must be a JSON object")
    return parsed


def _apply_authoritative_identity(record: dict[str, Any], task: TaskRecord) -> None:
    record["schema_version"] = "1.2"
    record["screening_record_id"] = f"sr-{task.task_id:08d}"
    record["candidate_id"] = task.candidate_id
    record["role"] = task.role
    record["jd_version"] = task.jd_version
    record["rubric_version"] = task.rubric_version
    record["screening_status"] = "non_final"
    if task.candidate_name:
        record["candidate_name"] = task.candidate_name
    else:
        record.pop("candidate_name", None)


class ScreeningPipeline:
    def __init__(
        self,
        *,
        store: TaskStore,
        client: ModelClient,
        output_root: str | Path,
        project_root: str | Path,
    ):
        self.store = store
        self.client = client
        self.output_root = Path(output_root).resolve()
        self.project_root = Path(project_root).resolve()

    def _output_dir(self, task: TaskRecord) -> Path:
        return self.output_root / _safe_candidate_dir(task.candidate_id)

    def process_next(self) -> TaskRecord | None:
        task = self.store.claim_next()
        if task is None:
            return None
        output_dir = self._output_dir(task)
        active_versions = (PARSER_VERSION, SCORING_VERSION, PROMPT_VERSION)
        task_versions = (
            task.parser_version,
            task.scoring_version,
            task.prompt_version,
        )
        if task_versions != active_versions:
            self.store.mark_manual_review(
                task.task_id,
                code="STALE_CONTRACT_VERSION",
                message=(
                    "task uses an older parser/prompt/scoring contract; "
                    "re-enqueue the source to create a versioned replacement"
                ),
            )
            return self.store.get(task.task_id)
        try:
            cleaned = clean_resume(
                task.source_path,
                candidate_id=task.candidate_id,
                candidate_name=task.candidate_name,
            )
        except ResumeQualityError as exc:
            self.store.mark_manual_review(task.task_id, code=exc.code, message=str(exc))
            return self.store.get(task.task_id)
        if cleaned.source_sha256 != task.source_sha256:
            self.store.mark_manual_review(
                task.task_id,
                code="SOURCE_CHANGED",
                message="source file changed after enqueue; enqueue the new file version",
            )
            return self.store.get(task.task_id)
        _atomic_write(output_dir / "resume.cleaned.md", cleaned.markdown)

        system_prompt = build_system_prompt(
            self.project_root,
            role=task.role,
            candidate_id=task.candidate_id,
            jd_version=task.jd_version,
            rubric_version=task.rubric_version,
            prompt_version=task.prompt_version,
        )
        self.store.start_model_attempt(task.task_id)
        try:
            response = self.client.analyze(
                system_prompt=system_prompt,
                resume_text=cleaned.model_text,
                model=task.model,
                idempotency_key=task.task_key,
            )
        except RetryableModelError as exc:
            self.store.mark_retryable_failure(
                task.task_id, code="MODEL_RETRYABLE", message=str(exc)
            )
            return self.store.get(task.task_id)
        except AmbiguousModelError as exc:
            self.store.mark_manual_review(
                task.task_id,
                code="MODEL_CALL_AMBIGUOUS",
                message=str(exc),
                model_completed=False,
            )
            return self.store.get(task.task_id)
        except ModelCallError as exc:
            self.store.mark_manual_review(
                task.task_id,
                code="MODEL_CALL_REJECTED",
                message=str(exc),
                model_completed=False,
            )
            return self.store.get(task.task_id)

        try:
            record = _parse_json_object(response.content)
            if task.role == "senior-fullstack-engineer":
                record = assemble_senior_record(
                    record,
                    screening_record_id=f"sr-{task.task_id:08d}",
                    candidate_id=task.candidate_id,
                    candidate_name=task.candidate_name,
                    jd_version=task.jd_version,
                    rubric_version=task.rubric_version,
                    prompt_version=task.prompt_version,
                    resume_text=cleaned.model_text,
                )
            elif task.role == "ai-product-manager":
                record = assemble_ai_product_manager_record(
                    record,
                    screening_record_id=f"sr-{task.task_id:08d}",
                    candidate_id=task.candidate_id,
                    candidate_name=task.candidate_name,
                    jd_version=task.jd_version,
                    rubric_version=task.rubric_version,
                )
            else:
                _apply_authoritative_identity(record, task)
            errors = validate_record(self.project_root, task.role, record)
            if errors:
                raise ValueError("; ".join(errors))
            scorecard = score_record(record).as_dict()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self.store.mark_manual_review(
                task.task_id,
                code="INVALID_MODEL_OUTPUT",
                message=str(exc),
                model_completed=True,
                api_response_id=response.response_id,
            )
            _atomic_write(
                output_dir / "manual-review.json",
                json.dumps(
                    {"code": "INVALID_MODEL_OUTPUT", "message": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            return self.store.get(task.task_id)

        envelope = {"screening_record": record, "scorecard": scorecard}
        conclusion = render_conclusion(record, scorecard)
        _atomic_write(
            output_dir / "screening.json",
            json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        _atomic_write(output_dir / "conclusion.md", conclusion)
        self.store.mark_succeeded(
            task.task_id,
            result=envelope,
            api_response_id=response.response_id,
            usage=response.usage,
        )
        return self.store.get(task.task_id)

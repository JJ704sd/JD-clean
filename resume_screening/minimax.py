"""Minimal MiniMax M3 HTTP client with explicit failure semantics."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_ENDPOINT = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
RETRYABLE_BUSINESS_CODES = {1000, 1001, 1002, 1013, 1024, 1033, 1041}


class ModelCallError(RuntimeError):
    """Base class for model transport and protocol failures."""


class RetryableModelError(ModelCallError):
    """The provider rejected the request before a completed response existed."""


class AmbiguousModelError(ModelCallError):
    """A request may have completed remotely and must not be retried automatically."""


@dataclass(frozen=True)
class ModelResponse:
    content: str
    response_id: str | None
    usage: dict[str, Any]


class MiniMaxClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        timeout_seconds: float = 180,
    ):
        self.api_key = api_key or os.environ.get("MINIMAX_API_KEY")
        self.endpoint = (
            endpoint or os.environ.get("MINIMAX_API_ENDPOINT") or DEFAULT_ENDPOINT
        )
        self.timeout_seconds = timeout_seconds

    def analyze(
        self,
        *,
        system_prompt: str,
        resume_text: str,
        model: str = "MiniMax-M3",
        idempotency_key: str | None = None,
    ) -> ModelResponse:
        if not self.api_key:
            raise RetryableModelError("MINIMAX_API_KEY is not configured")
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "name": "resume-screening-policy",
                    "content": system_prompt,
                },
                {"role": "user", "name": "resume", "content": resume_text},
            ],
            "stream": False,
            "max_completion_tokens": 20000,
            "temperature": 1.0,
            "top_p": 0.95,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["X-Request-ID"] = idempotency_key
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                raise RetryableModelError(f"MiniMax HTTP {exc.code}: {detail}") from exc
            raise ModelCallError(f"MiniMax HTTP {exc.code}: {detail}") from exc
        except TimeoutError as exc:
            raise AmbiguousModelError(
                "MiniMax request timed out after transmission"
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise AmbiguousModelError(
                    "MiniMax request timed out after transmission"
                ) from exc
            raise RetryableModelError(
                f"MiniMax connection failed: {exc.reason}"
            ) from exc
        try:
            data = json.loads(body)
            base_response = data.get("base_resp")
            if isinstance(base_response, dict):
                status_code = base_response.get("status_code")
                if isinstance(status_code, int) and status_code != 0:
                    status_message = str(
                        base_response.get("status_msg") or "provider rejected request"
                    )
                    status_message = " ".join(status_message.split())[:300]
                    error = f"MiniMax API {status_code}: {status_message}"
                    if status_code in RETRYABLE_BUSINESS_CODES:
                        raise RetryableModelError(error)
                    raise ModelCallError(error)
            message = data["choices"][0]["message"]
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty message content")
        except ModelCallError:
            raise
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise AmbiguousModelError(
                "MiniMax returned an invalid completed response envelope"
            ) from exc
        return ModelResponse(
            content=content,
            response_id=data.get("id"),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
        )

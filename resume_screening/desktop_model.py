"""Explicit desktop model configuration and immutable, isolated AI runs."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .desktop_store import ReviewStore, atomic_text, now, resource_root
from .minimax import (
    AmbiguousModelError,
    ModelCallError,
    ModelResponse,
    ProviderAuthError,
    ProviderRateLimitError,
)
from .pipeline import ScreeningPipeline
from .queue import TaskSpec, TaskStore
from .versions import ROLE_VERSIONS


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "openai-compatible"
    base_url: str = ""
    model: str = ""

    def _validated_base(self):
        base = self.base_url.strip().rstrip("/")
        parsed = urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("请填写不含凭据、查询参数或片段的 HTTPS Base URL")
        return base

    @property
    def endpoint(self):
        if self.provider not in ("openai-compatible", "minimax"):
            raise ValueError("未知模型协议")
        base = self._validated_base()
        if not self.model.strip():
            raise ValueError("请填写模型名称")
        suffix = (
            "/chat/completions"
            if self.provider == "openai-compatible"
            else "/text/chatcompletion_v2"
        )
        return base if base.endswith(suffix) else base + suffix

    @property
    def models_endpoint(self):
        """Return the conventional model-list endpoint for this provider."""

        if self.provider not in ("openai-compatible", "minimax"):
            raise ValueError("未知模型协议")
        base = self._validated_base()
        for suffix in ("/chat/completions", "/text/chatcompletion_v2"):
            if base.casefold().endswith(suffix.casefold()):
                base = base[: -len(suffix)]
                break
        return base.rstrip("/") + "/models"

    @property
    def identity(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()

    @property
    def credential_identity(self):
        """Stable OS-keychain identity shared by models on one endpoint."""

        return hashlib.sha256(
            json.dumps(
                {
                    "provider": self.provider,
                    "base_url": self.base_url.strip().rstrip("/"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class AnalysisResult:
    """Outcome of one desktop run, including a durable queue error code."""

    status: str
    folder: Path
    error_code: str | None = None

    def __iter__(self):
        # Preserve the original ``status, folder = run_analysis(...)`` API.
        yield self.status
        yield self.folder


def credential_backend():
    # Select OS stores explicitly; never silently fall back to plaintext backends.
    import sys

    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    raise ValueError("当前系统尚未支持安全凭据保存")


def save_config(root: Path, config: ModelConfig, key: str):
    _ = config.endpoint
    backend = credential_backend()
    value = key.strip()
    if value:
        backend.set_password("ResumeDesk", config.credential_identity, value)
    else:
        value = _saved_credential(backend, config)
    if not value:
        raise ValueError("请填写 API Key")
    atomic_text(
        root / "model.json", json.dumps(asdict(config), ensure_ascii=False, indent=2)
    )


def load_config(root: Path):
    path = root / "model.json"
    return (
        ModelConfig(**json.loads(path.read_text(encoding="utf-8")))
        if path.exists()
        else ModelConfig()
    )


def configured_client(config, key=""):
    _ = config.endpoint
    value = key.strip() or _saved_credential(credential_backend(), config)
    if not value:
        raise ValueError("未配置 API Key；可继续本地整理")
    return DesktopModelClient(config, value)


def list_models(config, key=""):
    """Fetch provider models using an explicit key or the saved OS credential."""

    value = key.strip() or _saved_credential(credential_backend(), config)
    if not value:
        raise ValueError("未配置 API Key；请先填写并保存或测试连接")
    return DesktopModelClient(config, value).list_models()


def saved_credential_available(config: ModelConfig) -> bool:
    """Return whether the OS credential store has a key for this config."""

    try:
        return bool(_saved_credential(credential_backend(), config))
    except Exception:  # noqa: BLE001 -- status display must not block local use.
        return False


def _saved_credential(backend, config: ModelConfig):
    """Read the endpoint-scoped key and migrate the prior model-scoped key."""

    value = backend.get_password("ResumeDesk", config.credential_identity)
    if value:
        return value
    legacy = backend.get_password("ResumeDesk", config.identity)
    if legacy:
        try:
            backend.set_password("ResumeDesk", config.credential_identity, legacy)
        except Exception:  # noqa: BLE001 -- legacy read still remains usable.
            return legacy
    return legacy


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DesktopModelClient:
    def __init__(self, config: ModelConfig, key: str, *, opener=None):
        self.config = config
        self.endpoint = config.endpoint
        self.key = key
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def analyze(self, *, system_prompt, resume_text, model=None, idempotency_key=None):
        payload = {
            "model": model or self.config.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": resume_text},
            ],
        }
        headers = {
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["X-Request-ID"] = idempotency_key
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), headers=headers
        )
        try:
            with self.opener.open(request, timeout=180) as response:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise AmbiguousModelError("响应过大，请人工核对；不自动重发")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ProviderAuthError("认证失败，请检查密钥或权限") from None
            if exc.code == 429:
                raise ProviderRateLimitError(
                    "服务限流或额度不足，请检查账户后显式重试"
                ) from None
            if exc.code >= 500 or exc.code in (408, 409):
                raise AmbiguousModelError(
                    f"HTTP {exc.code}：远端完成状态待核对"
                ) from None
            raise ModelCallError(
                f"HTTP {exc.code}：请求被拒绝或重定向；未重发"
            ) from None
        except (TimeoutError, urllib.error.URLError, OSError):
            raise AmbiguousModelError(
                "网络异常：远端完成状态待核对，不自动重试"
            ) from None
        try:
            data = json.loads(body)
            if self.config.provider == "minimax":
                code = data.get("base_resp", {}).get("status_code", 0)
                if code in (1004, 1005):
                    raise ProviderAuthError("MiniMax 认证失败")
                if code in (1002, 1013):
                    raise ProviderRateLimitError("MiniMax 限流或额度不足")
                if code:
                    raise ModelCallError("MiniMax 拒绝请求，请检查配置")
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if (
                not isinstance(content, str)
                or not content.strip()
                or choice.get("finish_reason") == "length"
            ):
                raise ValueError()
            return ModelResponse(
                content=content, response_id=data.get("id"), usage=data.get("usage", {})
            )
        except (ValueError, KeyError, TypeError, IndexError):
            raise AmbiguousModelError(
                "模型响应为空、截断或格式无效；不自动补发修复请求"
            ) from None

    def list_models(self):
        """Fetch model IDs from a provider's conventional ``/models`` endpoint."""

        request = urllib.request.Request(
            self.config.models_endpoint,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self.key,
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("模型列表响应过大，请手动填写模型名称")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ValueError("模型列表认证失败，请检查 API Key") from None
            if exc.code == 404:
                raise ValueError("该 Base URL 未提供 /models 接口，请手动填写模型名称") from None
            if exc.code == 429:
                raise ValueError("模型列表请求被限流，请稍后重试") from None
            raise ValueError(f"模型列表请求失败（HTTP {exc.code}），请检查 Base URL") from None
        except (TimeoutError, urllib.error.URLError, OSError):
            raise ValueError("模型列表获取失败，请检查网络和 Base URL") from None
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise ValueError("模型列表响应不是有效 JSON，请手动填写模型名称") from None
        models = _extract_model_names(payload)
        if not models:
            raise ValueError("模型列表为空或格式不兼容，请手动填写模型名称")
        return models

    def test(self):
        response = self.analyze(
            system_prompt='只返回 JSON 对象 {"ok":true}',
            resume_text="这是不含个人信息的连接测试。",
        )
        try:
            valid = json.loads(response.content) == {"ok": True}
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("连接成功，但模型未按测试要求返回 JSON；请检查模型兼容性")
        return "连接和 JSON 测试通过；实际简历判断仍需人工校准"


def _extract_model_names(payload):
    """Extract model IDs from common OpenAI-compatible response shapes."""

    values = []
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "items", "result"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                values = candidate
                break
            if isinstance(candidate, dict):
                values = candidate.get("data") or candidate.get("models") or []
                if isinstance(values, list):
                    break
    names = []
    seen = set()
    for value in values:
        if isinstance(value, str):
            name = value.strip()
        elif isinstance(value, dict):
            name = next(
                (
                    str(value[key]).strip()
                    for key in ("id", "name", "model", "model_name")
                    if value.get(key)
                ),
                "",
            )
        else:
            name = ""
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


def run_analysis(store: ReviewStore, document_id, config: ModelConfig, client):
    record = store.get(document_id)
    if record["status"] not in ("待人工审阅", "人工审阅完成"):
        raise ValueError("材料尚未整理成功")
    run_id = uuid.uuid4().hex
    folder = store.root / "ai-runs" / run_id
    folder.mkdir(parents=True)
    snapshot = asdict(config)
    snapshot["config_identity"] = config.identity
    snapshot["role_versions"] = ROLE_VERSIONS[record["role"]]
    atomic_text(folder / "run.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
    with closing(store.connect()) as db, db:
        db.execute(
            "INSERT INTO ai_runs VALUES (?,?,?,?,?,?)",
            (run_id, document_id, json.dumps(snapshot), "处理中", str(folder), now()),
        )
    status = "请求状态待核对"
    try:
        tasks = TaskStore(folder / "queue.sqlite3")
        jd, rubric = ROLE_VERSIONS[record["role"]]
        from .metadata import infer_candidate_name

        tasks.enqueue(
            TaskSpec(
                source_path=Path(record["snapshot"]),
                candidate_id=document_id,
                candidate_name=infer_candidate_name(Path(record["name"])),
                role=record["role"],
                jd_version=jd,
                rubric_version=rubric,
                model=config.model,
            )
        )
        # Each immutable run owns a separate queue; the desktop process lock prevents a second consumer.
        result = ScreeningPipeline(
            store=tasks,
            client=client,
            output_root=folder / "outputs",
            project_root=resource_root(),
        ).process_next()
        status = result.status if result else "未开始"
        return AnalysisResult(
            status=status,
            folder=folder,
            error_code=result.error_code if result else None,
        )
    finally:
        with closing(store.connect()) as db, db:
            db.execute("UPDATE ai_runs SET status=? WHERE id=?", (status, run_id))

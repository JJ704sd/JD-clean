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

    @property
    def endpoint(self):
        if self.provider not in ("openai-compatible", "minimax"):
            raise ValueError("未知模型协议")
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
        if not self.model.strip():
            raise ValueError("请填写模型名称")
        suffix = (
            "/chat/completions"
            if self.provider == "openai-compatible"
            else "/text/chatcompletion_v2"
        )
        return base if base.endswith(suffix) else base + suffix

    @property
    def identity(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
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
    if key.strip():
        backend.set_password("ResumeDesk", config.identity, key.strip())
    elif not backend.get_password("ResumeDesk", config.identity):
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
    value = key.strip() or credential_backend().get_password(
        "ResumeDesk", config.identity
    )
    if not value:
        raise ValueError("未配置 API Key；可继续本地整理")
    return DesktopModelClient(config, value)


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

"""Local-only preparation and revisioned human review, separate from AI queues."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from posixpath import normpath

from .cleaning import PARSER_VERSION, SUPPORTED_SUFFIXES, clean_resume
from .metadata import infer_candidate_name, infer_role
from .prompts import ROLE_SKILL_FILES
from .versions import ROLE_VERSIONS

ROLES = {
    "ai-product-manager": "AI 产品经理",
    "senior-fullstack-engineer": "资深全栈工程师",
    "fullstack-development-intern": "全栈开发实习生",
}
DECISIONS = ("未审阅", "有证据支持", "不符合", "信息不足")
BACKUP_FORMAT_VERSION = 1
BACKUP_DATA_PATHS = ("reviews.sqlite3", "materials", "ai-runs", "last-import-report.txt")


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def data_root() -> Path:
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "ResumeDesk"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ResumeDesk"
    return Path.home() / ".local/share/ResumeDesk"


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(value, encoding="utf-8")
    temp.replace(path)


def csv_safe(value: object) -> str:
    text = str(value or "")
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@"))
        or text.startswith(("\t", "\r", "\n"))
        else text
    )


class ReviewStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "reviews.sqlite3"
        with closing(self.connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("审阅数据库版本较新，请使用对应版本应用")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
                    snapshot TEXT NOT NULL, role TEXT NOT NULL, versions TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL, opinion TEXT NOT NULL, evidence TEXT NOT NULL,
                    created TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id));
                CREATE TABLE IF NOT EXISTS ai_runs (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, config TEXT NOT NULL,
                    status TEXT NOT NULL, directory TEXT NOT NULL, created TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id));
                CREATE TABLE IF NOT EXISTS drafts (
                    document_id TEXT PRIMARY KEY, reviewer TEXT NOT NULL DEFAULT '',
                    opinion TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL,
                    updated TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id));
                PRAGMA user_version=1;
            """)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def list_documents(self):
        with closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM documents ORDER BY created DESC")
            ]

    def get(self, document_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            if row is None:
                raise ValueError("未找到材料")
            return dict(row)

    def prepare(self, source: Path, role: str | None):
        source = Path(source).resolve()
        if source.suffix.lower() not in SUPPORTED_SUFFIXES or not source.is_file():
            raise ValueError("不支持的文件或文件不存在")
        detected = infer_role(source)
        if role is None:
            role = detected
        if role not in ROLES:
            raise ValueError("未知岗位，请选择固定岗位后重新导入")
        if detected and detected != role:
            raise ValueError("文件名岗位与指定岗位冲突")
        payload = source.read_bytes()
        versions = json.dumps(
            {"parser": PARSER_VERSION, "role": ROLE_VERSIONS[role]}, sort_keys=True
        )
        identity = hashlib.sha256(
            payload + role.encode() + versions.encode()
        ).hexdigest()
        with closing(self.connect()) as db:
            existing = db.execute(
                "SELECT status FROM documents WHERE id=?", (identity,)
            ).fetchone()
        if existing and existing[0] != "解析失败":
            return self.get(identity)
        folder = self.root / "materials" / identity
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = folder / ("source" + source.suffix.lower())
        snapshot.write_bytes(payload)
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,'','',?)",
                (
                    identity,
                    source.name,
                    str(source),
                    str(snapshot),
                    role,
                    versions,
                    "整理中",
                    now(),
                ),
            )
            db.execute(
                "UPDATE documents SET status='整理中',error='' WHERE id=?", (identity,)
            )
        try:
            cleaned = clean_resume(
                snapshot,
                candidate_id=identity,
                candidate_name=infer_candidate_name(source),
            )
            atomic_text(folder / "resume.cleaned.md", cleaned.markdown)
            with closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE documents SET status='待人工审阅',content=? WHERE id=?",
                    (cleaned.markdown, identity),
                )
        except Exception as exc:  # noqa: BLE001 -- isolate third-party parsers and persist only the error type.
            # Never persist raw parser/provider exception text that could contain PII.
            with closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE documents SET status='解析失败',error=? WHERE id=?",
                    (
                        f"{type(exc).__name__}：请检查原文件是否损坏、加密或缺少有效文字",
                        identity,
                    ),
                )
        return self.get(identity)

    def recover(self):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE documents SET status='解析失败',error='上次整理中断，请重新导入' WHERE status='整理中'"
            )
            db.execute(
                "UPDATE ai_runs SET status='请求状态待核对' WHERE status='处理中'"
            )

    def save_review(self, document_id, reviewer, opinion, evidence):
        record = self.get(document_id)
        if record["status"] not in ("待人工审阅", "人工审阅完成"):
            raise ValueError("请先成功整理材料")
        if not reviewer.strip():
            raise ValueError("请填写审阅者名称（本机自填）")
        if not evidence or any(
            item.get("decision") not in DECISIONS for item in evidence
        ):
            raise ValueError("审阅项目或结论无效")
        for item in evidence:
            if (
                item["decision"] in ("有证据支持", "不符合")
                and not item.get("note", "").strip()
            ):
                raise ValueError("有证据支持/不符合的项目必须填写原文证据或人工备注")
        complete = all(item["decision"] != "未审阅" for item in evidence)
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO revisions(document_id,reviewer,opinion,evidence,created) VALUES (?,?,?,?,?)",
                (
                    document_id,
                    reviewer.strip(),
                    opinion,
                    json.dumps(evidence, ensure_ascii=False),
                    now(),
                ),
            )
            db.execute(
                "UPDATE documents SET status=? WHERE id=?",
                ("人工审阅完成" if complete else "待人工审阅", document_id),
            )
            db.execute("DELETE FROM drafts WHERE document_id=?", (document_id,))

    def save_draft(self, document_id, reviewer, opinion, evidence):
        self.get(document_id)
        if not isinstance(evidence, list):
            raise TypeError("审阅草稿格式无效")
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO drafts(document_id,reviewer,opinion,evidence,updated)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                   reviewer=excluded.reviewer, opinion=excluded.opinion,
                   evidence=excluded.evidence, updated=excluded.updated""",
                (
                    document_id,
                    str(reviewer or ""),
                    str(opinion or ""),
                    json.dumps(evidence, ensure_ascii=False),
                    now(),
                ),
            )

    def get_draft(self, document_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM drafts WHERE document_id=?", (document_id,)
            ).fetchone()
            return dict(row) if row else None

    def clear_draft(self, document_id):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM drafts WHERE document_id=?", (document_id,))

    def find_ai_runs(
        self, document_id, *, config_identity, provider, base_url, model
    ):
        """Return prior runs for the same document and model configuration."""
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM ai_runs WHERE document_id=? ORDER BY created DESC",
                (document_id,),
            ).fetchall()
        matches = []
        for row in rows:
            try:
                config = json.loads(row["config"])
            except (TypeError, ValueError):
                continue
            if not isinstance(config, dict):
                continue
            if config.get("config_identity") == config_identity or (
                config.get("provider") == provider
                and config.get("base_url") == base_url
                and config.get("model") == model
            ):
                matches.append(dict(row))
        return matches

    def backup(self, directory: Path):
        """Create a consistent, data-only backup; credentials and provider config stay out."""
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / (
            "ResumeDesk-backup-"
            + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            + "-"
            + hashlib.sha256(now().encode()).hexdigest()[:8]
            + ".zip"
        )
        with tempfile.NamedTemporaryFile(
            prefix="resumedesk-backup-", suffix=".sqlite3", dir=self.root.parent, delete=False
        ) as temporary:
            database_snapshot = Path(temporary.name)
        try:
            with closing(sqlite3.connect(self.database)) as source, closing(
                sqlite3.connect(database_snapshot)
            ) as target:
                source.backup(target)
                target.commit()
            manifest = {
                "format": BACKUP_FORMAT_VERSION,
                "schema": 1,
                "created": now(),
                "scope": list(BACKUP_DATA_PATHS),
                "credentials_included": False,
            }
            with zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_DEFLATED
            ) as output:
                output.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
                output.write(database_snapshot, "data/reviews.sqlite3")
                for relative in BACKUP_DATA_PATHS[1:]:
                    source_path = self.root / relative
                    if source_path.is_file():
                        output.write(source_path, "data/" + relative)
                    elif source_path.is_dir():
                        for child in sorted(source_path.rglob("*")):
                            if child.is_file() and not child.name.endswith(".tmp"):
                                output.write(
                                    child,
                                    "data/" + child.relative_to(self.root).as_posix(),
                                )
        finally:
            database_snapshot.unlink(missing_ok=True)
        return archive

    @staticmethod
    def _path_exists(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    @classmethod
    def _remove_path(cls, path: Path) -> None:
        if not cls._path_exists(path):
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    @staticmethod
    def _move_path(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    def _apply_staged_restore(
        self, staging: Path, swaps: list[tuple[Path, Path, bool]]
    ) -> None:
        """Replace data paths while retaining each old path for rollback."""
        rollback_root = staging / "rollback-data"
        paths = (
            Path("reviews.sqlite3-wal"),
            Path("reviews.sqlite3-shm"),
            *(Path(item) for item in BACKUP_DATA_PATHS),
        )
        for relative in paths:
            current = self.root / relative
            incoming = staging / "data" / relative
            old = rollback_root / relative
            had_old = self._path_exists(current)
            if had_old:
                self._move_path(current, old)
            swaps.append((current, old, had_old))
            if self._path_exists(incoming):
                self._move_path(incoming, current)

        # Backups made before drafts existed have schema v1 but no drafts table.
        with closing(self.connect()) as database, database:
            database.execute(
                """CREATE TABLE IF NOT EXISTS drafts (
                   document_id TEXT PRIMARY KEY, reviewer TEXT NOT NULL DEFAULT '',
                   opinion TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL,
                   updated TEXT NOT NULL,
                   FOREIGN KEY(document_id) REFERENCES documents(id))"""
            )

    def _rollback_restore(self, swaps: list[tuple[Path, Path, bool]]) -> None:
        failures = []
        for current, old, had_old in reversed(swaps):
            try:
                self._remove_path(current)
                if had_old and self._path_exists(old):
                    self._move_path(old, current)
            except Exception as exc:  # noqa: BLE001 -- collect rollback failures.
                failures.append(exc)
        if failures:
            raise OSError("自动回滚失败") from failures[0]

    def restore(self, archive: Path):
        """Restore a data-only backup after creating an automatic rollback archive."""
        archive = Path(archive).resolve()
        if not archive.is_file() or archive.suffix.lower() != ".zip":
            raise ValueError("请选择 ResumeDesk 备份 ZIP 文件")
        with tempfile.TemporaryDirectory(prefix="resumedesk-restore-", dir=self.root.parent) as temporary:
            staging = Path(temporary)
            try:
                with zipfile.ZipFile(archive) as source:
                    names = source.namelist()
                    for name in names:
                        safe_name = name.replace("\\", "/")
                        normalized = normpath(safe_name)
                        if (
                            normalized != safe_name
                            or normalized.startswith(("/", "../"))
                            or "/../" in normalized
                        ):
                            raise ValueError("备份文件包含不安全路径")
                        if normalized != "manifest.json" and not normalized.startswith("data/"):
                            raise ValueError("备份格式不受支持")
                    manifest = json.loads(source.read("manifest.json"))
                    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT_VERSION:
                        raise ValueError("备份版本与当前应用不兼容")
                    for item in source.infolist():
                        safe_name = item.filename.replace("\\", "/")
                        target = (staging / safe_name).resolve()
                        if not target.is_relative_to(staging.resolve()):
                            raise ValueError("备份文件包含不安全路径")
                        if item.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        mode = (item.external_attr >> 16) & 0o170000
                        if mode == 0o120000:
                            raise ValueError("备份文件包含不支持的链接")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with source.open(item) as input_stream, target.open("wb") as output:
                            shutil.copyfileobj(input_stream, output)
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                zipfile.BadZipFile,
            ) as exc:
                raise ValueError("备份文件损坏或格式不受支持") from exc
            staged_database = staging / "data" / "reviews.sqlite3"
            if not staged_database.is_file():
                raise ValueError("备份缺少审阅数据库")
            try:
                with closing(sqlite3.connect(staged_database)) as database:
                    schema = database.execute("PRAGMA user_version").fetchone()[0]
                    tables = {
                        row[0]
                        for row in database.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
            except sqlite3.DatabaseError as exc:
                raise ValueError("备份数据库损坏或格式不受支持") from exc
            if schema > 1 or not {"documents", "revisions", "ai_runs"}.issubset(tables):
                raise ValueError("备份数据库版本过高或缺少必要表")
            rollback = self.backup(self.root.parent)
            swaps: list[tuple[Path, Path, bool]] = []
            try:
                self._apply_staged_restore(staging, swaps)
            except Exception as exc:
                try:
                    self._rollback_restore(swaps)
                except Exception as rollback_exc:
                    raise ValueError(
                        f"恢复未完成且自动回滚失败；请使用回滚备份：{rollback.name}"
                    ) from rollback_exc
                raise ValueError(
                    f"恢复未完成；已自动回滚，回滚备份：{rollback.name}"
                ) from exc
        return rollback

    def latest_review(self, document_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM revisions WHERE document_id=? ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            return dict(row) if row else None

    def role_material(self, role):
        directory, files = ROLE_SKILL_FILES[role]
        names = [
            name
            for name in files
            if "rubric" in name or "jd-profile" in name or "role-profile" in name
        ]
        return "\n\n".join(
            (resource_root() / "skills" / directory / name).read_text(encoding="utf-8")
            for name in names
        )

    def export(self, directory: Path):
        # A dedicated timestamped directory prevents overwriting previous exports.
        import uuid

        folder = Path(directory) / (
            "manual-review-"
            + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )
        folder.mkdir(parents=True)
        with (folder / "manual-review.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "材料ID",
                    "文件名",
                    "岗位",
                    "状态",
                    "审阅者",
                    "人工意见",
                    "修订编号",
                    "规则版本",
                ]
            )
            for row in self.list_documents():
                review = self.latest_review(row["id"]) or {}
                writer.writerow(
                    [
                        csv_safe(value)
                        for value in (
                            row["id"],
                            row["name"],
                            ROLES[row["role"]],
                            row["status"],
                            review.get("reviewer"),
                            review.get("opinion"),
                            review.get("id"),
                            row["versions"],
                        )
                    ]
                )
                target = folder / row["id"]
                target.mkdir()
                atomic_text(target / "resume.cleaned.md", row["content"])
                lines = [
                    "# 人工审阅记录",
                    f"文件：{row['name']}",
                    f"状态：{row['status']}",
                    f"规则版本：{row['versions']}",
                    f"审阅者（本机自填）：{review.get('reviewer', '')}",
                    f"人工意见：{review.get('opinion', '')}",
                    "无 AI 评分。",
                ]
                for item in json.loads(review.get("evidence", "[]")):
                    lines.extend(
                        [
                            f"## {item['criterion']}：{item['decision']}",
                            item.get("note", ""),
                        ]
                    )
                atomic_text(target / "review.md", "\n\n".join(lines))
        return folder

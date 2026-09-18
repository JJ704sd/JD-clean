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
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from posixpath import normpath

from .cleaning import PARSER_VERSION, SUPPORTED_SUFFIXES, clean_resume
from .metadata import infer_candidate_name, infer_role
from .prompts import ROLE_SKILL_FILES
from .versions import AI_OUTPUT_CONTRACT_VERSION, DESKTOP_SCHEMA_VERSION, ROLE_VERSIONS

ROLES = {
    "ai-product-manager": "AI 产品经理",
    "senior-fullstack-engineer": "资深全栈工程师",
    "fullstack-development-intern": "全栈开发实习生",
    "operations-devops-engineer": "运维开发工程师",
}
DECISIONS = ("未审阅", "有证据支持", "不符合", "信息不足")
BACKUP_FORMAT_VERSION = 1
TRASH_RETENTION_DAYS = 7
BACKUP_DATA_PATHS = (
    "reviews.sqlite3",
    "materials",
    "ai-runs",
    "import-reports",
    "last-import-report.txt",
)
IMPORT_REASON_CODES = {
    "OK",
    "DUPLICATE",
    "UNSUPPORTED_FORMAT",
    "UNKNOWN_ROLE",
    "ROLE_CONFLICT",
    "READ_ERROR",
    "ENCODING_PENDING",
    "PARSE_ERROR",
}


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


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, dict):
        secret_names = {
            "key",
            "apikey",
            "api_key",
            "access_token",
            "authorization",
            "token",
            "password",
            "secret",
            "secret_key",
        }
        return any(
            str(key).casefold() in secret_names or _contains_secret_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item) for item in value)
    return False


class ReviewStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "reviews.sqlite3"
        version = self._read_schema_version()
        if version > DESKTOP_SCHEMA_VERSION:
            raise ValueError("审阅数据库版本较新，请使用对应版本应用")
        if version == 1:
            self._migrate_v1_to_v2()
        else:
            with closing(self.connect()) as db, db:
                self._create_schema_v2(db)
        self.purge_expired()

    def _read_schema_version(self) -> int:
        if not self.database.exists():
            return 0
        try:
            with closing(sqlite3.connect(self.database)) as db:
                return int(db.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise ValueError("审阅数据库损坏或格式不受支持") from exc

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _add_column(
        cls, db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        if column not in cls._columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @classmethod
    def _create_schema_v2(cls, db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
                snapshot TEXT NOT NULL, role TEXT NOT NULL, versions TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
                encoding_status TEXT NOT NULL DEFAULT 'ok',
                ai_status TEXT NOT NULL DEFAULT '未运行',
                ai_band TEXT, ai_recommendation TEXT,
                ai_primary_risk TEXT NOT NULL DEFAULT '',
                human_status TEXT NOT NULL DEFAULT '未审阅',
                deleted_at TEXT, purge_at TEXT, restored_at TEXT,
                import_batch_id TEXT, updated TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
                reviewer TEXT NOT NULL, opinion TEXT NOT NULL, evidence TEXT NOT NULL,
                created TEXT NOT NULL, revision_number INTEGER,
                role TEXT, versions TEXT, source_ai_run_id TEXT,
                created_by TEXT, FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS ai_runs (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, config TEXT NOT NULL,
                status TEXT NOT NULL, directory TEXT NOT NULL, created TEXT NOT NULL,
                batch_id TEXT, contract_version TEXT, input_redacted_sha256 TEXT,
                raw_response_path TEXT, normalized_output_path TEXT,
                validation_error_path TEXT, model_recommendation TEXT,
                ai_band TEXT, primary_risk TEXT NOT NULL DEFAULT '',
                updated TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                document_id TEXT PRIMARY KEY, reviewer TEXT NOT NULL DEFAULT '',
                opinion TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL,
                updated TEXT NOT NULL, FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS import_batches (
                id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT '',
                recursive INTEGER NOT NULL DEFAULT 0,
                planned_count INTEGER NOT NULL DEFAULT 0,
                processed_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '预览',
                report_path TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL, finished TEXT
            );
            CREATE TABLE IF NOT EXISTS import_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
                path TEXT NOT NULL, name TEXT NOT NULL, role TEXT,
                status TEXT NOT NULL, reason_code TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '', document_id TEXT,
                created TEXT NOT NULL, FOREIGN KEY(batch_id) REFERENCES import_batches(id)
            );
            CREATE INDEX IF NOT EXISTS idx_import_items_batch ON import_items(batch_id);
            CREATE TABLE IF NOT EXISTS ai_batches (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                endpoint_identity TEXT NOT NULL, config_snapshot TEXT NOT NULL,
                selected_count INTEGER NOT NULL, repeat_billing_confirmed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '已创建', total_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0, stopped_count INTEGER NOT NULL DEFAULT 0,
                stop_requested INTEGER NOT NULL DEFAULT 0, stop_note TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL, started TEXT, finished TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_batch_items (
                batch_id TEXT NOT NULL, document_id TEXT NOT NULL, position INTEGER NOT NULL,
                run_id TEXT, status TEXT NOT NULL DEFAULT '排队', error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(batch_id, document_id), FOREIGN KEY(batch_id) REFERENCES ai_batches(id),
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS ai_results (
                run_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                output_contract_version TEXT NOT NULL, input_redacted_sha256 TEXT NOT NULL,
                raw_response_json TEXT NOT NULL DEFAULT '', normalized_output_json TEXT NOT NULL,
                validation_errors_json TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES ai_runs(id), FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS trash_entries (
                document_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL,
                purge_at TEXT NOT NULL, original_status TEXT NOT NULL,
                restored_at TEXT, FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS model_states (
                identity TEXT NOT NULL, provider TEXT NOT NULL, base_url TEXT NOT NULL,
                model TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
                connection_tested_at TEXT, real_call_verified_at TEXT,
                calibrated_at TEXT, sample_count INTEGER NOT NULL DEFAULT 0,
                positive_count INTEGER NOT NULL DEFAULT 0, recall REAL,
                evidence_rate REAL, contract_rate REAL, auto_actions INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(identity, role)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT,
                event_type TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_documents_active ON documents(deleted_at, created);
            CREATE INDEX IF NOT EXISTS idx_revisions_document ON revisions(document_id, id);
            CREATE INDEX IF NOT EXISTS idx_ai_runs_document ON ai_runs(document_id, created);
            CREATE VIEW IF NOT EXISTS manual_revisions AS
                SELECT id AS revision_id, document_id, reviewer AS reviewer_name,
                       reviewer, opinion, evidence, created, revision_number, role,
                       versions, source_ai_run_id, created_by
                FROM revisions;
            """
        )
        db.execute(f"PRAGMA user_version={DESKTOP_SCHEMA_VERSION}")

    def _migration_backup(self, version: int) -> Path:
        folder = self.root / "migration-backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (
            "reviews-schema-"
            + str(version)
            + "-"
            + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            + ".sqlite3"
        )
        with closing(sqlite3.connect(self.database)) as source, closing(
            sqlite3.connect(path)
        ) as target:
            source.backup(target)
            target.commit()
        return path

    def _restore_migration_backup(self, backup: Path) -> None:
        for suffix in ("-wal", "-shm"):
            (self.database.parent / (self.database.name + suffix)).unlink(
                missing_ok=True
            )
        shutil.copy2(backup, self.database)

    def _upgrade_schema(self, db: sqlite3.Connection) -> None:
        """Additive v1 -> v2 migration; no existing row or file is discarded."""

        for column, definition in (
            ("encoding_status", "TEXT NOT NULL DEFAULT 'ok'"),
            ("ai_status", "TEXT NOT NULL DEFAULT '未运行'"),
            ("ai_band", "TEXT"),
            ("ai_recommendation", "TEXT"),
            ("ai_primary_risk", "TEXT NOT NULL DEFAULT ''"),
            ("human_status", "TEXT NOT NULL DEFAULT '未审阅'"),
            ("deleted_at", "TEXT"),
            ("purge_at", "TEXT"),
            ("restored_at", "TEXT"),
            ("import_batch_id", "TEXT"),
            ("updated", "TEXT NOT NULL DEFAULT ''"),
        ):
            self._add_column(db, "documents", column, definition)
        for column, definition in (
            ("revision_number", "INTEGER"),
            ("role", "TEXT"),
            ("versions", "TEXT"),
            ("source_ai_run_id", "TEXT"),
            ("created_by", "TEXT"),
        ):
            self._add_column(db, "revisions", column, definition)
        for column, definition in (
            ("batch_id", "TEXT"),
            ("contract_version", "TEXT"),
            ("input_redacted_sha256", "TEXT"),
            ("raw_response_path", "TEXT"),
            ("normalized_output_path", "TEXT"),
            ("validation_error_path", "TEXT"),
            ("model_recommendation", "TEXT"),
            ("ai_band", "TEXT"),
            ("primary_risk", "TEXT NOT NULL DEFAULT ''"),
            ("updated", "TEXT NOT NULL DEFAULT ''"),
        ):
            self._add_column(db, "ai_runs", column, definition)
        db.execute(
            "UPDATE documents SET human_status = CASE "
            "WHEN status = '人工审阅完成' THEN '已完成' "
            "WHEN status = '待人工审阅' THEN '待审阅' "
            "ELSE human_status END, "
            "updated = CASE WHEN updated = '' THEN created ELSE updated END"
        )
        db.execute(
            "UPDATE revisions SET revision_number = COALESCE(revision_number, id), "
            "role = COALESCE(role, (SELECT role FROM documents WHERE documents.id = revisions.document_id)), "
            "versions = COALESCE(versions, (SELECT versions FROM documents WHERE documents.id = revisions.document_id)), "
            "created_by = COALESCE(created_by, reviewer)"
        )
        self._create_schema_v2(db)

    def _migrate_v1_to_v2(self) -> None:
        backup = self._migration_backup(1)
        try:
            with closing(self.connect()) as db, db:
                self._upgrade_schema(db)
        except Exception as exc:
            try:
                self._restore_migration_backup(backup)
            except Exception as rollback_exc:
                raise ValueError(
                    f"数据库迁移失败且自动回滚失败；请使用备份：{backup}"
                ) from rollback_exc
            raise ValueError(
                f"数据库迁移失败，已自动回滚；备份保存在：{backup}"
            ) from exc

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def list_documents(self, *, include_deleted: bool = False):
        with closing(self.connect()) as db:
            predicate = "" if include_deleted else " WHERE deleted_at IS NULL"
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM documents" + predicate + " ORDER BY created DESC"
                )
            ]

    def get(self, document_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            if row is None:
                raise ValueError("未找到材料")
            return dict(row)

    def _document_identity(self, payload: bytes, role: str) -> str:
        versions = json.dumps(
            {"parser": PARSER_VERSION, "role": ROLE_VERSIONS[role]}, sort_keys=True
        )
        return hashlib.sha256(
            payload + role.encode() + versions.encode()
        ).hexdigest()

    def _log(self, event_type: str, document_id: str | None = None, **details) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO audit_events(document_id,event_type,details,created) VALUES (?,?,?,?)",
                (document_id, event_type, json.dumps(details, ensure_ascii=False), now()),
            )

    @staticmethod
    def _encoding_pending(source: Path, payload: bytes) -> bool:
        if source.suffix.lower() not in {".txt", ".md"}:
            return False
        try:
            payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            return True
        return False

    def prepare(
        self, source: Path, role: str | None, *, batch_id: str | None = None
    ):
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
        identity = self._document_identity(payload, role)
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
        encoding_pending = self._encoding_pending(source, payload)
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT OR IGNORE INTO documents
                   (id,name,source,snapshot,role,versions,status,error,content,created,
                    encoding_status,ai_status,human_status,import_batch_id,updated)
                   VALUES (?,?,?,?,?,?,'','',?,?,?,?,?,?,?)""",
                (
                    identity,
                    source.name,
                    str(source),
                    str(snapshot),
                    role,
                    versions,
                    "编码待确认" if encoding_pending else "整理中",
                    now(),
                    "pending" if encoding_pending else "ok",
                    "未运行",
                    "未审阅",
                    batch_id,
                    now(),
                ),
            )
            if encoding_pending:
                db.execute(
                    "UPDATE documents SET status='编码待确认', error=?, encoding_status='pending', "
                    "import_batch_id=COALESCE(?, import_batch_id), updated=? WHERE id=?",
                    ("文本不是有效 UTF-8，请确认编码后再整理", batch_id, now(), identity),
                )
            else:
                db.execute(
                    "UPDATE documents SET status='整理中',error='', encoding_status='ok', "
                    "import_batch_id=COALESCE(?, import_batch_id), updated=? WHERE id=?",
                    (batch_id, now(), identity),
                )
        if encoding_pending:
            self._log("import_encoding_pending", identity, path=str(source))
            return self.get(identity)
        try:
            cleaned = clean_resume(
                snapshot,
                candidate_id=identity,
                candidate_name=infer_candidate_name(source),
            )
            atomic_text(folder / "resume.cleaned.md", cleaned.markdown)
            with closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE documents SET status='待人工审阅', human_status='未审阅', content=?, "
                    "error='', updated=? WHERE id=?",
                    (cleaned.markdown, now(), identity),
                )
            self._log("document_prepared", identity, path=str(source))
        except Exception as exc:  # noqa: BLE001 -- isolate third-party parsers and persist only the error type.
            # Never persist raw parser/provider exception text that could contain PII.
            with closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE documents SET status='解析失败',error=?, updated=? WHERE id=?",
                    (
                        f"{type(exc).__name__}：请检查原文件是否损坏、加密或缺少有效文字",
                        now(),
                        identity,
                    ),
                )
            self._log("document_parse_failed", identity, error=type(exc).__name__)
        return self.get(identity)

    def recover(self):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE documents SET status='解析失败',error='上次整理中断，请重新导入',updated=? "
                "WHERE status='整理中' AND deleted_at IS NULL",
                (now(),),
            )
            db.execute(
                "UPDATE ai_runs SET status='请求状态待核对',updated=? WHERE status='处理中'",
                (now(),),
            )

    def list_trash(self):
        with closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT d.*, t.deleted_at AS trash_deleted_at,
                              t.purge_at AS trash_purge_at, t.original_status
                       FROM documents AS d JOIN trash_entries AS t ON t.document_id=d.id
                       ORDER BY t.deleted_at DESC"""
                )
            ]

    def delete_document(self, document_id: str, *, purge: bool = False) -> dict:
        """Soft-delete one local document, or permanently purge it when asked."""

        record = self.get(document_id)
        if purge:
            return self.purge_document(document_id)
        if record.get("deleted_at"):
            return record
        deleted_at = now()
        purge_at = (
            datetime.fromisoformat(deleted_at) + timedelta(days=TRASH_RETENTION_DAYS)
        ).isoformat()
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT OR REPLACE INTO trash_entries
                   (document_id,deleted_at,purge_at,original_status,restored_at)
                   VALUES (?,?,?,?,NULL)""",
                (document_id, deleted_at, purge_at, record["status"]),
            )
            db.execute(
                """UPDATE documents SET deleted_at=?, purge_at=?, status='已删除',
                   updated=? WHERE id=?""",
                (deleted_at, purge_at, now(), document_id),
            )
        self._log(
            "document_soft_deleted",
            document_id,
            purge_at=purge_at,
            scope="本机应用管理的数据；不影响飞书或模型供应商侧数据",
        )
        return self.get(document_id)

    # Public descriptive alias used by lifecycle callers.
    soft_delete = delete_document

    def restore_document(self, document_id: str) -> dict:
        record = self.get(document_id)
        trash = None
        with closing(self.connect()) as db:
            trash = db.execute(
                "SELECT * FROM trash_entries WHERE document_id=?", (document_id,)
            ).fetchone()
        if trash is None:
            return record
        status = trash["original_status"]
        if status == "已删除":
            status = "待人工审阅"
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE documents SET deleted_at=NULL,purge_at=NULL,restored_at=?,status=?,updated=? WHERE id=?",
                (now(), status, now(), document_id),
            )
            db.execute("DELETE FROM trash_entries WHERE document_id=?", (document_id,))
        self._log("document_restored", document_id)
        return self.get(document_id)

    def purge_document(self, document_id: str) -> dict:
        """Permanently remove local files and all related database records."""

        record = self.get(document_id)
        with closing(self.connect()) as db:
            run_rows = db.execute(
                "SELECT id,directory FROM ai_runs WHERE document_id=?", (document_id,)
            ).fetchall()
        paths = {self._owned_path(Path(record["snapshot"]).parent)}
        paths.update(
            self._owned_path(Path(row["directory"]))
            for row in run_rows
            if row["directory"]
        )
        with self._staged_removal(paths):
            with closing(self.connect()) as db, db:
                db.execute("DELETE FROM drafts WHERE document_id=?", (document_id,))
                db.execute("DELETE FROM revisions WHERE document_id=?", (document_id,))
                db.execute("DELETE FROM ai_results WHERE document_id=?", (document_id,))
                db.execute("DELETE FROM ai_batch_items WHERE document_id=?", (document_id,))
                db.execute("DELETE FROM ai_runs WHERE document_id=?", (document_id,))
                db.execute("DELETE FROM import_items WHERE document_id=?", (document_id,))
                db.execute("DELETE FROM trash_entries WHERE document_id=?", (document_id,))
                db.execute("DELETE FROM documents WHERE id=?", (document_id,))
        self._log("document_permanently_purged", document_id, local_only=True)
        return {"document_id": document_id, "removed": True, "name": record["name"]}

    def purge_expired(self, *, at: str | None = None) -> list[str]:
        point = at or now()
        with closing(self.connect()) as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT document_id FROM trash_entries WHERE purge_at <= ?", (point,)
                )
            ]
        for document_id in ids:
            self.purge_document(document_id)
        return ids

    # Short alias for scheduled cleanup callers.
    purge_expired_documents = purge_expired

    def clear_all_data(self) -> dict[str, int]:
        """Clear local material/review/AI data while retaining model settings."""

        paths = {
            self.root / relative
            for relative in ("materials", "ai-runs", "import-reports", "last-import-report.txt")
        }
        with self._staged_removal(paths):
            with closing(self.connect()) as db, db:
                document_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                revision_count = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
                run_count = db.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0]
                db.execute("DELETE FROM ai_results")
                db.execute("DELETE FROM ai_batch_items")
                db.execute("DELETE FROM ai_batches")
                db.execute("DELETE FROM ai_runs")
                db.execute("DELETE FROM revisions")
                db.execute("DELETE FROM drafts")
                db.execute("DELETE FROM import_items")
                db.execute("DELETE FROM import_batches")
                db.execute("DELETE FROM trash_entries")
                db.execute("DELETE FROM audit_events")
                db.execute("DELETE FROM documents")
        self._log("all_local_data_cleared", None, credentials_retained=True)
        return {
            "documents": int(document_count),
            "revisions": int(revision_count),
            "ai_runs": int(run_count),
        }

    clear_all = clear_all_data

    def save_review(
        self,
        document_id,
        reviewer,
        opinion,
        evidence,
        *,
        source_ai_run_id: str | None = None,
    ):
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
            if source_ai_run_id:
                linked = db.execute(
                    "SELECT document_id FROM ai_runs WHERE id=?", (source_ai_run_id,)
                ).fetchone()
                if linked is None or linked["document_id"] != document_id:
                    raise ValueError("人工修订关联的 AI 运行不存在或不属于当前材料")
            revision_number = (
                db.execute(
                    "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM revisions WHERE document_id=?",
                    (document_id,),
                ).fetchone()[0]
            )
            db.execute(
                """INSERT INTO revisions
                   (document_id,reviewer,opinion,evidence,created,revision_number,
                    role,versions,source_ai_run_id,created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    reviewer.strip(),
                    opinion,
                    json.dumps(evidence, ensure_ascii=False),
                    now(),
                    revision_number,
                    record["role"],
                    record["versions"],
                    source_ai_run_id,
                    reviewer.strip(),
                ),
            )
            db.execute(
                "UPDATE documents SET status=?,human_status=?,updated=? WHERE id=?",
                ("人工审阅完成" if complete else "待人工审阅", "已完成" if complete else "待审阅", now(), document_id),
            )
            db.execute("DELETE FROM drafts WHERE document_id=?", (document_id,))
        self._log(
            "manual_revision_saved",
            document_id,
            revision_number=revision_number,
            source_ai_run_id=source_ai_run_id,
        )

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

    def list_revisions(self, document_id: str) -> list[dict]:
        with closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM revisions WHERE document_id=? ORDER BY id DESC",
                    (document_id,),
                )
            ]

    def revision_history(self, document_id: str) -> list[dict]:
        return self.list_revisions(document_id)

    def get_revision(self, revision_id: int) -> dict:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise ValueError("未找到人工修订")
        return dict(row)

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

    @staticmethod
    def collect_import_paths(sources, *, recursive: bool = False) -> list[Path]:
        """Expand files/directories deterministically for preview and import."""

        paths: list[Path] = []
        for value in sources:
            path = Path(value).expanduser().resolve()
            if path.is_file():
                paths.append(path)
            elif path.is_dir():
                iterator = path.rglob("*") if recursive else path.iterdir()
                paths.extend(item.resolve() for item in iterator if item.is_file())
        return sorted(set(paths), key=lambda item: str(item).casefold())

    def preview_import(
        self,
        sources,
        role: str | None,
        *,
        recursive: bool = False,
    ) -> dict:
        """Return a non-mutating, per-file import preview."""

        role_code = role
        if role_code in ROLES.values():
            role_code = next(key for key, value in ROLES.items() if value == role_code)
        paths = self.collect_import_paths(sources, recursive=recursive)
        items: list[dict] = []
        seen: set[str] = set()
        for path in paths:
            item = {
                "path": str(path),
                "name": path.name,
                "role": role_code,
                "status": "跳过",
                "reason_code": "OK",
                "message": "",
                "document_id": None,
                "will_process": False,
            }
            suffix = path.suffix.lower()
            if suffix not in SUPPORTED_SUFFIXES:
                item.update(reason_code="UNSUPPORTED_FORMAT", message="不支持的文件格式")
                items.append(item)
                continue
            try:
                payload = path.read_bytes()
            except OSError:
                item.update(reason_code="READ_ERROR", message="文件无法读取")
                items.append(item)
                continue
            detected = infer_role(path)
            effective_role = role_code or detected
            item["detected_role"] = detected
            item["role"] = effective_role
            if effective_role not in ROLES:
                item.update(reason_code="UNKNOWN_ROLE", message="无法识别岗位，请明确选择固定岗位")
                items.append(item)
                continue
            if detected and detected != effective_role:
                item.update(reason_code="ROLE_CONFLICT", message="文件名岗位与指定岗位冲突")
                items.append(item)
                continue
            identity = self._document_identity(payload, effective_role)
            item["document_id"] = identity
            if identity in seen:
                item.update(reason_code="DUPLICATE", message="本次导入中重复")
            else:
                with closing(self.connect()) as db:
                    existing = db.execute(
                        "SELECT status,deleted_at FROM documents WHERE id=?", (identity,)
                    ).fetchone()
                if existing is not None and not (
                    existing["status"] == "解析失败" and not existing["deleted_at"]
                ):
                    item.update(reason_code="DUPLICATE", message="本机已有相同材料与岗位版本")
                elif self._encoding_pending(path, payload):
                    item.update(
                        reason_code="ENCODING_PENDING",
                        status="待确认",
                        message="文本不是 UTF-8，将保留为编码待确认，不进入 AI",
                        will_process=True,
                    )
                else:
                    item.update(status="待处理", will_process=True)
            seen.add(identity)
            items.append(item)
        counts = {
            "total": len(items),
            "supported": sum(item["reason_code"] not in {"UNSUPPORTED_FORMAT", "READ_ERROR"} for item in items),
            "duplicates": sum(item["reason_code"] == "DUPLICATE" for item in items),
            "role_conflicts": sum(item["reason_code"] == "ROLE_CONFLICT" for item in items),
            "unknown_role": sum(item["reason_code"] == "UNKNOWN_ROLE" for item in items),
            "skipped": sum(not item["will_process"] for item in items),
            "to_process": sum(item["will_process"] for item in items),
            "encoding_pending": sum(item["reason_code"] == "ENCODING_PENDING" for item in items),
        }
        return {"recursive": recursive, "role": role_code, "items": items, "counts": counts}

    import_preview = preview_import

    def create_import_batch(
        self,
        *,
        source: str = "",
        recursive: bool = False,
        planned_count: int = 0,
    ) -> str:
        import uuid

        batch_id = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO import_batches
                   (id,source,recursive,planned_count,status,created)
                   VALUES (?,?,?,?,?,?)""",
                (batch_id, str(source), int(recursive), planned_count, "处理中", now()),
            )
        return batch_id

    def record_import_item(
        self,
        batch_id: str,
        *,
        path: str | Path,
        status: str,
        reason_code: str,
        message: str = "",
        role: str | None = None,
        document_id: str | None = None,
    ) -> None:
        if reason_code not in IMPORT_REASON_CODES:
            raise ValueError("不支持的导入报告原因")
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO import_items
                   (batch_id,path,name,role,status,reason_code,message,document_id,created)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    str(path),
                    Path(path).name,
                    role,
                    status,
                    reason_code,
                    str(message or "")[:500],
                    document_id,
                    now(),
                ),
            )

    def finish_import_batch(self, batch_id: str, *, status: str = "完成") -> dict:
        with closing(self.connect()) as db, db:
            rows = db.execute(
                "SELECT reason_code,status FROM import_items WHERE batch_id=?", (batch_id,)
            ).fetchall()
            counts = {
                "processed": sum(row["status"] in {"成功", "待确认"} for row in rows),
                "success": sum(row["status"] == "成功" for row in rows),
                "failed": sum(row["status"] == "失败" for row in rows),
                "skipped": sum(row["status"] in {"跳过", "重复"} for row in rows),
            }
            db.execute(
                """UPDATE import_batches SET processed_count=?,success_count=?,failed_count=?,
                   skipped_count=?,status=?,finished=? WHERE id=?""",
                (
                    counts["processed"],
                    counts["success"],
                    counts["failed"],
                    counts["skipped"],
                    status,
                    now(),
                    batch_id,
                ),
            )
        return self.write_import_report(batch_id, counts=counts)

    def write_import_report(self, batch_id: str, *, counts: dict | None = None) -> dict:
        with closing(self.connect()) as db:
            batch = db.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
            rows = db.execute(
                "SELECT * FROM import_items WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        if batch is None:
            raise ValueError("未找到导入批次")
        payload = {
            "batch": dict(batch),
            "counts": counts or {
                "total": len(rows),
                "failed": sum(row["status"] == "失败" for row in rows),
            },
            "items": [dict(row) for row in rows],
        }
        report_dir = self.root / "import-reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        json_path = report_dir / f"import-{batch_id}.json"
        text_path = report_dir / f"import-{batch_id}.txt"
        atomic_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
        lines = [
            f"导入批次：{batch_id}",
            f"状态：{batch['status']}",
            f"递归：{'是' if batch['recursive'] else '否'}",
            "",
        ]
        for row in rows:
            lines.append(
                f"[{row['status']}] {row['name']} · {row['reason_code']} · {row['message']}"
            )
        atomic_text(text_path, "\n".join(lines) + "\n")
        with closing(self.connect()) as db, db:
            db.execute("UPDATE import_batches SET report_path=? WHERE id=?", (str(json_path), batch_id))
        atomic_text(self.root / "last-import-report.txt", "\n".join(lines) + "\n")
        return {"json": json_path, "text": text_path, "counts": payload["counts"]}

    def get_import_batch(self, batch_id: str) -> dict:
        with closing(self.connect()) as db:
            batch = db.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("未找到导入批次")
            result = dict(batch)
            result["items"] = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM import_items WHERE batch_id=? ORDER BY id", (batch_id,)
                )
            ]
            return result

    def list_import_batches(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM import_batches ORDER BY created DESC")]

    def create_ai_batch(
        self,
        document_ids: list[str],
        *,
        config_snapshot: dict,
        repeat_billing_confirmed: bool = False,
    ) -> str:
        import uuid

        if _contains_secret_field(config_snapshot):
            raise ValueError("AI 批次配置快照不得包含密钥")
        document_ids = list(dict.fromkeys(document_ids))
        if not document_ids:
            raise ValueError("AI 批次至少需要一份材料")
        batch_id = uuid.uuid4().hex
        provider = str(config_snapshot.get("provider", ""))
        model = str(config_snapshot.get("model", ""))
        endpoint_identity = str(config_snapshot.get("endpoint_identity", ""))
        snapshot = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True)
        with closing(self.connect()) as db, db:
            placeholders = ",".join("?" for _ in document_ids)
            existing = {
                row[0]
                for row in db.execute(
                    f"SELECT id FROM documents WHERE deleted_at IS NULL AND id IN ({placeholders})",
                    document_ids,
                )
            }
            if existing != set(document_ids):
                raise ValueError("AI 批次包含不存在或已删除的材料")
            db.execute(
                """INSERT INTO ai_batches
                   (id,provider,model,endpoint_identity,config_snapshot,selected_count,total_count,
                    repeat_billing_confirmed,status,created)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    provider,
                    model,
                    endpoint_identity,
                    snapshot,
                    len(document_ids),
                    len(document_ids),
                    int(repeat_billing_confirmed),
                    "已创建",
                    now(),
                ),
            )
            for position, document_id in enumerate(document_ids, 1):
                db.execute(
                    "INSERT INTO ai_batch_items(batch_id,document_id,position) VALUES (?,?,?)",
                    (batch_id, document_id, position),
                )
        return batch_id

    def mark_ai_batch_started(self, batch_id: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE ai_batches SET status='处理中',started=? WHERE id=?",
                (now(), batch_id),
            )

    def request_ai_batch_stop(self, batch_id: str, *, note: str = "用户停止") -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE ai_batches SET stop_requested=1,stop_note=? WHERE id=?",
                (note[:200], batch_id),
            )

    def update_ai_batch_item(
        self, batch_id: str, document_id: str, *, status: str, run_id: str | None = None, error: str = ""
    ) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                """UPDATE ai_batch_items SET status=?,run_id=?,error=?
                   WHERE batch_id=? AND document_id=?""",
                (status, run_id, error[:500], batch_id, document_id),
            )

    def finish_ai_batch(
        self,
        batch_id: str,
        *,
        stopped: bool = False,
        paused: bool = False,
        note: str = "",
    ) -> dict:
        with closing(self.connect()) as db, db:
            rows = db.execute(
                "SELECT status FROM ai_batch_items WHERE batch_id=?", (batch_id,)
            ).fetchall()
            counts = {
                "succeeded": sum(row["status"] == "成功" for row in rows),
                "failed": sum(row["status"] in {"失败", "需人工核对"} for row in rows),
                "remaining": sum(row["status"] == "排队" for row in rows),
            }
            counts["completed"] = len(rows) - counts["remaining"]
            final_status = "已暂停" if paused else ("已停止" if stopped else "完成")
            db.execute(
                """UPDATE ai_batches SET status=?,completed_count=?,stopped_count=?,
                   stop_note=COALESCE(NULLIF(?,''),stop_note),finished=? WHERE id=?""",
                (final_status, counts["completed"], counts["remaining"], note, now(), batch_id),
            )
        return counts

    def list_ai_batches(self) -> list[dict]:
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM ai_batches ORDER BY created DESC")]

    def create_ai_run(
        self,
        run_id: str,
        document_id: str,
        *,
        config_snapshot: dict,
        directory: str | Path,
        batch_id: str | None = None,
        input_redacted_sha256: str | None = None,
    ) -> None:
        if _contains_secret_field(config_snapshot):
            raise ValueError("AI 运行配置快照不得包含密钥")
        created = now()
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO ai_runs
                   (id,document_id,config,status,directory,created,batch_id,contract_version,
                    input_redacted_sha256,updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    document_id,
                    json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True),
                    "处理中",
                    str(directory),
                    created,
                    batch_id,
                    config_snapshot.get("output_contract_version", ""),
                    input_redacted_sha256,
                    created,
                ),
            )
            db.execute(
                "UPDATE documents SET ai_status='处理中',updated=? WHERE id=?",
                (created, document_id),
            )
            if batch_id:
                db.execute(
                    "UPDATE ai_batch_items SET status='处理中',run_id=? WHERE batch_id=? AND document_id=?",
                    (run_id, batch_id, document_id),
                )

    def update_ai_run(
        self,
        run_id: str,
        *,
        status: str,
        contract_version: str | None = None,
        input_redacted_sha256: str | None = None,
        raw_response_path: str | None = None,
        normalized_output_path: str | None = None,
        validation_error_path: str | None = None,
        model_recommendation: str | None = None,
        ai_band: str | None = None,
        primary_risk: str = "",
        batch_item_status: str | None = None,
        error: str = "",
    ) -> None:
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT document_id,batch_id FROM ai_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("未找到 AI 运行")
            db.execute(
                """UPDATE ai_runs SET status=?,contract_version=COALESCE(?,contract_version),
                   input_redacted_sha256=COALESCE(?,input_redacted_sha256),
                   raw_response_path=COALESCE(?,raw_response_path),
                   normalized_output_path=COALESCE(?,normalized_output_path),
                   validation_error_path=COALESCE(?,validation_error_path),
                   model_recommendation=COALESCE(?,model_recommendation),
                   ai_band=COALESCE(?,ai_band),primary_risk=?,updated=? WHERE id=?""",
                (
                    status,
                    contract_version,
                    input_redacted_sha256,
                    raw_response_path,
                    normalized_output_path,
                    validation_error_path,
                    model_recommendation,
                    ai_band,
                    primary_risk[:500],
                    now(),
                    run_id,
                ),
            )
            document_state = {
                "succeeded": "成功",
                "manual_review": "需人工核对",
                "retryable_failed": "失败",
                "请求状态待核对": "需人工核对",
            }.get(status, status)
            db.execute(
                """UPDATE documents SET ai_status=?,ai_band=COALESCE(?,ai_band),
                   ai_recommendation=COALESCE(?,ai_recommendation),ai_primary_risk=?,updated=? WHERE id=?""",
                (document_state, ai_band, model_recommendation, primary_risk[:500], now(), row["document_id"]),
            )
            if row["batch_id"] and batch_item_status:
                db.execute(
                    "UPDATE ai_batch_items SET status=?,error=? WHERE batch_id=? AND document_id=?",
                    (batch_item_status, error[:500], row["batch_id"], row["document_id"]),
                )

    def save_ai_result(
        self,
        *,
        run_id: str,
        document_id: str,
        output_contract_version: str,
        input_redacted_sha256: str,
        raw_response: dict | str,
        normalized_output: dict,
        validation_errors: list[str] | None = None,
    ) -> None:
        """Insert one immutable normalized AI result; never update an existing run."""

        if output_contract_version != AI_OUTPUT_CONTRACT_VERSION:
            raise ValueError("AI 结果合同版本不受支持")
        if _contains_secret_field(raw_response) or _contains_secret_field(normalized_output):
            raise ValueError("AI 原始结果和规范化结果不得包含密钥字段")
        with closing(self.connect()) as db, db:
            run = db.execute(
                "SELECT document_id FROM ai_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None or run["document_id"] != document_id:
                raise ValueError("AI 结果关联的运行不存在或不属于当前材料")
            try:
                db.execute(
                    """INSERT INTO ai_results
                       (run_id,document_id,output_contract_version,input_redacted_sha256,
                        raw_response_json,normalized_output_json,validation_errors_json,created)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        document_id,
                        output_contract_version,
                        input_redacted_sha256,
                        raw_response if isinstance(raw_response, str) else json.dumps(raw_response, ensure_ascii=False),
                        json.dumps(normalized_output, ensure_ascii=False, sort_keys=True),
                        json.dumps(validation_errors or [], ensure_ascii=False),
                        now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("AI 原始结果已存在且不可覆盖") from exc

    def get_ai_result(self, run_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM ai_results WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("raw_response_json", "normalized_output_json", "validation_errors_json"):
            try:
                result[key[:-5] if key.endswith("_json") else key] = json.loads(result[key])
            except (TypeError, ValueError):
                pass
        return result

    def list_ai_runs(self, document_id: str | None = None) -> list[dict]:
        with closing(self.connect()) as db:
            if document_id:
                rows = db.execute(
                    "SELECT * FROM ai_runs WHERE document_id=? ORDER BY created DESC", (document_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM ai_runs ORDER BY created DESC").fetchall()
        return [dict(row) for row in rows]

    def latest_ai_result(self, document_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT r.* FROM ai_results AS r JOIN ai_runs AS a ON a.id=r.run_id
                   WHERE r.document_id=? ORDER BY r.created DESC LIMIT 1""",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_model_connection(self, config, *, role: str = "") -> None:
        self._upsert_model_state(config, role=role, connection=True)

    def mark_model_real_call(self, config, *, role: str = "") -> None:
        self._upsert_model_state(config, role=role, real=True)

    def _upsert_model_state(self, config, *, role: str, connection: bool = False, real: bool = False) -> None:
        identity = config.identity
        with closing(self.connect()) as db, db:
            existing = db.execute(
                "SELECT * FROM model_states WHERE identity=? AND role=?", (identity, role)
            ).fetchone()
            timestamp = now()
            db.execute(
                """INSERT INTO model_states
                   (identity,provider,base_url,model,role,connection_tested_at,real_call_verified_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(identity,role) DO UPDATE SET
                   connection_tested_at=COALESCE(excluded.connection_tested_at,model_states.connection_tested_at),
                   real_call_verified_at=COALESCE(excluded.real_call_verified_at,model_states.real_call_verified_at)""",
                (
                    identity,
                    config.provider,
                    config.base_url.strip().rstrip("/"),
                    config.model,
                    role,
                    timestamp if connection else (existing["connection_tested_at"] if existing else None),
                    timestamp if real else (existing["real_call_verified_at"] if existing else None),
                ),
            )

    def record_calibration(
        self,
        config,
        *,
        role: str,
        sample_count: int,
        positive_count: int,
        recall: float,
        evidence_rate: float,
        contract_rate: float,
        auto_actions: int = 0,
    ) -> dict:
        if sample_count < 0 or positive_count < 0 or positive_count > sample_count:
            raise ValueError("校准样本数量无效")
        if any(
            not 0 <= value <= 1
            for value in (recall, evidence_rate, contract_rate)
        ):
            raise ValueError("校准比例必须在 0 到 1 之间")
        if auto_actions < 0:
            raise ValueError("自动动作次数无效")
        calibrated = (
            sample_count >= 50
            and positive_count >= 30
            and recall >= 0.90
            and evidence_rate >= 0.95
            and contract_rate >= 0.98
            and auto_actions == 0
        )
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT INTO model_states
                   (identity,provider,base_url,model,role,calibrated_at,sample_count,
                    positive_count,recall,evidence_rate,contract_rate,auto_actions)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(identity,role) DO UPDATE SET
                   calibrated_at=excluded.calibrated_at,sample_count=excluded.sample_count,
                   positive_count=excluded.positive_count,recall=excluded.recall,
                   evidence_rate=excluded.evidence_rate,contract_rate=excluded.contract_rate,
                   auto_actions=excluded.auto_actions""",
                (
                    config.identity,
                    config.provider,
                    config.base_url.strip().rstrip("/"),
                    config.model,
                    role,
                    now() if calibrated else None,
                    sample_count,
                    positive_count,
                    recall,
                    evidence_rate,
                    contract_rate,
                    auto_actions,
                ),
            )
        return self.model_state(config, role=role)

    def model_state(self, config, *, role: str = "") -> dict:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM model_states WHERE identity=? AND role=?", (config.identity, role)
            ).fetchone()
            if row is None and role:
                row = db.execute(
                    "SELECT * FROM model_states WHERE identity=? AND role=''",
                    (config.identity,),
                ).fetchone()
        if row is None:
            return {"state": "未配置", "role": role}
        value = dict(row)
        if value.get("calibrated_at"):
            state = "岗位已校准"
        elif value.get("real_call_verified_at"):
            state = "真实调用已验证"
        elif value.get("connection_tested_at"):
            state = "连接可用"
        else:
            state = "未配置"
        value["state"] = state
        return value

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
                "schema": DESKTOP_SCHEMA_VERSION,
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

    def _owned_path(self, path: str | Path) -> Path:
        """Resolve a persisted path and reject anything outside this data root."""

        candidate = Path(path).resolve()
        root = self.root.resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError("本地数据路径不在 ResumeDesk 数据目录内，已停止清除")
        return candidate

    @staticmethod
    def _move_path(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    @contextmanager
    def _staged_removal(self, paths):
        with tempfile.TemporaryDirectory(
            prefix="resumedesk-delete-", dir=self.root.parent
        ) as temporary:
            staging = Path(temporary)
            swaps = []
            try:
                for path in sorted(paths, key=lambda value: len(value.parts), reverse=True):
                    if not self._path_exists(path):
                        continue
                    staged = staging / path.relative_to(self.root)
                    self._move_path(path, staged)
                    swaps.append((path, staged, True))
                yield
            except Exception:
                self._rollback_restore(swaps)
                raise

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
            if schema > DESKTOP_SCHEMA_VERSION or not {"documents", "revisions", "ai_runs"}.issubset(tables):
                raise ValueError("备份数据库版本过高或缺少必要表")
            if schema < DESKTOP_SCHEMA_VERSION:
                try:
                    with closing(sqlite3.connect(staged_database)) as database, database:
                        database.row_factory = sqlite3.Row
                        self._upgrade_schema(database)
                except (OSError, sqlite3.DatabaseError, ValueError) as exc:
                    raise ValueError("备份数据库迁移失败，未替换当前数据") from exc
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
                    "AI状态",
                    "AI档位",
                    "AI建议",
                    "主要风险",
                    "人工状态",
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
                            row["ai_status"],
                            row["ai_band"] or "",
                            row["ai_recommendation"] or "",
                            row["ai_primary_risk"] or "",
                            row["human_status"],
                            review.get("reviewer"),
                            review.get("opinion"),
                            review.get("revision_number", review.get("id")),
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
                ]
                ai = self.latest_ai_result(row["id"])
                if ai:
                    normalized = json.loads(ai["normalized_output_json"])
                    score = normalized.get("computed_score", {})
                    lines.extend(
                        [
                            "",
                            "## AI 建议（只读，非最终决定）",
                            f"合同版本：{ai.get('output_contract_version', '')}",
                            f"档位：{score.get('band', '')}",
                            f"建议：{normalized.get('model_recommendation', '')}",
                            f"摘要：{normalized.get('summary', '')}",
                        ]
                    )
                else:
                    lines.append("无 AI 评分。")
                for item in json.loads(review.get("evidence", "[]")):
                    lines.extend(
                        [
                            f"## {item['criterion']}：{item['decision']}",
                            item.get("note", ""),
                        ]
                    )
                atomic_text(
                    target / "revisions.json",
                    json.dumps(self.list_revisions(row["id"]), ensure_ascii=False, indent=2),
                )
                atomic_text(target / "review.md", "\n\n".join(lines))
        return folder

    def export_audit(self, directory: Path, document_ids: list[str] | None = None) -> Path:
        """Export a complete local audit view without credentials or raw secrets."""

        import uuid

        folder = Path(directory).resolve() / (
            "audit-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        )
        folder.mkdir(parents=True)
        scoped = bool(document_ids)
        selected = set(
            document_ids
            or [row["id"] for row in self.list_documents(include_deleted=True)]
        )
        documents = [
            row for row in self.list_documents(include_deleted=True) if row["id"] in selected
        ]
        payload = {
            "schema": DESKTOP_SCHEMA_VERSION,
            "created": now(),
            "credentials_included": False,
            "scope": "本机应用管理的数据；不包含飞书或模型供应商侧数据",
            "documents": [],
        }
        with closing(self.connect()) as db:
            if scoped:
                placeholders = ",".join("?" for _ in selected)
                payload["import_batches"] = [
                    dict(row)
                    for row in db.execute(
                        f"""SELECT b.* FROM import_batches AS b
                            WHERE EXISTS (
                                SELECT 1 FROM import_items AS i
                                WHERE i.batch_id=b.id AND i.document_id IN ({placeholders})
                            )
                            ORDER BY b.created DESC""",
                        tuple(selected),
                    )
                ]
                payload["ai_batches"] = [
                    dict(row)
                    for row in db.execute(
                        f"""SELECT b.* FROM ai_batches AS b
                            WHERE EXISTS (
                                SELECT 1 FROM ai_batch_items AS i
                                WHERE i.batch_id=b.id AND i.document_id IN ({placeholders})
                            )
                            ORDER BY b.created DESC""",
                        tuple(selected),
                    )
                ]
                payload["audit_events"] = [
                    dict(row)
                    for row in db.execute(
                        f"SELECT * FROM audit_events WHERE document_id IN ({placeholders}) ORDER BY id",
                        tuple(selected),
                    )
                ]
            else:
                payload["import_batches"] = [
                    dict(row) for row in db.execute("SELECT * FROM import_batches ORDER BY created DESC")
                ]
                payload["ai_batches"] = [
                    dict(row) for row in db.execute("SELECT * FROM ai_batches ORDER BY created DESC")
                ]
                payload["audit_events"] = [
                    dict(row) for row in db.execute("SELECT * FROM audit_events ORDER BY id")
                ]
        for document in documents:
            item = dict(document)
            item["revisions"] = self.list_revisions(document["id"])
            runs = self.list_ai_runs(document["id"])
            item["ai_runs"] = runs
            results = [self.get_ai_result(row["id"]) for row in runs]
            item["ai_result_ids"] = [
                result["run_id"] for result in results if result
            ]
            item["ai_results"] = [result for result in results if result]
            payload["documents"].append(item)
        atomic_text(folder / "audit.json", json.dumps(payload, ensure_ascii=False, indent=2))
        return folder

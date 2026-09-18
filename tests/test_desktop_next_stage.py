import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from keyring.errors import PasswordDeleteError

from resume_screening.desktop_ai_contract import build_ai_result, validate_ai_result
from resume_screening.desktop_model import ModelConfig, clear_saved_credential
from resume_screening.desktop_smoke import TEXT
from resume_screening.desktop_store import ReviewStore, now
from resume_screening.scoring import ROLE_WEIGHTS, score_record
from resume_screening.versions import (
    AI_OUTPUT_CONTRACT_VERSION,
    DESKTOP_SCHEMA_VERSION,
)


def write_v1_database(root: Path) -> dict:
    """Create the last desktop schema without going through the v2 creator."""

    database = root / "reviews.sqlite3"
    record = {
        "id": "legacy-document",
        "name": "legacy.txt",
        "source": str(root / "legacy.txt"),
        "snapshot": str(root / "materials" / "legacy-document" / "source.txt"),
        "role": "senior-fullstack-engineer",
        "versions": '{"parser":"legacy-parser","role":["jd-v1","rubric-v1"]}',
        "status": "待人工审阅",
        "error": "",
        "content": "legacy extracted text",
        "created": now(),
    }
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL,
                snapshot TEXT NOT NULL, role TEXT NOT NULL, versions TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '', created TEXT NOT NULL
            );
            CREATE TABLE revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
                reviewer TEXT NOT NULL, opinion TEXT NOT NULL, evidence TEXT NOT NULL,
                created TEXT NOT NULL
            );
            CREATE TABLE ai_runs (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, config TEXT NOT NULL,
                status TEXT NOT NULL, directory TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE drafts (
                document_id TEXT PRIMARY KEY, reviewer TEXT NOT NULL DEFAULT '',
                opinion TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            PRAGMA user_version=1;
            """
        )
        db.execute(
            """INSERT INTO documents
               (id,name,source,snapshot,role,versions,status,error,content,created)
               VALUES (:id,:name,:source,:snapshot,:role,:versions,:status,:error,:content,:created)""",
            record,
        )
    return record


def synthetic_contract_record() -> dict:
    criteria = list(ROLE_WEIGHTS["senior-fullstack-engineer"])
    evidence = [
        {
            "criterion_id": code,
            "state": "supported",
            "strength": "E3",
            "excerpt": code,
            "location": "synthetic line 1",
            "rationale": "合成证据",
        }
        for code in criteria
    ]
    return {
        "role": "senior-fullstack-engineer",
        "jd_version": "jd-v1",
        "rubric_version": "rubric-v1",
        "evidence": evidence,
        "model_recommendation": "advance_pending_human",
        "summary": "合成结果，必须人工确认。",
        "interview_probes": [{"question": "请说明上线后的验证方式。"}],
        "uncertainties": [{"description": "合成数据，未作真实招聘判断。"}],
    }


class DesktopNextStageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_v1_migration_preserves_rows_and_creates_backup(self):
        legacy = write_v1_database(self.root)

        store = ReviewStore(self.root)

        with closing(store.connect()) as db:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], DESKTOP_SCHEMA_VERSION
            )
            self.assertEqual(db.execute("SELECT name FROM documents").fetchone()[0], legacy["name"])
            self.assertTrue(
                {"ai_batches", "ai_results", "trash_entries", "audit_events"}.issubset(
                    {
                        row[0]
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                )
            )
        backups = list((self.root / "migration-backups").glob("reviews-schema-1-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_migration_failure_restores_v1_database(self):
        legacy = write_v1_database(self.root)

        with patch.object(ReviewStore, "_upgrade_schema", side_effect=RuntimeError("synthetic migration failure")):
            with self.assertRaisesRegex(ValueError, "已自动回滚"):
                ReviewStore(self.root)

        with closing(sqlite3.connect(self.root / "reviews.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT name FROM documents").fetchone()[0], legacy["name"])

    def test_soft_delete_restore_expiry_and_purge_remove_local_associations(self):
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        store = ReviewStore(self.root / "data")
        row = store.prepare(source, "senior-fullstack-engineer")
        store.save_draft(row["id"], "合成审阅者", "待确认", [{"criterion": "学历"}])
        store.save_review(
            row["id"],
            "合成审阅者",
            "待确认",
            [{"criterion": "学历", "decision": "信息不足", "note": "请人工核对"}],
        )
        run_directory = store.root / "ai-runs" / "synthetic-run"
        run_directory.mkdir(parents=True)
        (run_directory / "raw-response.json").write_text("{}", encoding="utf-8")
        store.create_ai_run(
            "synthetic-run",
            row["id"],
            config_snapshot={
                "provider": "openai-compatible",
                "base_url": "https://example.invalid/v1",
                "endpoint_identity": "https://example.invalid/v1",
                "model": "fixture",
                "output_contract_version": AI_OUTPUT_CONTRACT_VERSION,
            },
            directory=run_directory,
        )

        deleted = store.delete_document(row["id"])
        self.assertEqual(deleted["status"], "已删除")
        self.assertEqual(store.list_documents(), [])
        self.assertEqual(len(store.list_trash()), 1)
        self.assertEqual(store.restore_document(row["id"])["status"], "人工审阅完成")
        self.assertEqual(len(store.list_documents()), 1)

        store.delete_document(row["id"])
        with closing(store.connect()) as db, db:
            db.execute(
                "UPDATE trash_entries SET purge_at=? WHERE document_id=?",
                ("2000-01-01T00:00:00+00:00", row["id"]),
            )
        self.assertEqual(store.purge_expired(at="2026-01-01T00:00:00+00:00"), [row["id"]])
        self.assertFalse((store.root / "materials" / row["id"]).exists())
        self.assertFalse(run_directory.exists())
        with closing(store.connect()) as db:
            for table in ("documents", "drafts", "revisions", "ai_runs", "trash_entries"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_purge_filesystem_failure_keeps_database_record(self):
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        store = ReviewStore(self.root / "data")
        row = store.prepare(source, "senior-fullstack-engineer")
        material = store.root / "materials" / row["id"]

        with patch.object(store, "_move_path", side_effect=OSError("disk denied")):
            with self.assertRaisesRegex(OSError, "disk denied"):
                store.purge_document(row["id"])

        self.assertEqual(store.get(row["id"])["id"], row["id"])
        self.assertTrue(material.exists())

    def test_purge_partial_filesystem_failure_restores_all_paths(self):
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        store = ReviewStore(self.root / "data")
        row = store.prepare(source, "senior-fullstack-engineer")
        material = store.root / "materials" / row["id"]
        run_directory = store.root / "ai-runs" / "synthetic-run"
        run_directory.mkdir(parents=True)
        store.create_ai_run(
            "synthetic-run",
            row["id"],
            config_snapshot={"model": "fixture"},
            directory=run_directory,
        )
        store.delete_document(row["id"])
        original_move = store._move_path
        moves = []

        def fail_on_second(source_path, target_path):
            moves.append((source_path, target_path))
            if len(moves) == 2:
                raise OSError("disk denied")
            original_move(source_path, target_path)

        with patch.object(store, "_move_path", side_effect=fail_on_second):
            with self.assertRaisesRegex(OSError, "disk denied"):
                store.purge_document(row["id"])

        self.assertEqual(store.get(row["id"])["id"], row["id"])
        self.assertTrue(material.exists())
        self.assertTrue(run_directory.exists())

    def test_clear_all_filesystem_failure_keeps_database_record(self):
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        store = ReviewStore(self.root / "data")
        row = store.prepare(source, "senior-fullstack-engineer")

        with patch.object(store, "_move_path", side_effect=OSError("disk denied")):
            with self.assertRaisesRegex(OSError, "disk denied"):
                store.clear_all_data()

        self.assertEqual(store.get(row["id"])["id"], row["id"])

    def test_startup_purges_expired_trash(self):
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        store = ReviewStore(self.root / "data")
        row = store.prepare(source, "senior-fullstack-engineer")
        store.delete_document(row["id"])
        with closing(store.connect()) as db, db:
            db.execute(
                "UPDATE trash_entries SET purge_at=? WHERE document_id=?",
                ("2000-01-01T00:00:00+00:00", row["id"]),
            )

        ReviewStore(self.root / "data")

        with self.assertRaisesRegex(ValueError, "未找到材料"):
            store.get(row["id"])
        self.assertFalse((store.root / "materials" / row["id"]).exists())

    def test_clear_all_data_keeps_model_settings_and_removes_reports(self):
        store = ReviewStore(self.root / "data")
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        store.prepare(source, "senior-fullstack-engineer")
        (store.root / "model.json").write_text('{"model":"fixture"}', encoding="utf-8")
        (store.root / "import-reports").mkdir()
        (store.root / "import-reports" / "report.txt").write_text("report", encoding="utf-8")
        (store.root / "last-import-report.txt").write_text("report", encoding="utf-8")

        result = store.clear_all_data()

        self.assertEqual(result["documents"], 1)
        self.assertEqual(store.list_documents(), [])
        self.assertTrue((store.root / "model.json").exists())
        self.assertFalse((store.root / "import-reports").exists())
        self.assertFalse((store.root / "last-import-report.txt").exists())

    def test_audit_export_contains_revisions_runs_results_and_events(self):
        store = ReviewStore(self.root / "data")
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        row = store.prepare(source, "senior-fullstack-engineer")
        store.save_review(
            row["id"],
            "合成审阅者",
            "待确认",
            [{"criterion": "学历", "decision": "信息不足", "note": "请核对"}],
        )
        run_directory = store.root / "ai-runs" / "audit-run"
        run_directory.mkdir(parents=True)
        store.create_ai_run(
            "audit-run",
            row["id"],
            config_snapshot={"model": "fixture"},
            directory=run_directory,
        )
        store.save_ai_result(
            run_id="audit-run",
            document_id=row["id"],
            output_contract_version=AI_OUTPUT_CONTRACT_VERSION,
            input_redacted_sha256="synthetic-hash",
            raw_response={"content": "synthetic"},
            normalized_output={"summary": "synthetic"},
        )

        audit = store.export_audit(self.root / "exports")
        payload = json.loads((audit / "audit.json").read_text(encoding="utf-8"))

        self.assertFalse(payload["credentials_included"])
        self.assertEqual(len(payload["documents"][0]["revisions"]), 1)
        self.assertEqual(len(payload["documents"][0]["ai_results"]), 1)
        self.assertTrue(payload["audit_events"])

    def test_scoped_audit_export_excludes_unselected_batches_and_events(self):
        store = ReviewStore(self.root / "data")
        sources = []
        rows = []
        for index in range(2):
            source = self.root / f"resume-{index}.txt"
            source.write_text(TEXT + f" Synthetic {index}", encoding="utf-8")
            sources.append(source)
            rows.append(store.prepare(source, "senior-fullstack-engineer"))

        import_batches = []
        for source, row in zip(sources, rows):
            batch_id = store.create_import_batch(
                source=source, recursive=False, planned_count=1
            )
            store.record_import_item(
                batch_id,
                path=source,
                role="senior-fullstack-engineer",
                document_id=row["id"],
                status="成功",
                reason_code="OK",
            )
            store.finish_import_batch(batch_id)
            import_batches.append(batch_id)

        config_snapshot = {
            "provider": "openai-compatible",
            "base_url": "https://example.invalid/v1",
            "endpoint_identity": "https://example.invalid/v1",
            "model": "fixture",
        }
        ai_batches = [
            store.create_ai_batch([row["id"]], config_snapshot=config_snapshot)
            for row in rows
        ]

        audit = store.export_audit(self.root / "exports", [rows[0]["id"]])
        payload = json.loads((audit / "audit.json").read_text(encoding="utf-8"))

        self.assertEqual(
            {batch["id"] for batch in payload["import_batches"]},
            {import_batches[0]},
        )
        self.assertEqual(
            {batch["id"] for batch in payload["ai_batches"]},
            {ai_batches[0]},
        )
        self.assertEqual(
            {event["document_id"] for event in payload["audit_events"]},
            {rows[0]["id"]},
        )

    def test_preview_and_import_report_keep_non_utf8_as_pending(self):
        folder = self.root / "incoming"
        folder.mkdir()
        good = folder / "good.txt"
        bad = folder / "bad.txt"
        good.write_text("合成内容", encoding="utf-8")
        bad.write_bytes(b"\x81\x82 not utf8")
        store = ReviewStore(self.root / "data")

        preview = store.preview_import(
            [folder], "senior-fullstack-engineer", recursive=True
        )
        self.assertEqual(preview["counts"]["total"], 2)
        self.assertEqual(preview["counts"]["encoding_pending"], 1)
        self.assertEqual(preview["counts"]["to_process"], 2)

        batch_id = store.create_import_batch(
            source=folder, recursive=True, planned_count=2
        )
        store.record_import_item(
            batch_id,
            path=good,
            role="senior-fullstack-engineer",
            status="成功",
            reason_code="OK",
            message="本地解析完成",
        )
        store.record_import_item(
            batch_id,
            path=bad,
            role="senior-fullstack-engineer",
            status="待确认",
            reason_code="ENCODING_PENDING",
            message="文本编码待确认",
        )
        report = store.finish_import_batch(batch_id)
        self.assertEqual(report["counts"]["processed"], 2)
        self.assertTrue(report["json"].is_file())
        self.assertIn("bad.txt", report["text"].read_text(encoding="utf-8"))

    def test_ai_batch_stop_and_secret_free_snapshot(self):
        store = ReviewStore(self.root / "data")
        config = ModelConfig("openai-compatible", "https://example.invalid/v1", "fixture")
        ids = []
        for index in range(2):
            source = self.root / f"resume-{index}.txt"
            source.write_text(TEXT + f" Synthetic batch item {index}.", encoding="utf-8")
            ids.append(store.prepare(source, "senior-fullstack-engineer")["id"])
        snapshot = {
            "provider": config.provider,
            "base_url": config.base_url,
            "endpoint_identity": config.endpoint_identity,
            "model": config.model,
            "config_identity": config.identity,
            "output_contract_version": AI_OUTPUT_CONTRACT_VERSION,
        }
        batch_id = store.create_ai_batch(ids, config_snapshot=snapshot)
        store.mark_ai_batch_started(batch_id)
        store.update_ai_batch_item(batch_id, ids[0], status="成功", run_id="run-1")
        store.request_ai_batch_stop(batch_id)
        counts = store.finish_ai_batch(batch_id, stopped=True, note="用户停止")

        self.assertEqual(counts["succeeded"], 1)
        batch = store.list_ai_batches()[0]
        self.assertEqual(batch["status"], "已停止")
        self.assertEqual(batch["stopped_count"], 1)
        with self.assertRaisesRegex(ValueError, "不得包含密钥"):
            store.create_ai_batch(ids, config_snapshot={**snapshot, "api_key": "secret"})
        with self.assertRaisesRegex(ValueError, "不得包含密钥"):
            store.create_ai_batch(
                ids,
                config_snapshot={**snapshot, "provider_options": {"access_token": "secret"}},
            )

    def test_ai_results_are_versioned_immutable_and_location_aware(self):
        record = synthetic_contract_record()
        redacted = " ".join(ROLE_WEIGHTS[record["role"]])
        scorecard = score_record(record).as_dict()
        result = build_ai_result(
            record,
            scorecard,
            document_id="doc-1",
            redacted_text=redacted,
            provider="openai-compatible",
            endpoint_identity="https://example.invalid/v1",
            model="fixture",
        )
        self.assertEqual(validate_ai_result(result, redacted), [])
        self.assertEqual(result["computed_score"]["value"], 100)
        self.assertIn("rule_versions", result)
        tampered = copy.deepcopy(result)
        tampered["criteria"][0]["evidence"][0]["quote"] = "not in input"
        self.assertTrue(validate_ai_result(tampered, redacted))

        unlocatable = copy.deepcopy(record)
        unlocatable["evidence"][0]["excerpt"] = "not in input"
        lowered = build_ai_result(
            unlocatable,
            scorecard,
            document_id="doc-1",
            redacted_text=redacted,
        )
        self.assertLess(lowered["computed_score"]["value"], result["computed_score"]["value"])
        self.assertTrue(lowered["evidence_warnings"])

        store = ReviewStore(self.root / "data")
        source = self.root / "resume.txt"
        source.write_text(TEXT, encoding="utf-8")
        document_id = store.prepare(source, "senior-fullstack-engineer")["id"]
        run_directory = store.root / "ai-runs" / "run-1"
        run_directory.mkdir(parents=True)
        store.create_ai_run(
            "run-1",
            document_id,
            config_snapshot={"model": "fixture"},
            directory=run_directory,
        )
        input_hash = hashlib.sha256(redacted.encode()).hexdigest()
        store.save_ai_result(
            run_id="run-1",
            document_id=document_id,
            output_contract_version=AI_OUTPUT_CONTRACT_VERSION,
            input_redacted_sha256=input_hash,
            raw_response={"content": "synthetic"},
            normalized_output=result,
        )
        with self.assertRaisesRegex(ValueError, "不可覆盖"):
            store.save_ai_result(
                run_id="run-1",
                document_id=document_id,
                output_contract_version=AI_OUTPUT_CONTRACT_VERSION,
                input_redacted_sha256=input_hash,
                raw_response={"content": "replacement"},
                normalized_output=result,
            )

    def test_model_states_require_calibration_thresholds(self):
        store = ReviewStore(self.root / "data")
        config = ModelConfig("openai-compatible", "https://example.invalid/v1", "fixture")
        state = store.record_calibration(
            config,
            role="senior-fullstack-engineer",
            sample_count=49,
            positive_count=30,
            recall=0.99,
            evidence_rate=0.99,
            contract_rate=1.0,
        )
        self.assertNotEqual(state["state"], "岗位已校准")
        state = store.record_calibration(
            config,
            role="senior-fullstack-engineer",
            sample_count=50,
            positive_count=30,
            recall=0.90,
            evidence_rate=0.95,
            contract_rate=0.98,
        )
        self.assertEqual(state["state"], "岗位已校准")
        state = store.record_calibration(
            config,
            role="ai-product-manager",
            sample_count=100,
            positive_count=50,
            recall=1.0,
            evidence_rate=1.0,
            contract_rate=1.0,
            auto_actions=1,
        )
        self.assertNotEqual(state["state"], "岗位已校准")

    def test_credential_clear_targets_endpoint_and_legacy_key(self):
        config = ModelConfig("openai-compatible", "https://example.invalid/v1", "fixture")
        backend = Mock()
        with patch("resume_screening.desktop_model.credential_backend", return_value=backend):
            clear_saved_credential(config)
        identities = [call.args[1] for call in backend.delete_password.call_args_list]
        self.assertEqual(identities, [config.credential_identity, config.identity])

    def test_credential_clear_ignores_only_missing_entries(self):
        config = ModelConfig("openai-compatible", "https://example.invalid/v1", "fixture")
        backend = Mock()
        backend.delete_password.side_effect = PasswordDeleteError("ResumeDesk")
        with patch("resume_screening.desktop_model.credential_backend", return_value=backend):
            self.assertFalse(clear_saved_credential(config))

    def test_credential_clear_surfaces_unexpected_backend_errors(self):
        config = ModelConfig("openai-compatible", "https://example.invalid/v1", "fixture")
        backend = Mock()
        backend.delete_password.side_effect = RuntimeError("backend unavailable")
        with patch("resume_screening.desktop_model.credential_backend", return_value=backend):
            with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
                clear_saved_credential(config)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import urllib.error
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from resume_screening.desktop_model import (
    DesktopModelClient,
    ModelConfig,
    NoRedirect,
    _extract_model_names,
    configured_client,
    run_analysis,
)
from resume_screening.desktop_smoke import TEXT
from resume_screening.desktop_store import ReviewStore, csv_safe
from resume_screening.minimax import (
    AmbiguousModelError,
    ProviderAuthError,
    ProviderRateLimitError,
)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ReviewStore(self.root / "data")
        self.source = self.root / "resume.txt"
        self.source.write_text(TEXT, encoding="utf-8")

    def prepare(self):
        return self.store.prepare(self.source, "senior-fullstack-engineer")

    def test_offline_dedup_and_snapshot(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network")):
            row = self.prepare()
            self.prepare()
        self.assertEqual(len(self.store.list_documents()), 1)
        self.source.write_text("changed", encoding="utf-8")
        self.assertEqual(Path(row["snapshot"]).read_text(encoding="utf-8"), TEXT)
        self.assertEqual(row["status"], "待人工审阅")

    def test_unknown_role_and_mismatch(self):
        with self.assertRaises(ValueError):
            self.store.prepare(self.source, None)
        other = self.root / "【全栈开发实习生_深圳】测试 1年.txt"
        other.write_text(TEXT, encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.prepare(other, "senior-fullstack-engineer")

    def test_failure_not_zero_score(self):
        self.source.write_text("too short", encoding="utf-8")
        row = self.prepare()
        self.assertEqual(row["status"], "解析失败")
        self.assertNotIn("score", row)
        with self.assertRaises(ValueError):
            self.store.save_review(row["id"], "r", "", [{"decision": "信息不足"}])

    def test_review_evidence_and_revisions(self):
        row = self.prepare()
        evidence = [{"criterion": "学历", "decision": "有证据支持", "note": ""}]
        with self.assertRaises(ValueError):
            self.store.save_review(row["id"], "reviewer", "", evidence)
        evidence[0]["note"] = "原文 Bachelor"
        self.store.save_review(row["id"], "reviewer", "待确认", evidence)
        evidence[0]["decision"] = "信息不足"
        self.store.save_review(row["id"], "reviewer", "需追问", evidence)
        with closing(self.store.connect()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM revisions").fetchone()[0], 2
            )
        self.assertEqual(self.store.get(row["id"])["status"], "人工审阅完成")
        exported = self.store.export(self.root)
        self.assertIn(
            "无 AI 评分",
            (exported / row["id"] / "review.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((exported / row["id"] / "screening.json").exists())

    def test_csv_formula_and_incomplete_review(self):
        self.assertTrue(csv_safe("  =HYPERLINK(x)").startswith("'"))
        self.assertTrue(csv_safe("\tstuff").startswith("'"))
        row = self.prepare()
        self.store.save_review(
            row["id"],
            "r",
            "",
            [{"criterion": "学历", "decision": "未审阅", "note": ""}],
        )
        self.assertEqual(self.store.get(row["id"])["status"], "待人工审阅")

    def test_draft_round_trip_and_review_clears_draft(self):
        row = self.prepare()
        evidence = [{"criterion": "学历", "decision": "未审阅", "note": "稍后核对"}]
        self.store.save_draft(row["id"], "草稿审阅者", "待追问", evidence)
        draft = self.store.get_draft(row["id"])
        self.assertEqual(draft["reviewer"], "草稿审阅者")
        self.assertEqual(json.loads(draft["evidence"]), evidence)
        self.store.save_review(row["id"], "正式审阅者", "已核对", evidence)
        self.assertIsNone(self.store.get_draft(row["id"]))

    def test_backup_restore_and_no_credentials(self):
        row = self.prepare()
        self.store.save_draft(
            row["id"],
            "草稿审阅者",
            "待追问",
            [{"criterion": "学历", "decision": "未审阅", "note": ""}],
        )
        (self.store.root / "model.json").write_text(
            '{"model":"fixture"}', encoding="utf-8"
        )
        (self.store.root / "feishu-config.json").write_text(
            '{"app_secret":"secret"}', encoding="utf-8"
        )
        backup = self.store.backup(self.root / "backups")
        with zipfile.ZipFile(backup) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
        self.assertFalse(manifest["credentials_included"])
        self.assertNotIn("data/model.json", names)
        self.assertNotIn("data/feishu-config.json", names)

        extra = self.root / "extra.txt"
        extra.write_text(TEXT, encoding="utf-8")
        self.store.prepare(extra, "senior-fullstack-engineer")
        rollback = self.store.restore(backup)
        self.assertTrue(rollback.exists())
        self.assertEqual(len(self.store.list_documents()), 1)
        self.assertIsNotNone(self.store.get_draft(row["id"]))
        self.assertEqual(
            (self.store.root / "model.json").read_text(encoding="utf-8"),
            '{"model":"fixture"}',
        )
        self.assertEqual(
            (self.store.root / "feishu-config.json").read_text(encoding="utf-8"),
            '{"app_secret":"secret"}',
        )

    def test_restore_failure_rolls_back_all_data(self):
        original = self.prepare()
        marker = self.store.root / "materials" / original["id"] / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        backup = self.store.backup(self.root / "backups")
        extra = self.root / "extra.txt"
        extra.write_text(TEXT + "\nsecond", encoding="utf-8")
        self.store.prepare(extra, "senior-fullstack-engineer")
        before_ids = {row["id"] for row in self.store.list_documents()}

        real_move = shutil.move
        calls = 0

        def fail_once(source, target, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk full")
            return real_move(source, target, *args, **kwargs)

        with (
            patch("resume_screening.desktop_store.shutil.move", side_effect=fail_once),
            self.assertRaisesRegex(ValueError, "恢复未完成"),
        ):
            self.store.restore(backup)

        self.assertEqual(
            {row["id"] for row in self.store.list_documents()}, before_ids
        )
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_newer_schema_is_rejected(self):
        with closing(self.store.connect()) as db, db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(ValueError):
            ReviewStore(self.store.root)

    def test_ai_run_isolated_and_invalid_response_not_retried(self):
        row = self.prepare()
        config = ModelConfig(
            "openai-compatible", "https://example.invalid/v1", "fixture"
        )
        from resume_screening.minimax import ModelResponse

        client = Mock()
        client.analyze.return_value = ModelResponse("invalid JSON", None, {})
        status, folder = run_analysis(self.store, row["id"], config, client)
        self.assertEqual(status, "manual_review")
        self.assertEqual(client.analyze.call_count, 1)
        self.assertEqual(self.store.get(row["id"])["status"], "待人工审阅")
        self.assertEqual(
            len(
                self.store.find_ai_runs(
                    row["id"],
                    config_identity=config.identity,
                    provider=config.provider,
                    base_url=config.base_url,
                    model=config.model,
                )
            ),
            1,
        )
        _other_status, other_folder = run_analysis(
            self.store, row["id"], config, client
        )
        self.assertNotEqual(folder, other_folder)
        self.assertNotIn("key", (folder / "run.json").read_text())

    def test_run_analysis_exposes_provider_error_code(self):
        row = self.prepare()
        config = ModelConfig(
            "openai-compatible", "https://example.invalid/v1", "fixture"
        )
        client = Mock()
        for error, code in (
            (ProviderAuthError("认证失败"), "PROVIDER_AUTH_FAILED"),
            (ProviderRateLimitError("限流"), "PROVIDER_RATE_LIMITED"),
        ):
            with self.subTest(code=code):
                client.analyze.side_effect = error
                outcome = run_analysis(self.store, row["id"], config, client)
                status, folder = outcome
                self.assertEqual(status, "retryable_failed")
                self.assertTrue(folder.exists())
                self.assertEqual(outcome.error_code, code)

    def test_all_role_results_through_desktop_pipeline(self):
        from test_pipeline import FakeClient, documented_record

        from resume_screening.prompts import ROLE_SKILL_FILES

        for role, (skill_directory, _) in ROLE_SKILL_FILES.items():
            with self.subTest(role=role):
                row = self.store.prepare(self.source, role)
                config = ModelConfig(
                    "openai-compatible", "https://example.invalid/v1", "fixture"
                )
                client = FakeClient(
                    json.dumps(documented_record(skill_directory), ensure_ascii=False)
                )
                status, folder = run_analysis(self.store, row["id"], config, client)
                self.assertEqual(status, "succeeded")
                self.assertTrue(
                    (folder / "outputs" / row["id"] / "conclusion.md").exists()
                )
                self.assertEqual(client.calls, 1)


class ModelTests(unittest.TestCase):
    def test_model_list_endpoint_and_common_response_shapes(self):
        config = ModelConfig(
            "openai-compatible", "https://example.invalid/v1", "fixture"
        )
        self.assertEqual(config.models_endpoint, "https://example.invalid/v1/models")
        self.assertEqual(
            _extract_model_names(
                {
                    "data": [
                        {"id": "model-a"},
                        {"name": "model-b"},
                        {"id": "MODEL-A"},
                    ]
                }
            ),
            ["model-a", "model-b"],
        )

    def test_list_models_uses_bearer_key_and_returns_ids(self):
        config = ModelConfig(
            "openai-compatible", "https://example.invalid/v1", "fixture"
        )
        opener = Mock()
        opener.open.return_value = io.BytesIO(
            json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
        )
        client = DesktopModelClient(config, "short-secret", opener=opener)
        self.assertEqual(client.list_models(), ["model-a", "model-b"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/v1/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer short-secret")

    def test_configured_client_loads_persisted_key_when_ui_field_is_blank(self):
        config = ModelConfig(
            "openai-compatible", "https://example.invalid/v1", "fixture"
        )
        backend = Mock()
        backend.get_password.return_value = "saved-secret"
        with patch(
            "resume_screening.desktop_model.credential_backend",
            return_value=backend,
        ):
            client = configured_client(config)
        self.assertEqual(client.key, "saved-secret")
        backend.get_password.assert_called_once_with("ResumeDesk", config.identity)

    def test_minimax_native_envelope_and_business_error(self):
        config = ModelConfig("minimax", "https://example.invalid/v1", "fixture")
        opener = Mock()
        opener.open.return_value = io.BytesIO(
            json.dumps(
                {
                    "base_resp": {"status_code": 0},
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                }
            ).encode()
        )
        client = DesktopModelClient(config, "synthetic", opener=opener)
        self.assertIn("通过", client.test())
        self.assertTrue(
            opener.open.call_args.args[0].full_url.endswith("/text/chatcompletion_v2")
        )
        opener.open.return_value = io.BytesIO(b'{"base_resp":{"status_code":1004}}')
        with self.assertRaises(ProviderAuthError):
            client.test()

    def test_config_saved_without_secret(self):
        from resume_screening.desktop_model import load_config, save_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ModelConfig(
                "openai-compatible", "https://example.invalid/v1", "fixture"
            )
            backend = Mock()
            with patch(
                "resume_screening.desktop_model.credential_backend",
                return_value=backend,
            ):
                save_config(root, config, "synthetic-secret")
            self.assertEqual(load_config(root), config)
            self.assertNotIn("synthetic-secret", (root / "model.json").read_text())
            backend.set_password.assert_called_once_with(
                "ResumeDesk", config.identity, "synthetic-secret"
            )

    def test_config_save_keeps_existing_credential_when_key_field_is_blank(self):
        from resume_screening.desktop_model import save_config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ModelConfig(
                "openai-compatible", "https://example.invalid/v1", "fixture"
            )
            backend = Mock()
            backend.get_password.return_value = "already-saved"
            with patch(
                "resume_screening.desktop_model.credential_backend",
                return_value=backend,
            ):
                save_config(root, config, "")
            backend.set_password.assert_not_called()
            self.assertEqual(json.loads((root / "model.json").read_text())["model"], "fixture")

    def client(self, body):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(body).encode())
        return DesktopModelClient(
            ModelConfig("openai-compatible", "https://example.invalid/v1", "model"),
            "short-secret",
            opener=opener,
        )

    def test_endpoint_and_minimal_payload(self):
        client = self.client({"choices": [{"message": {"content": '{"ok":true}'}}]})
        self.assertIn("通过", client.test())
        request = client.opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://example.invalid/v1/chat/completions"
        )
        self.assertNotIn("max_completion_tokens", json.loads(request.data))
        self.assertEqual(
            ModelConfig("openai-compatible", request.full_url, "model").endpoint,
            request.full_url,
        )

    def test_endpoint_credentials_and_http_rejected(self):
        for url in (
            "http://example.com/v1",
            "https://u:p@example.com/v1",
            "https://example.com/v1?api_key=x",
        ):
            with self.assertRaises(ValueError):
                _ = ModelConfig("openai-compatible", url, "m").endpoint

    def test_redirect_never_follows(self):
        self.assertIsNone(
            NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://elsewhere.invalid"
            )
        )

    def test_errors_do_not_expose_key_and_never_retry(self):
        for code, error in (
            (401, ProviderAuthError),
            (429, ProviderRateLimitError),
            (503, AmbiguousModelError),
        ):
            client = self.client({})
            client.opener.open.side_effect = urllib.error.HTTPError(
                "https://example.invalid", code, "short-secret", {}, None
            )
            with self.assertRaises(error) as caught:
                client.test()
            self.assertNotIn("short-secret", str(caught.exception))
            self.assertEqual(client.opener.open.call_count, 1)

    def test_truncation_not_success(self):
        client = self.client(
            {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]}
        )
        with self.assertRaises(AmbiguousModelError):
            client.test()

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from resume_screening.minimax import (
    MiniMaxClient,
    ModelCallError,
    RetryableModelError,
)


class FakeHttpResponse:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class MiniMaxClientTests(unittest.TestCase):
    def test_endpoint_must_use_https(self):
        with self.assertRaises(ValueError):
            MiniMaxClient(api_key="test-key", endpoint="http://example.test/chat")

    def test_http_error_does_not_keep_provider_body_in_exception(self):
        error = urllib.error.HTTPError(
            "https://example.test/chat",
            401,
            "Unauthorized",
            {},
            io.BytesIO("简历正文 13812345678 candidate@example.com".encode()),
        )
        client = MiniMaxClient(api_key="test-key")
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(RetryableModelError) as caught,
        ):
            client.analyze(system_prompt="policy", resume_text="resume")

        self.assertNotIn("13812345678", str(caught.exception))
        self.assertNotIn("candidate@example.com", str(caught.exception))

    def test_default_request_uses_domestic_endpoint(self):
        response = FakeHttpResponse(
            {
                "id": "response-1",
                "choices": [{"message": {"content": "{}"}}],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )
        urlopen = Mock(return_value=response)
        client = MiniMaxClient(api_key="test-key")

        with patch("urllib.request.urlopen", urlopen):
            client.analyze(system_prompt="policy", resume_text="resume")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.minimaxi.com/v1/text/chatcompletion_v2",
        )

    def test_environment_base_builds_domestic_text_endpoint(self):
        response = FakeHttpResponse(
            {
                "id": "response-1",
                "choices": [{"message": {"content": "{}"}}],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )
        urlopen = Mock(return_value=response)

        with (
            patch.dict(
                "os.environ",
                {"MINIMAX_API_BASE": "https://api.minimaxi.com/v1"},
                clear=True,
            ),
            patch("urllib.request.urlopen", urlopen),
        ):
            MiniMaxClient(api_key="test-key").analyze(
                system_prompt="policy", resume_text="resume"
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.minimaxi.com/v1/text/chatcompletion_v2",
        )

    def test_environment_can_override_endpoint_for_other_regions(self):
        response = FakeHttpResponse(
            {
                "id": "response-1",
                "choices": [{"message": {"content": "{}"}}],
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
        )
        urlopen = Mock(return_value=response)

        with (
            patch.dict(
                "os.environ",
                {"MINIMAX_API_ENDPOINT": "https://example.test/chat"},
                clear=True,
            ),
            patch("urllib.request.urlopen", urlopen),
        ):
            MiniMaxClient(api_key="test-key").analyze(
                system_prompt="policy", resume_text="resume"
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/chat")

    def test_http_200_rate_limit_business_error_is_retryable_and_diagnostic(self):
        response = FakeHttpResponse(
            {"base_resp": {"status_code": 1002, "status_msg": "rate limit"}}
        )
        client = MiniMaxClient(api_key="test-key")

        with (
            patch("urllib.request.urlopen", return_value=response),
            self.assertRaises(RetryableModelError) as caught,
        ):
            client.analyze(system_prompt="policy", resume_text="resume")

        self.assertEqual(str(caught.exception), "MiniMax API 1002: rate limit")

    def test_http_200_auth_business_error_is_reported_as_rejected(self):
        response = FakeHttpResponse(
            {"base_resp": {"status_code": 1004, "status_msg": "invalid token"}}
        )
        client = MiniMaxClient(api_key="test-key")

        with (
            patch("urllib.request.urlopen", return_value=response),
            self.assertRaises(ModelCallError) as caught,
        ):
            client.analyze(system_prompt="policy", resume_text="resume")

        self.assertEqual(str(caught.exception), "MiniMax API 1004: invalid token")


if __name__ == "__main__":
    unittest.main()

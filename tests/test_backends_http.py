#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from backends.copilot_direct import CopilotDirectBackend  # noqa: E402
from backends.gemini import GeminiBackend  # noqa: E402
from core.errors import PartnerError  # noqa: E402
from core.http import _SafeRedirectHandler, post_json  # noqa: E402


class BackendHttpTest(unittest.TestCase):
    def test_cross_origin_redirect_is_rejected(self):
        req = SimpleNamespace(full_url="https://api.githubcopilot.com/models")
        with self.assertRaises(PartnerError) as ctx:
            _SafeRedirectHandler().redirect_request(
                req, None, 302, "Found", {}, "https://example.com/models"
            )
        self.assertEqual(ctx.exception.code, "partner_unreachable")

    def test_relative_cross_origin_redirect_is_rejected(self):
        req = SimpleNamespace(full_url="https://api.githubcopilot.com/models")
        with self.assertRaises(PartnerError):
            _SafeRedirectHandler().redirect_request(
                req, None, 302, "Found", {}, "//example.com/models"
            )

    def test_https_to_http_redirect_is_rejected(self):
        req = SimpleNamespace(full_url="https://api.githubcopilot.com/models")
        with self.assertRaises(PartnerError):
            _SafeRedirectHandler().redirect_request(
                req, None, 302, "Found", {}, "http://api.githubcopilot.com/models"
            )

    def test_invalid_auth_header_error_does_not_leak_secret(self):
        with self.assertRaises(PartnerError) as ctx:
            post_json(
                "https://example.com", {}, headers={"Authorization": "Bearer secret\n"}, timeout=1
            )
        self.assertEqual(ctx.exception.code, "invalid_config")
        self.assertNotIn("secret", ctx.exception.to_text())

    def test_copilot_model_cache_checks_session_first(self):
        backend = CopilotDirectBackend({"github_oauth_token": "oauth"}, {}, 10)
        backend._models_cache = [{"id": "cached"}]
        with patch.object(
            backend, "_session", return_value=("https://api.githubcopilot.com", "session")
        ) as session:
            self.assertEqual(backend._model_entries(), [{"id": "cached"}])
        session.assert_called_once_with()

    def test_copilot_list_models_serves_catalog_cache(self):
        backend = CopilotDirectBackend({"github_oauth_token": "oauth"}, {}, 10)
        backend._models_cache = [{"id": "cached-model"}]
        with patch.object(
            backend, "_session", return_value=("https://api.githubcopilot.com", "session")
        ):
            self.assertEqual(backend.list_models(), ["cached-model"])

    def test_copilot_list_models_refresh_clears_catalog_cache(self):
        backend = CopilotDirectBackend({"github_oauth_token": "oauth"}, {}, 10)
        backend._models_cache = [{"id": "stale-model"}]
        fetched = []

        def fake_get(url, **kwargs):
            fetched.append(url)
            return {"data": [{"id": "fresh-model"}]}

        with (
            patch.object(
                backend, "_session", return_value=("https://api.githubcopilot.com", "session")
            ),
            patch("backends.copilot_direct.get_json", side_effect=fake_get),
        ):
            self.assertEqual(backend.list_models(refresh=True), ["fresh-model"])
            self.assertEqual(fetched, ["https://api.githubcopilot.com/models"])
            # The fresh catalog is cached again for non-refresh calls.
            self.assertEqual(backend.list_models(), ["fresh-model"])
            self.assertEqual(len(fetched), 1)

    def test_gemini_model_listing_follows_pagination(self):
        backend = GeminiBackend({"gemini_api_key": "secret"}, {"ipv4_only": False}, 10)
        pages = [
            {"models": [{"name": "models/b"}], "nextPageToken": "next"},
            {"models": [{"name": "models/a"}]},
        ]
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return pages.pop(0)

        with patch("backends.gemini.get_json", side_effect=fake_get):
            self.assertEqual(backend.list_models(), ["a", "b"])
        self.assertEqual(len(calls), 2)
        self.assertIn("pageToken=next", calls[1])


if __name__ == "__main__":
    unittest.main()

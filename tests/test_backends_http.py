#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import http.client
import importlib.util
import io
import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from backends.copilot_direct import CopilotDirectBackend  # noqa: E402
from backends.gemini import GeminiBackend  # noqa: E402
from backends.openai_compatible import OpenAICompatibleBackend  # noqa: E402
from core.errors import PartnerError  # noqa: E402
from core.http import _SafeRedirectHandler, post_json  # noqa: E402

FAKE_SERVER = ROOT / "tools" / "fake_openai_server.py"


def _load_fake_server_module():
    spec = importlib.util.spec_from_file_location("fake_openai_server", FAKE_SERVER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {FAKE_SERVER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _load_fake_server_module()


class BackendHttpTest(unittest.TestCase):
    def test_cross_origin_redirect_is_rejected(self):
        req = urllib.request.Request("https://api.githubcopilot.com/models")
        with self.assertRaises(PartnerError) as ctx:
            _SafeRedirectHandler().redirect_request(
                req,
                io.BytesIO(b""),
                302,
                "Found",
                http.client.HTTPMessage(),
                "https://example.com/models",
            )
        self.assertEqual(ctx.exception.code, "partner_unreachable")

    def test_relative_cross_origin_redirect_is_rejected(self):
        req = urllib.request.Request("https://api.githubcopilot.com/models")
        with self.assertRaises(PartnerError):
            _SafeRedirectHandler().redirect_request(
                req,
                io.BytesIO(b""),
                302,
                "Found",
                http.client.HTTPMessage(),
                "//example.com/models",
            )

    def test_https_to_http_redirect_is_rejected(self):
        req = urllib.request.Request("https://api.githubcopilot.com/models")
        with self.assertRaises(PartnerError):
            _SafeRedirectHandler().redirect_request(
                req,
                io.BytesIO(b""),
                302,
                "Found",
                http.client.HTTPMessage(),
                "http://api.githubcopilot.com/models",
            )

    def test_invalid_auth_header_error_does_not_leak_secret(self):
        with self.assertRaises(PartnerError) as ctx:
            post_json(
                "https://example.com", {}, headers={"Authorization": "Bearer secret\n"}, timeout=1
            )
        self.assertEqual(ctx.exception.code, "invalid_config")
        self.assertNotIn("secret", ctx.exception.to_text())

    def test_malformed_response_json_maps_to_backend_error(self):
        # Regression pin for the except ordering in core.http.request_json:
        # JSONDecodeError is a ValueError subclass, so a malformed response
        # body must classify as backend_error, never invalid_config.
        with (
            patch("core.http._OPENER.open", return_value=io.BytesIO(b"<html>not json</html>")),
            self.assertRaises(PartnerError) as ctx,
        ):
            post_json("https://example.com", {})
        self.assertEqual(ctx.exception.code, "backend_error")

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


class OpenAICompatibleLiveTest(unittest.TestCase):
    """Drive OpenAICompatibleBackend against the threaded fake endpoint."""

    def setUp(self):
        self.server = FAKE.build_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _backend(self, base_url=None):
        return OpenAICompatibleBackend(
            {"api_key": "fake-test-key"},
            {"openai": {"base_url": base_url or self.base_url}},
            10,
        )

    def _complete_ex(self, backend, model="fake-model-a"):
        return backend.complete_ex(
            [{"role": "user", "content": "ping"}],
            system_prompt="sys",
            model=model,
            generation={},
        )

    def test_healthz_reports_ready(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5) as res:
            self.assertEqual(res.status, 200)

    def test_list_models_returns_fake_ids(self):
        self.assertEqual(self._backend().list_models(), ["fake-model-a", "fake-model-b"])

    def test_complete_ex_reports_echoed_model(self):
        result = self._complete_ex(self._backend())
        self.assertTrue(result.text.startswith("fake-ok:"))
        self.assertIn("ping", result.text)
        # Without --served-model the fake echoes the requested model.
        self.assertEqual(result.model_used, "fake-model-a")

    def test_served_model_override_wins_over_request(self):
        server = FAKE.build_server(0, served_model="fake-served-model")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            backend = self._backend(f"http://127.0.0.1:{port}/v1")
            result = self._complete_ex(backend, model="fake-model-a")
            # Provenance verdict: model_used carries the response-side
            # model verbatim, which may differ from the requested one.
            self.assertEqual(result.model_used, "fake-served-model")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_unknown_path_returns_json_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)
        body = json.loads(ctx.exception.read())
        self.assertIn("error", body)

    def test_unreachable_endpoint_raises_partner_unreachable(self):
        # Bound but never listen()ing: the port stays taken for the
        # whole test (no bind-then-close reclaim window) while the
        # kernel refuses connections.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        try:
            port = sock.getsockname()[1]
            backend = self._backend(f"http://127.0.0.1:{port}/v1")
            with self.assertRaises(PartnerError) as ctx:
                self._complete_ex(backend)
            self.assertEqual(ctx.exception.code, "partner_unreachable")
            with self.assertRaises(PartnerError) as ctx:
                backend.list_models()
            self.assertEqual(ctx.exception.code, "partner_unreachable")
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()

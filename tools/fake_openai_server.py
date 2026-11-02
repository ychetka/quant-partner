#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Deterministic fake OpenAI-compatible endpoint for no-secret testing.

Binds to 127.0.0.1 only. Implements the minimal surface the
``openai-compatible`` backend needs: ``GET /healthz`` for readiness
polling, ``GET <prefix>/models`` for discovery and
``POST <prefix>/chat/completions`` for completions. Any ``<prefix>``
(such as ``/v1``) is accepted. Responses are small, deterministic and
contain no credential-shaped strings, so the server is safe to run in
CI and on a developer loopback without secrets. The ``Authorization``
header, when present, is ignored.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 4143
# Fixed timestamp keeps completion responses deterministic.
CREATED = 1_700_000_000
MAX_CONTENT_CHARS = 80
MAX_BODY_BYTES = 1_000_000
MODELS_PAYLOAD: dict[str, Any] = {
    "object": "list",
    "data": [
        {"id": "fake-model-a", "object": "model"},
        {"id": "fake-model-b", "object": "model"},
    ],
}


def _first_user_content(payload: Any) -> str:
    """Return the content of the first ``user`` message, or ``""``."""
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _completion_payload(served_model: str, user_content: str) -> dict[str, Any]:
    snippet = user_content[:MAX_CONTENT_CHARS]
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": CREATED,
        "model": served_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"fake-ok: {snippet}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _error_payload(status: int, message: str) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": f"http_{status}",
        }
    }


def make_handler(served_model: str | None = None) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to one served-model policy.

    When ``served_model`` is ``None`` the response ``model`` echoes the
    requested model back, like most real endpoints; a fixed value makes
    the response report that name regardless of the request.
    """

    class _Handler(BaseHTTPRequestHandler):
        server_version = "fake-openai/1.0"

        def log_message(self, *args: Any) -> None:
            # Keep CI and test logs clean; readiness is probed via /healthz.
            return

        def _path(self) -> str:
            path = self.path.split("?", 1)[0].rstrip("/")
            return path or "/"

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _not_found(self) -> None:
            self._send_json(404, _error_payload(404, "unknown path"))

        def do_GET(self) -> None:
            path = self._path()
            if path == "/healthz":
                self._send_json(200, {"status": "ok"})
            elif path.endswith("/models"):
                self._send_json(200, MODELS_PAYLOAD)
            else:
                self._not_found()

        def do_POST(self) -> None:
            path = self._path()
            if not path.endswith("/chat/completions"):
                self._not_found()
                return
            content_length = self.headers.get("Content-Length")
            if content_length is not None:
                try:
                    length = int(content_length)
                except ValueError:
                    length = 0
                if length > MAX_BODY_BYTES:
                    self._send_json(413, _error_payload(413, "request body too large"))
                    return
                raw = self.rfile.read(length) if length > 0 else b""
            else:
                # No declared length: read one byte past the cap so
                # oversized bodies are rejected instead of clamped.
                raw = self.rfile.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    self._send_json(413, _error_payload(413, "request body too large"))
                    return
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, _error_payload(400, "request body is not valid JSON"))
                return
            requested = payload.get("model") if isinstance(payload, dict) else ""
            served = served_model if served_model else str(requested or "")
            self._send_json(200, _completion_payload(served, _first_user_content(payload)))

    return _Handler


def build_server(port: int = DEFAULT_PORT, served_model: str | None = None) -> ThreadingHTTPServer:
    """Return a not-yet-serving threaded HTTP server on 127.0.0.1:``port``.

    ``port=0`` picks a free ephemeral port, which tests read back from
    ``server.server_address[1]``.
    """
    return ThreadingHTTPServer((BIND_HOST, port), make_handler(served_model))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fake OpenAI-compatible endpoint bound to 127.0.0.1 for testing."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port to bind on 127.0.0.1 (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--served-model",
        default=None,
        help="model id reported in completion responses; default echoes the requested model back",
    )
    args = parser.parse_args()
    server = build_server(args.port, args.served_model)
    host, port = server.server_address[:2]
    print(f"fake-openai serving on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

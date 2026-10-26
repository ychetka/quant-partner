#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from core.errors import PartnerError  # noqa: E402
from core.mcp import handle_message  # noqa: E402


class DummyServer:
    def server_info(self):
        return {"name": "dummy", "version": "test"}

    def tools(self):
        return []

    def call_tool(self, name, arguments):
        raise PartnerError("invalid_request", f"unknown tool: {name}")

    def secrets(self):
        return []


class LeakyServer(DummyServer):
    def call_tool(self, name, arguments):
        raise ValueError("Authorization: Bearer secret\\n")

    def secrets(self):
        return ["secret\n"]


class McpProtocolTest(unittest.TestCase):
    def _handle(self, message, server=None):
        stream = io.StringIO()
        with patch.object(sys, "stdout", stream):
            handle_message(server or DummyServer(), message)
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    def test_non_dict_frame_is_ignored(self):
        self.assertEqual(self._handle([]), [])

    def test_initialize_with_bad_params(self):
        out = self._handle({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": "bad"})
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "dummy")

    def test_tools_call_with_bad_params_returns_tool_error(self):
        out = self._handle({"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": "bad"})
        result = out[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("ERROR invalid_request", result["content"][0]["text"])

    def test_internal_error_does_not_leak_exception_text(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            handle_message(
                LeakyServer(),
                {"jsonrpc": "2.0", "id": "call", "method": "tools/call", "params": {"name": "x", "arguments": {}}},
            )
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("Authorization", output)
        self.assertNotIn("secret", output)
        self.assertIn("internal_error", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

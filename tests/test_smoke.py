#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tools" / "mcp_smoke.py"


def load_smoke_module():
    spec = importlib.util.spec_from_file_location("mcp_smoke", SMOKE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SMOKE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_completed(stdout, stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class SmokeClientTest(unittest.TestCase):
    def _env(self):
        env = os.environ.copy()
        env["PARTNER_ID"] = "gh-copilot-partner"
        env["PARTNER_CONFIG"] = str(ROOT / "partners" / "config.example.json")
        return env

    def _run_main(self, argv, completed):
        module = load_smoke_module()
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"PARTNER_ID": "gh-copilot-partner"}),
            patch.object(sys, "argv", argv),
            patch.object(subprocess, "run", return_value=completed),
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = module.main()
        return code, out.getvalue(), err.getvalue()

    def test_list_succeeds_with_example_config(self):
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--list"],
            cwd=ROOT,
            env=self._env(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn('"id": "init"', proc.stdout)
        self.assertIn('"id": "tools"', proc.stdout)

    def test_tool_error_returns_nonzero(self):
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--call", "unknown_tool"],
            cwd=ROOT,
            env=self._env(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn('"isError": true', proc.stdout)

    def test_polluted_stdout_fails_without_echoing_line(self):
        secret_line = "debug banner: Bearer TOPSECRET"
        stdout = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": "init", "result": {}}),
                secret_line,
                json.dumps({"jsonrpc": "2.0", "id": "tools", "result": {"tools": []}}),
                "",
            ]
        )
        code, out, err = self._run_main(["mcp_smoke.py", "--list"], fake_completed(stdout))
        self.assertNotEqual(code, 0)
        self.assertNotIn("TOPSECRET", out)
        self.assertNotIn("TOPSECRET", err)
        self.assertIn('"id": "init"', out)

    def test_missing_response_fails(self):
        stdout = json.dumps({"jsonrpc": "2.0", "id": "init", "result": {}}) + "\n"
        code, _, err = self._run_main(["mcp_smoke.py", "--list"], fake_completed(stdout))
        self.assertNotEqual(code, 0)
        self.assertIn("tools", err)

    def test_empty_stdout_fails(self):
        code, _, _ = self._run_main(["mcp_smoke.py", "--list"], fake_completed(""))
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

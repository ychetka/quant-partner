#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SmokeClientTest(unittest.TestCase):
    def test_tool_error_returns_nonzero(self):
        env = os.environ.copy()
        env["PARTNER_ID"] = "gh-copilot-partner"
        env["PARTNER_CONFIG"] = str(ROOT / "partners" / "config.example.json")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "mcp_smoke.py"), "--call", "unknown_tool"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn('"isError": true', proc.stdout)


if __name__ == "__main__":
    unittest.main()

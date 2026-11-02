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


def _response(rid, result):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


def _error_artifact_text(code="unknown_model"):
    return json.dumps(
        {
            "schema_version": "1.0",
            "status": "error",
            "advisory": True,
            "review_id": "rev-smoke",
            "idempotency_key": "a" * 64,
            "cached": False,
            "head_sha": "0" * 40,
            "base_sha": "0" * 40,
            "provenance": {
                "partner_id": "code-reviewer",
                "backend": "copilot-direct",
                "model_alias": "(unresolved)",
                "model_requested": "(unresolved)",
                "model_used": "",
                "strict_model": True,
                "model_match": False,
                "discovery_confirmed": False,
                "prompt_version": "2026-11-02",
                "partner_version": "0.3.0",
                "generated_at_utc": "2026-11-02T12:00:00Z",
            },
            "verdict": "no_findings",
            "summary": "",
            "findings": [],
            "dropped_findings": [],
            "unreviewed": [],
            "warnings": [],
            "errors": [{"code": code, "message": "model alias is not allowlisted"}],
        }
    )


class SmokeArgumentJsonTest(unittest.TestCase):
    def _run_main(self, argv, completed):
        module = load_smoke_module()
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"PARTNER_ID": "code-reviewer"}),
            patch.object(sys, "argv", argv),
            patch.object(subprocess, "run", return_value=completed),
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = module.main()
        return code, out.getvalue(), err.getvalue()

    def test_malformed_arguments_json_fails_closed(self):
        for bad in ("{not json", "]", ""):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as ctx:
                self._run_main(
                    ["mcp_smoke.py", "--call", "review_code", "--arguments-json", bad],
                    fake_completed(""),
                )
            self.assertNotEqual(ctx.exception.code, 0)

    def test_non_object_arguments_json_fails_closed(self):
        for bad in ("[1, 2]", '"text"', "42", "null"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as ctx:
                self._run_main(
                    ["mcp_smoke.py", "--call", "review_code", "--arguments-json", bad],
                    fake_completed(""),
                )
            self.assertNotEqual(ctx.exception.code, 0)

    def test_arguments_json_requires_call(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run_main(["mcp_smoke.py", "--list", "--arguments-json", "{}"], fake_completed(""))
        self.assertNotEqual(ctx.exception.code, 0)

    def test_artifact_expectation_requires_call(self):
        with self.assertRaises(SystemExit):
            self._run_main(
                ["mcp_smoke.py", "--list", "--expect-artifact-status", "ok"],
                fake_completed(""),
            )


class SmokeToolSurfaceTest(unittest.TestCase):
    TOOLS = _response(
        "tools",
        {
            "tools": [
                {"name": "partner_info"},
                {"name": "list_partner_skills"},
                {"name": "list_reviewer_models"},
                {"name": "review_code"},
            ]
        },
    )

    def _run_main(self, argv, completed):
        module = load_smoke_module()
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"PARTNER_ID": "code-reviewer"}),
            patch.object(sys, "argv", argv),
            patch.object(subprocess, "run", return_value=completed),
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = module.main()
        return code, out.getvalue(), err.getvalue()

    def test_expect_and_forbid_tools_pass(self):
        stdout = _response("init", {}) + "\n" + self.TOOLS + "\n"
        code, _, _ = self._run_main(
            [
                "mcp_smoke.py",
                "--expect-tool",
                "review_code",
                "--expect-tool",
                "list_reviewer_models",
                "--forbid-tool",
                "ask_code_partner",
                "--forbid-tool",
                "list_partner_models",
            ],
            fake_completed(stdout),
        )
        self.assertEqual(code, 0)

    def test_missing_expected_tool_fails(self):
        stdout = _response("init", {}) + "\n" + self.TOOLS + "\n"
        code, _, err = self._run_main(
            ["mcp_smoke.py", "--expect-tool", "ask_code_partner"],
            fake_completed(stdout),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("expected tool", err)

    def test_forbidden_tool_present_fails(self):
        leaked = _response(
            "tools",
            {"tools": [{"name": "review_code"}, {"name": "ask_product_partner"}]},
        )
        stdout = _response("init", {}) + "\n" + leaked + "\n"
        code, _, err = self._run_main(
            ["mcp_smoke.py", "--forbid-tool", "ask_product_partner"],
            fake_completed(stdout),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("forbidden tool", err)


class SmokeArtifactCheckTest(unittest.TestCase):
    def _run_main(self, argv, completed):
        module = load_smoke_module()
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"PARTNER_ID": "code-reviewer"}),
            patch.object(sys, "argv", argv),
            patch.object(subprocess, "run", return_value=completed),
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = module.main()
        return code, out.getvalue(), err.getvalue()

    def _call_stdout(self, text):
        return (
            _response("init", {})
            + "\n"
            + _response("call", {"content": [{"type": "text", "text": text}]})
            + "\n"
        )

    def test_artifact_status_and_error_code_pass(self):
        stdout = self._call_stdout(_error_artifact_text())
        code, _, _ = self._run_main(
            [
                "mcp_smoke.py",
                "--call",
                "review_code",
                "--arguments-json",
                "{}",
                "--expect-artifact-status",
                "error",
                "--expect-error-code",
                "unknown_model",
            ],
            fake_completed(stdout),
        )
        self.assertEqual(code, 0)

    def test_artifact_status_mismatch_fails(self):
        stdout = self._call_stdout(_error_artifact_text())
        code, _, err = self._run_main(
            [
                "mcp_smoke.py",
                "--call",
                "review_code",
                "--expect-artifact-status",
                "ok",
            ],
            fake_completed(stdout),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("status", err)

    def test_missing_error_code_fails(self):
        stdout = self._call_stdout(_error_artifact_text())
        code, _, err = self._run_main(
            [
                "mcp_smoke.py",
                "--call",
                "review_code",
                "--expect-error-code",
                "model_unconfirmed",
            ],
            fake_completed(stdout),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("error code", err)

    def test_unparseable_call_text_fails_without_echo(self):
        stdout = self._call_stdout("SENTINEL artifact junk")
        code, _, err = self._run_main(
            [
                "mcp_smoke.py",
                "--call",
                "review_code",
                "--expect-artifact-status",
                "error",
            ],
            fake_completed(stdout),
        )
        self.assertNotEqual(code, 0)
        self.assertNotIn("SENTINEL", err)

    def test_schema_invalid_call_text_fails_without_echo(self):
        payload = json.loads(_error_artifact_text())
        payload["SENTINEL_KEY"] = "SENTINEL_VALUE"
        stdout = self._call_stdout(json.dumps(payload))
        code, _, err = self._run_main(
            [
                "mcp_smoke.py",
                "--call",
                "review_code",
                "--expect-artifact-status",
                "error",
            ],
            fake_completed(stdout),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("schema", err)
        self.assertNotIn("SENTINEL_VALUE", err)
        self.assertNotIn("SENTINEL_KEY", err)

    def test_unknown_key_name_not_echoed_to_stderr(self):
        # Schema issue paths embed the offending key, which can be a
        # secret-like attacker-controlled name; stderr must carry only
        # stable issue codes.
        secret_key = "topsecret_do_not_leak_key"
        payload = json.loads(_error_artifact_text())
        payload[secret_key] = "value"
        stdout = self._call_stdout(json.dumps(payload))
        code, _, err = self._run_main(
            [
                "mcp_smoke.py",
                "--call",
                "review_code",
                "--expect-artifact-status",
                "error",
            ],
            fake_completed(stdout),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("unknown_key", err)
        self.assertNotIn(secret_key, err)


class ReviewSmokeEndToEndTest(unittest.TestCase):
    """Real subprocess smoke against the review toolset; no provider access."""

    def _env(self):
        env = os.environ.copy()
        env["PARTNER_ID"] = "code-reviewer"
        env["PARTNER_CONFIG"] = str(ROOT / "partners" / "config.example.json")
        return env

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(SMOKE), *argv],
            cwd=ROOT,
            env=self._env(),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def test_review_tool_surface(self):
        proc = self._run(
            "--expect-tool",
            "review_code",
            "--expect-tool",
            "list_reviewer_models",
            "--forbid-tool",
            "ask_code_partner",
            "--forbid-tool",
            "ask_product_partner",
            "--forbid-tool",
            "list_partner_models",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn('"review_code"', proc.stdout)

    def test_list_reviewer_models_call(self):
        proc = self._run("--call", "list_reviewer_models")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_review_code_unknown_alias_artifact(self):
        arguments = json.dumps(
            {
                "head_sha": "1" * 40,
                "base_sha": "2" * 40,
                "diff": "diff --git a/f.py b/f.py\n"
                "index 1..2 100644\n--- a/f.py\n+++ b/f.py\n"
                "@@ -1,1 +1,1 @@\n-old\n+new\n",
                "model": "definitely-not-allowlisted",
            }
        )
        proc = self._run(
            "--call",
            "review_code",
            "--arguments-json",
            arguments,
            "--expect-artifact-status",
            "error",
            "--expect-error-code",
            "unknown_model",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()

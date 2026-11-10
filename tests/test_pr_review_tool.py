#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

PR_REVIEW = ROOT / "tools" / "pr_review.py"


def load_module():
    spec = importlib.util.spec_from_file_location("pr_review", PR_REVIEW)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {PR_REVIEW}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pr = load_module()

HEAD = "b" * 40
BASE = "c" * 40

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 ctx_one
-removed
+added_one
+added_two
 ctx_last
"""


def _artifact(findings=None, verdict="no_findings", summary="looks fine", **extra):
    artifact = {
        "schema_version": "1.0",
        "status": "ok",
        "advisory": True,
        "review_id": "rev-test",
        "idempotency_key": "0" * 64,
        "cached": False,
        "head_sha": HEAD,
        "base_sha": BASE,
        "provenance": {
            "partner_id": "auto-reviewer",
            "backend": "gemini",
            "model_alias": "gemini",
            "model_requested": "gemini-3.5-flash",
            "model_used": "gemini-3.5-flash",
            "strict_model": True,
            "model_match": True,
            "discovery_confirmed": True,
            "prompt_version": "2026-11-04",
            "partner_version": "0.3.1",
            "generated_at_utc": "2026-01-01T00:00:00Z",
        },
        "verdict": verdict,
        "summary": summary,
        "findings": findings or [],
        "dropped_findings": [],
        "unreviewed": [],
        "warnings": [],
        "errors": [],
    }
    artifact.update(extra)
    return artifact


def _error_artifact(code="backend_error", message="provider unavailable"):
    return _artifact(
        status="error",
        verdict="no_findings",
        summary="",
        errors=[{"code": code, "message": message}],
    )


def _finding(path="src/app.py", line=2, end_line=2, severity="major", title="bug"):
    return {
        "id": "F1",
        "severity": severity,
        "category": "correctness",
        "path": path,
        "line": line,
        "end_line": end_line,
        "side": "RIGHT",
        "title": title,
        "body": "details about the problem",
        "confidence": "high",
        "suggested_fix": "fix it",
    }


class PrReviewToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = str(Path(self.tmp.name) / "review.json")
        self.diff_file = Path(self.tmp.name) / "diff.txt"
        self.diff_file.write_text(DIFF, encoding="utf-8")
        self.stderr = io.StringIO()
        stderr_patch = patch.object(sys, "stderr", self.stderr)
        stderr_patch.start()
        self.addCleanup(stderr_patch.stop)

    def _argv(self, *extra):
        return [
            "--diff-file",
            str(self.diff_file),
            "--commit-id",
            HEAD,
            "--out",
            self.out,
            *extra,
        ]

    def _payload(self):
        return json.loads(Path(self.out).read_text(encoding="utf-8"))

    # GWT-1: dry-run emits an offline skeleton with a stub verdict
    def test_dry_run_emits_skeleton_without_partner(self):
        with patch.object(pr, "call_review") as call:
            code = pr.main(self._argv("--dry-run"))
        self.assertEqual(code, 0)
        call.assert_not_called()
        payload = self._payload()
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertTrue(payload["body"].startswith("AUTO_REVIEW_VERDICT: DRY_RUN"))
        self.assertNotIn("APPROVE", payload["body"])
        self.assertIn("dry run", payload["body"].lower())
        self.assertEqual(payload["comments"], [])

    # GWT-1b: a stubbed ok artifact becomes a COMMENT review with comments
    def test_ok_artifact_maps_to_review_payload(self):
        findings = [
            _finding(line=2, end_line=3, severity="major", title="race on counter"),
            _finding(line=4, end_line=4, severity="minor", title="nit"),
        ]
        artifact = _artifact(findings=findings, verdict="needs_changes", summary="two issues")
        options = {
            "max_diff_chars": 200_000,
            "max_context_chars": 50_000,
            "max_findings": 20,
            "allowed_models": {"gemini": {"backend_model": "gemini-3.5-flash"}},
        }
        with (
            patch.object(pr, "load_review_options", return_value=options),
            patch.object(pr, "call_review", return_value=json.dumps(artifact)) as call,
        ):
            code = pr.main(self._argv("--base-sha", BASE))
        self.assertEqual(code, 0)
        (arguments,) = call.call_args.args
        self.assertEqual(arguments["head_sha"], HEAD)
        self.assertEqual(arguments["base_sha"], BASE)
        self.assertEqual(arguments["model"], "gemini")
        self.assertEqual(arguments["diff"], DIFF)
        payload = self._payload()
        self.assertTrue(payload["body"].startswith("AUTO_REVIEW_VERDICT: REQUEST_CHANGES"))
        self.assertIn("two issues", payload["body"])
        self.assertEqual(len(payload["comments"]), 2)
        multi = payload["comments"][0]
        self.assertEqual(multi["path"], "src/app.py")
        self.assertEqual(multi["line"], 3)
        self.assertEqual(multi["side"], "RIGHT")
        self.assertEqual(multi["start_line"], 2)
        self.assertEqual(multi["start_side"], "RIGHT")
        self.assertTrue(multi["body"].startswith("[WARNING]"))
        self.assertTrue(payload["comments"][1]["body"].startswith("[MINOR]"))

    # GWT-2: partner error artifact still yields review.json, soft-fail exit 0
    def test_partner_error_artifact_soft_fails(self):
        with patch.object(pr, "call_review", return_value=json.dumps(_error_artifact())):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        payload = self._payload()
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["comments"], [])
        self.assertIn("AUTO_REVIEW_VERDICT: ERROR", payload["body"])
        self.assertIn("backend_error", payload["body"])

    # GWT-2b: partner exception also soft-fails; --strict flips the exit code
    def test_partner_exception_soft_fails_and_strict_fails(self):
        with patch.object(pr, "call_review", side_effect=RuntimeError("boom")):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        payload = self._payload()
        self.assertIn("AUTO_REVIEW_VERDICT: ERROR", payload["body"])
        self.assertIn("partner call failed", payload["body"])

        with patch.object(pr, "call_review", side_effect=RuntimeError("boom")):
            code = pr.main(self._argv("--strict"))
        self.assertEqual(code, 1)
        # The advisory payload is still written even under --strict.
        payload = self._payload()
        self.assertIn("AUTO_REVIEW_VERDICT: ERROR", payload["body"])

    # GWT-2c: unparseable partner output is a failure, not a crash
    def test_unparseable_partner_output_soft_fails(self):
        with patch.object(pr, "call_review", return_value="not json at all"):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        payload = self._payload()
        self.assertIn("AUTO_REVIEW_VERDICT: ERROR", payload["body"])
        self.assertIn("unparseable", payload["body"])

    # GWT-3: findings on lines outside the diff are moved into the body
    def test_out_of_diff_findings_relocate_to_body(self):
        findings = [
            _finding(path="src/app.py", line=2, end_line=2, title="anchored"),
            _finding(path="src/other.py", line=42, end_line=42, title="far away"),
            _finding(path="src/app.py", line=999, end_line=999, title="no such line"),
        ]
        artifact = _artifact(findings=findings, verdict="needs_changes")
        with patch.object(pr, "call_review", return_value=json.dumps(artifact)):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        payload = self._payload()
        self.assertEqual(len(payload["comments"]), 1)
        self.assertEqual(payload["comments"][0]["path"], "src/app.py")
        body = payload["body"]
        self.assertIn("outside the diff", body)
        self.assertIn("far away", body)
        self.assertIn("no such line", body)

    # GWT-4: oversized diffs are truncated per review options and noted
    def test_diff_truncation_is_bounded_and_noted(self):
        options = {
            "max_diff_chars": 60,
            "max_context_chars": 50_000,
            "max_findings": 20,
            "allowed_models": {"gemini": {"backend_model": "gemini-3.5-flash"}},
        }
        artifact = _artifact()
        with (
            patch.object(pr, "load_review_options", return_value=options),
            patch.object(pr, "call_review", return_value=json.dumps(artifact)) as call,
        ):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        (arguments,) = call.call_args.args
        self.assertLessEqual(len(arguments["diff"]), 61)
        self.assertLess(len(arguments["diff"]), len(DIFF))
        payload = self._payload()
        self.assertIn("truncated", payload["body"].lower())

    # Supporting contract checks ------------------------------------------------

    def test_empty_diff_short_circuits_without_partner(self):
        self.diff_file.write_text("", encoding="utf-8")
        with patch.object(pr, "call_review") as call:
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        call.assert_not_called()
        payload = self._payload()
        self.assertTrue(payload["body"].startswith("AUTO_REVIEW_VERDICT: APPROVE"))
        self.assertIn("nothing to review", payload["body"])

    def test_invalid_commit_id_is_usage_error(self):
        code = pr.main(["--diff-file", str(self.diff_file), "--commit-id", "nope"])
        self.assertEqual(code, 2)
        self.assertFalse(Path(self.out).exists())

    def test_stdin_diff_input(self):
        artifact = _artifact()
        with (
            patch.object(pr, "call_review", return_value=json.dumps(artifact)),
            patch.object(sys, "stdin", io.StringIO(DIFF)),
        ):
            code = pr.main(["--commit-id", HEAD, "--out", self.out])
        self.assertEqual(code, 0)
        payload = self._payload()
        self.assertTrue(payload["body"].startswith("AUTO_REVIEW_VERDICT:"))

    # Usage accounting ------------------------------------------------------------

    def _usage_lines(self):
        return [
            line
            for line in self.stderr.getvalue().splitlines()
            if line.startswith("auto_review_usage ")
        ]

    def test_artifact_usage_mirrored_and_logged(self):
        artifact = _artifact(
            usage={"prompt_tokens": 12000, "candidates_tokens": 1500, "total_tokens": 13500}
        )
        with patch.object(pr, "call_review", return_value=json.dumps(artifact)):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        payload = self._payload()
        self.assertEqual(
            payload["usage"],
            {
                "prompt_tokens": 12000,
                "candidates_tokens": 1500,
                "total_tokens": 13500,
                "model": "gemini-3.5-flash",
            },
        )
        self.assertIn("Usage: in 12000 / out 1500 / total 13500 tokens", payload["body"])
        lines = self._usage_lines()
        self.assertEqual(len(lines), 1, self.stderr.getvalue())
        self.assertIn("prompt_tokens=12000", lines[0])
        self.assertIn("candidates_tokens=1500", lines[0])
        self.assertIn("total_tokens=13500", lines[0])
        # gemini-3.5-flash is in the price table, so a cost is estimated.
        self.assertIn("est_cost_usd=0.00", lines[0])
        self.assertNotIn("unknown", lines[0])

    def test_missing_usage_logs_unknown_cost(self):
        artifact = _artifact()
        with patch.object(pr, "call_review", return_value=json.dumps(artifact)):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        payload = self._payload()
        self.assertNotIn("usage", payload)
        self.assertNotIn("Usage: in", payload["body"])
        lines = self._usage_lines()
        self.assertEqual(len(lines), 1, self.stderr.getvalue())
        self.assertIn("prompt_tokens=0", lines[0])
        self.assertIn("est_cost_usd=unknown", lines[0])

    def test_unpriced_model_logs_unknown_cost(self):
        artifact = _artifact(
            usage={"prompt_tokens": 100, "candidates_tokens": 10, "total_tokens": 110}
        )
        artifact["provenance"]["model_used"] = "some-other-model"
        artifact["provenance"]["model_requested"] = "some-other-model"
        with patch.object(pr, "call_review", return_value=json.dumps(artifact)):
            code = pr.main(self._argv())
        self.assertEqual(code, 0)
        lines = self._usage_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("prompt_tokens=100", lines[0])
        self.assertIn("est_cost_usd=unknown", lines[0])

    def test_usage_summary_flag_emits_markdown(self):
        review = {
            "usage": {
                "prompt_tokens": 12000,
                "candidates_tokens": 1500,
                "total_tokens": 13500,
                "model": "gemini-3.5-flash",
            }
        }
        path = Path(self.tmp.name) / "review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            code = pr.main(["--usage-summary", str(path)])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("| Input tokens | 12000 |", text)
        self.assertIn("| Output tokens | 1500 |", text)
        self.assertIn("| Total tokens | 13500 |", text)
        self.assertIn("gemini-3.5-flash", text)
        self.assertIn("Est. cost | $0.007", text)

    def test_usage_summary_without_usage_soft_fails(self):
        path = Path(self.tmp.name) / "review.json"
        path.write_text(json.dumps({"event": "COMMENT"}), encoding="utf-8")
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            code = pr.main(["--usage-summary", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("not reported", stdout.getvalue())

    def test_usage_summary_missing_file_soft_fails(self):
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            code = pr.main(["--usage-summary", str(Path(self.tmp.name) / "missing.json")])
        self.assertEqual(code, 0)
        self.assertIn("not reported", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

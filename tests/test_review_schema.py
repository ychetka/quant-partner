#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from review.schema import (  # noqa: E402
    Issue,
    SchemaError,
    extract_json,
    require_valid,
    validate,
)


def _provenance(**overrides: Any) -> dict[str, Any]:
    provenance = {
        "partner_id": "code-reviewer",
        "backend": "copilot-direct",
        "model_alias": "claude",
        "model_requested": "claude-sonnet-5.5",
        "model_used": "claude-sonnet-5.5",
        "strict_model": True,
        "model_match": True,
        "discovery_confirmed": True,
        "prompt_version": "2026-11-02",
        "partner_version": "0.3.0",
        "generated_at_utc": "2026-11-02T12:00:00Z",
    }
    provenance.update(overrides)
    return provenance


def _finding(**overrides: Any) -> dict[str, Any]:
    finding = {
        "id": "F1",
        "severity": "major",
        "category": "correctness",
        "path": "src/app.py",
        "line": 3,
        "end_line": 3,
        "side": "RIGHT",
        "title": "Possible off-by-one",
        "body": "The loop bound looks wrong.",
        "confidence": "high",
        "suggested_fix": None,
    }
    finding.update(overrides)
    return finding


def _artifact(**overrides: Any) -> dict[str, Any]:
    artifact = {
        "schema_version": "1.0",
        "status": "ok",
        "advisory": True,
        "review_id": "rev-1",
        "idempotency_key": "a" * 64,
        "cached": False,
        "head_sha": "b" * 40,
        "base_sha": "c" * 40,
        "provenance": _provenance(),
        "verdict": "needs_changes",
        "summary": "One issue found.",
        "findings": [_finding()],
        "dropped_findings": [],
        "unreviewed": [],
        "warnings": [],
        "errors": [],
    }
    artifact.update(overrides)
    return artifact


def _codes(issues: list[Issue]) -> set[str]:
    return {issue.code for issue in issues}


class ExtractJsonTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_object(self):
        text = '```json\n{"a": 1}\n```\n'
        self.assertEqual(extract_json(text), {"a": 1})

    def test_fenced_without_language(self):
        text = '```\n{"a": 1}\n```'
        self.assertEqual(extract_json(text), {"a": 1})

    def test_prose_around_fenced_block_rejected(self):
        text = 'Here is the result:\n```json\n{"a": 1}\n```'
        with self.assertRaises(SchemaError):
            extract_json(text)

    def test_prose_after_fence_rejected(self):
        text = '```json\n{"a": 1}\n```\nDone.'
        with self.assertRaises(SchemaError):
            extract_json(text)

    def test_prose_plus_json_rejected(self):
        with self.assertRaises(SchemaError):
            extract_json('Result: {"a": 1}')

    def test_malformed_json_rejected(self):
        for text in ('{"a": ', "{a:1}", '{"a": 1,}', "", "   "):
            with self.subTest(text=text), self.assertRaises(SchemaError):
                extract_json(text)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(SchemaError) as ctx:
            extract_json('{"a": 1, "a": 2}')
        self.assertEqual(ctx.exception.issues[0].code, "duplicate_key")

    def test_nested_duplicate_keys_rejected(self):
        with self.assertRaises(SchemaError):
            extract_json('{"a": {"x": 1, "x": 2}}')

    def test_non_json_constants_rejected(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
            with self.subTest(text=text), self.assertRaises(SchemaError) as ctx:
                extract_json(text)
            self.assertEqual(ctx.exception.issues[0].code, "non_json_constant")

    def test_non_object_json_rejected(self):
        for text in ("[1, 2]", '"text"', "42", "true", "null"):
            with self.subTest(text=text), self.assertRaises(SchemaError):
                extract_json(text)


class ValidateTest(unittest.TestCase):
    def test_valid_artifact(self):
        self.assertEqual(validate(_artifact()), [])
        self.assertIsInstance(require_valid(_artifact()), dict)

    def test_valid_rejected_artifact(self):
        artifact = _artifact(
            status="rejected",
            verdict="no_findings",
            findings=[],
            errors=[{"code": "model_unconfirmed", "message": "not confirmed"}],
        )
        self.assertEqual(validate(artifact), [])

    def test_unknown_top_level_key(self):
        artifact = _artifact(extra_field=1)
        self.assertIn("unknown_key", _codes(validate(artifact)))

    def test_unknown_nested_keys(self):
        artifact = _artifact()
        artifact["provenance"]["extra"] = 1
        artifact["findings"][0]["extra"] = 1
        artifact["errors"] = []
        issues = validate(artifact)
        paths = {issue.path for issue in issues}
        self.assertIn("$.provenance.extra", paths)
        self.assertIn("$.findings[0].extra", paths)

    def test_missing_required_key(self):
        artifact = _artifact()
        del artifact["summary"]
        self.assertIn("required", _codes(validate(artifact)))

    def test_wrong_types(self):
        cases = [
            ({"summary": 5}, "$.summary"),
            ({"findings": "x"}, "$.findings"),
            ({"provenance": []}, "$.provenance"),
            ({"unreviewed": [1]}, "$.unreviewed[0]"),
            ({"warnings": [None]}, "$.warnings[0]"),
        ]
        for patch, path in cases:
            artifact = _artifact(status="error", verdict="no_findings", findings=[])
            artifact["errors"] = [{"code": "c", "message": "m"}]
            artifact.update(patch)
            issues = validate(artifact)
            self.assertTrue(
                any(issue.path == path for issue in issues),
                f"{path} not flagged in {patch}",
            )

    def test_null_not_allowed(self):
        artifact = _artifact()
        artifact["summary"] = None
        artifact["findings"][0]["title"] = None
        self.assertIn("type", _codes(validate(artifact)))

    def test_bool_where_int_expected(self):
        artifact = _artifact()
        artifact["findings"][0]["line"] = True
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.findings[0].line" and i.code == "type" for i in issues))

    def test_int_where_bool_expected(self):
        artifact = _artifact(cached=1)
        artifact["provenance"]["strict_model"] = 0
        issues = validate(artifact)
        self.assertIn("type", _codes(issues))

    def test_invalid_enums(self):
        artifact = _artifact(status="ok")
        artifact["status"] = "weird"
        artifact["findings"][0]["severity"] = "critical"
        artifact["findings"][0]["confidence"] = "sure"
        artifact["findings"][0]["side"] = "LEFT"
        issues = validate(artifact)
        self.assertIn("enum", _codes(issues))
        paths = {i.path for i in issues if i.code == "enum"}
        self.assertIn("$.status", paths)
        self.assertIn("$.findings[0].severity", paths)
        self.assertIn("$.findings[0].side", paths)

    def test_invalid_verdict_and_drop_reason(self):
        artifact = _artifact(verdict="ship_it")
        artifact["dropped_findings"] = [{"id": "F9", "reason": "nope"}]
        self.assertIn("enum", _codes(validate(artifact)))

    def test_sha_format(self):
        for bad in ("x" * 40, "b" * 39, "b" * 41, 12345):
            artifact = _artifact(head_sha=bad)
            issues = validate(artifact)
            self.assertTrue(
                any(i.path == "$.head_sha" and i.code in {"type", "format"} for i in issues),
                f"head_sha {bad!r} not flagged",
            )

    def test_idempotency_key_format(self):
        for bad in ("a" * 63, "a" * 65, "z" * 64, 7):
            artifact = _artifact(idempotency_key=bad)
            issues = validate(artifact)
            self.assertTrue(
                any(i.path == "$.idempotency_key" and i.code in {"type", "format"} for i in issues),
                f"idempotency_key {bad!r} not flagged",
            )

    def test_timestamp_format(self):
        for bad in (
            "not a date",
            "2026-11-02",
            "2026-11-02T12:00:00",
            "02-11-2026T00:00:00Z",
            "2026-11-02T12:00:00z",
            "2026-11-02T12:00:00.5Z",
            "2026-11-02T12:00:00.000000Z",
            "2026-11-02T12:00:00+00:00",
            "2026-11-02T12:00:00-05:00",
            "2026-11-02T12:00:00+05:30",
        ):
            artifact = _artifact()
            artifact["provenance"]["generated_at_utc"] = bad
            issues = validate(artifact)
            self.assertTrue(
                any(
                    i.path == "$.provenance.generated_at_utc" and i.code == "format" for i in issues
                ),
                f"timestamp {bad!r} not flagged",
            )
        artifact = _artifact()
        artifact["provenance"]["generated_at_utc"] = "2026-11-02T12:00:00Z"
        self.assertEqual(validate(artifact), [])

    def test_timestamp_rejects_impossible_calendar_values(self):
        for bad in (
            "2026-13-40T00:00:00Z",
            "2026-02-30T12:00:00Z",
            "2026-11-02T24:00:00Z",
            "2026-11-02T12:60:00Z",
            "2026-11-02T12:00:60Z",
        ):
            artifact = _artifact()
            artifact["provenance"]["generated_at_utc"] = bad
            issues = validate(artifact)
            self.assertTrue(
                any(
                    i.path == "$.provenance.generated_at_utc" and i.code == "format" for i in issues
                ),
                f"timestamp {bad!r} not flagged",
            )

    def test_ok_with_errors_inconsistent(self):
        artifact = _artifact(errors=[{"code": "c", "message": "m"}])
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.errors" and i.code == "inconsistent" for i in issues))

    def test_error_without_errors_inconsistent(self):
        artifact = _artifact(status="error", verdict="no_findings", findings=[], errors=[])
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.errors" and i.code == "inconsistent" for i in issues))

    def test_rejected_with_findings_inconsistent(self):
        artifact = _artifact(
            status="rejected",
            errors=[{"code": "c", "message": "m"}],
        )
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.findings" and i.code == "inconsistent" for i in issues))

    def test_cached_true_on_error_inconsistent(self):
        artifact = _artifact(
            status="error",
            cached=True,
            verdict="no_findings",
            findings=[],
            errors=[{"code": "c", "message": "m"}],
        )
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.cached" and i.code == "inconsistent" for i in issues))

    def test_advisory_must_be_true(self):
        artifact = _artifact(advisory=False)
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.advisory" and i.code == "inconsistent" for i in issues))

    def test_verdict_consistent_with_findings(self):
        artifact = _artifact(verdict="no_findings")
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.verdict" and i.code == "inconsistent" for i in issues))
        artifact = _artifact(verdict="needs_changes", findings=[])
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.verdict" and i.code == "inconsistent" for i in issues))

    def test_model_match_inconsistent(self):
        artifact = _artifact()
        artifact["provenance"]["model_used"] = "other-model"
        issues = validate(artifact)
        self.assertTrue(
            any(i.path == "$.provenance.model_match" and i.code == "inconsistent" for i in issues)
        )

    def test_line_ranges(self):
        artifact = _artifact()
        artifact["findings"][0]["line"] = 0
        issues = validate(artifact)
        self.assertTrue(any(i.path == "$.findings[0].line" for i in issues))

        artifact = _artifact()
        artifact["findings"][0]["line"] = 10
        artifact["findings"][0]["end_line"] = 5
        issues = validate(artifact)
        self.assertTrue(
            any(i.path == "$.findings[0].end_line" and i.code == "range" for i in issues)
        )

    def test_duplicate_finding_ids(self):
        artifact = _artifact(findings=[_finding(id="F1"), _finding(id="F1")])
        issues = validate(artifact)
        self.assertTrue(
            any(i.path == "$.findings[1].id" and i.code == "duplicate_id" for i in issues)
        )

    def test_oversized_strings_and_lists(self):
        artifact = _artifact(summary="x" * 1001)
        artifact["findings"][0]["title"] = "t" * 121
        artifact["findings"][0]["body"] = "b" * 2001
        artifact["warnings"] = ["w"] * 51
        issues = validate(artifact)
        self.assertIn("too_large", _codes(issues))

    def test_suggested_fix_null_or_string(self):
        artifact = _artifact()
        artifact["findings"][0]["suggested_fix"] = "return x + 1"
        self.assertEqual(validate(artifact), [])
        artifact["findings"][0]["suggested_fix"] = 42
        self.assertIn("type", _codes(validate(artifact)))

    def test_usage_optional_object_validates(self):
        artifact = _artifact(
            usage={"prompt_tokens": 12, "candidates_tokens": 4, "total_tokens": 16}
        )
        self.assertEqual(validate(artifact), [])

    def test_usage_partial_counters_validates(self):
        self.assertEqual(validate(_artifact(usage={"total_tokens": 16})), [])
        self.assertEqual(validate(_artifact(usage={})), [])

    def test_usage_optional_thinking_counters_validate(self):
        artifact = _artifact(
            usage={
                "prompt_tokens": 12,
                "candidates_tokens": 4,
                "total_tokens": 16,
                "thoughts_tokens": 7,
                "cached_tokens": 3,
            }
        )
        self.assertEqual(validate(artifact), [])
        self.assertEqual(validate(_artifact(usage={"thoughts_tokens": 7})), [])
        self.assertEqual(validate(_artifact(usage={"cached_tokens": 3})), [])

    def test_usage_absent_still_validates(self):
        self.assertEqual(validate(_artifact()), [])

    def test_usage_wrong_types(self):
        for usage in (7, "tokens", [1], None):
            artifact = _artifact(usage=usage)
            issues = validate(artifact)
            self.assertTrue(
                any(issue.path == "$.usage" and issue.code == "type" for issue in issues),
                f"{usage!r} not flagged",
            )

    def test_usage_unknown_key_and_bad_counters(self):
        artifact = _artifact(
            usage={
                "prompt_tokens": -1,
                "candidates_tokens": "x",
                "total_tokens": True,
                "thoughts_tokens": "many",
                "cached_tokens": -2,
                "mystery_key": 3,
            }
        )
        issues = validate(artifact)
        by_path = {issue.path: issue.code for issue in issues}
        self.assertEqual(by_path["$.usage.prompt_tokens"], "range")
        self.assertEqual(by_path["$.usage.candidates_tokens"], "type")
        self.assertEqual(by_path["$.usage.total_tokens"], "type")
        self.assertEqual(by_path["$.usage.thoughts_tokens"], "type")
        self.assertEqual(by_path["$.usage.cached_tokens"], "range")
        self.assertEqual(by_path["$.usage.mystery_key"], "unknown_key")

    def test_issue_carries_no_offending_value(self):
        artifact = _artifact(summary="s3cr3t-value-that-is-too-long" * 100)
        issues = validate(artifact)
        for issue in issues:
            self.assertNotIn("s3cr3t", issue.path)
            self.assertNotIn("s3cr3t", issue.code)
        try:
            require_valid(artifact)
        except SchemaError as e:
            self.assertNotIn("s3cr3t", str(e))


class SerializeRoundTripTest(unittest.TestCase):
    def test_extracted_artifact_validates(self):
        artifact = _artifact()
        text = json.dumps(artifact)
        self.assertEqual(validate(extract_json(text)), [])

    def test_deepcopy_mutation_does_not_alias(self):
        artifact = copy.deepcopy(_artifact())
        artifact["findings"].append(_finding(id="F2", line=4, end_line=4))
        self.assertEqual(validate(artifact), [])


if __name__ == "__main__":
    unittest.main()

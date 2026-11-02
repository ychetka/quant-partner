#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from review.provenance import (  # noqa: E402
    ProvenanceError,
    ResolvedModel,
    build_provenance,
    check_model_used,
    confirm_discovery,
    resolve_alias,
)
from review.schema import validate  # noqa: E402

ALLOWED = {
    "claude": {"backend_model": "claude-sonnet-5.5", "match": "exact"},
    "gpt": {"backend_model": "gpt-5.3-codex", "match": "exact"},
}


class ResolveAliasTest(unittest.TestCase):
    def test_known_alias(self):
        resolved = resolve_alias("claude", ALLOWED)
        self.assertEqual(resolved.backend_model, "claude-sonnet-5.5")
        self.assertEqual(resolved.match, "exact")

    def test_unknown_alias_fails_closed(self):
        for alias in ("opus", "claude-sonnet-5.5", "", None, 7):
            with self.subTest(alias=alias), self.assertRaises(ProvenanceError) as ctx:
                resolve_alias(alias, ALLOWED)
            self.assertEqual(ctx.exception.code, "unknown_model")
            self.assertEqual(ctx.exception.status, "error")

    def test_free_form_provider_id_is_not_an_alias(self):
        with self.assertRaises(ProvenanceError) as ctx:
            resolve_alias("claude-opus-5.5", ALLOWED)
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_missing_or_bad_config_fails_closed(self):
        with self.assertRaises(ProvenanceError):
            resolve_alias("claude", None)
        with self.assertRaises(ProvenanceError) as ctx:
            resolve_alias("claude", {"claude": {"match": "exact"}})
        self.assertEqual(ctx.exception.code, "invalid_model_config")
        with self.assertRaises(ProvenanceError):
            resolve_alias("claude", {"claude": {"backend_model": "m", "match": "prefix"}})


class DiscoveryTest(unittest.TestCase):
    def test_confirmed_model(self):
        resolved = resolve_alias("claude", ALLOWED)
        confirm_discovery(resolved, ["gpt-5.3-codex", "claude-sonnet-5.5"])

    def test_unconfirmed_model_fails_closed(self):
        resolved = resolve_alias("claude", ALLOWED)
        for discovered in (None, [], ["claude-opus-5.5", "gpt-5.3-codex"]):
            with self.subTest(discovered=discovered), self.assertRaises(ProvenanceError) as ctx:
                confirm_discovery(resolved, discovered)
            self.assertEqual(ctx.exception.code, "model_unconfirmed")
            self.assertEqual(ctx.exception.status, "rejected")

    def test_configured_but_unavailable_model_is_rejected(self):
        allowed = {"opus": {"backend_model": "claude-opus-5.5", "match": "exact"}}
        resolved = resolve_alias("opus", allowed)
        with self.assertRaises(ProvenanceError) as ctx:
            confirm_discovery(resolved, ["claude-sonnet-5.5"])
        self.assertEqual(ctx.exception.code, "model_unconfirmed")


class CheckModelUsedTest(unittest.TestCase):
    def test_strict_match(self):
        self.assertTrue(
            check_model_used("claude-sonnet-5.5", "claude-sonnet-5.5", strict_model=True)
        )

    def test_strict_missing_model_used(self):
        for used in (None, "", 42):
            with self.subTest(used=used), self.assertRaises(ProvenanceError) as ctx:
                check_model_used("claude-sonnet-5.5", used, strict_model=True)
            self.assertEqual(ctx.exception.code, "model_unverifiable")

    def test_strict_mismatch_reports_both_ids(self):
        with self.assertRaises(ProvenanceError) as ctx:
            check_model_used("claude-sonnet-5.5", "claude-opus-5.5", strict_model=True)
        self.assertEqual(ctx.exception.code, "model_mismatch")
        self.assertIn("claude-sonnet-5.5", ctx.exception.message)
        self.assertIn("claude-opus-5.5", ctx.exception.message)

    def test_non_strict_mismatch_reports_false(self):
        self.assertFalse(check_model_used("claude-sonnet-5.5", "other", strict_model=False))
        self.assertFalse(check_model_used("claude-sonnet-5.5", None, strict_model=False))
        self.assertTrue(
            check_model_used("claude-sonnet-5.5", "claude-sonnet-5.5", strict_model=False)
        )


class BuildProvenanceTest(unittest.TestCase):
    def test_provenance_block_validates(self):
        provenance = build_provenance(
            partner_id="code-reviewer",
            backend="copilot-direct",
            model_alias="claude",
            model_requested="claude-sonnet-5.5",
            model_used="claude-sonnet-5.5",
            strict_model=True,
            discovery_confirmed=True,
            prompt_version="2026-11-02",
            partner_version="0.3.0",
            generated_at_utc="2026-11-02T12:00:00Z",
        )
        artifact = {
            "schema_version": "1.0",
            "status": "error",
            "advisory": True,
            "review_id": "rev-1",
            "idempotency_key": "a" * 64,
            "cached": False,
            "head_sha": "b" * 40,
            "base_sha": "c" * 40,
            "provenance": provenance,
            "verdict": "no_findings",
            "summary": "",
            "findings": [],
            "dropped_findings": [],
            "unreviewed": [],
            "warnings": [],
            "errors": [{"code": "x", "message": "y"}],
        }
        self.assertEqual(validate(artifact), [])
        self.assertTrue(provenance["model_match"])

    def test_model_match_derived_not_accepted(self):
        provenance = build_provenance(
            partner_id="code-reviewer",
            backend="copilot-direct",
            model_alias="claude",
            model_requested="claude-sonnet-5.5",
            model_used="different-model",
            strict_model=False,
            discovery_confirmed=True,
            prompt_version="2026-11-02",
            partner_version="0.3.0",
        )
        self.assertFalse(provenance["model_match"])
        self.assertEqual(provenance["model_used"], "different-model")

    def test_resolved_model_dataclass(self):
        resolved = ResolvedModel(alias="claude", backend_model="claude-sonnet-5.5", match="exact")
        self.assertEqual(resolved.alias, "claude")


if __name__ == "__main__":
    unittest.main()

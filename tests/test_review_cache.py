#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from review.cache import ArtifactCache, idempotency_key  # noqa: E402

_KEY_PARAMS = {
    "prompt_version": "2026-11-02",
    "partner_id": "code-reviewer",
    "backend": "copilot-direct",
    "model_alias": "claude",
    "model_requested": "claude-sonnet-5.5",
    "strict_model": True,
    "head_sha": "b" * 40,
    "base_sha": "c" * 40,
    "diff": "diff --git a/f.py b/f.py\n@@ -1,1 +1,1 @@\n-a\n+b\n",
    "context": "ctx",
    "file_contents": ["f1", "f2"],
}


def _artifact(status: str = "ok", **overrides: Any) -> dict[str, Any]:
    artifact = {
        "schema_version": "1.0",
        "status": status,
        "advisory": True,
        "review_id": "rev-1",
        "idempotency_key": "a" * 64,
        "cached": False,
        "head_sha": "b" * 40,
        "base_sha": "c" * 40,
        "provenance": {
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
        },
        "verdict": "no_findings",
        "summary": "",
        "findings": [],
        "dropped_findings": [],
        "unreviewed": [],
        "warnings": [],
        "errors": [] if status == "ok" else [{"code": "x", "message": "y"}],
    }
    artifact.update(overrides)
    return artifact


class IdempotencyKeyTest(unittest.TestCase):
    def test_key_is_canonical_and_filename_safe(self):
        key = idempotency_key(**_KEY_PARAMS)
        self.assertEqual(len(key), 64)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertNotIn("/", key)
        self.assertNotIn("..", key)

    def test_same_inputs_same_key(self):
        self.assertEqual(idempotency_key(**_KEY_PARAMS), idempotency_key(**_KEY_PARAMS))

    def test_different_inputs_different_key(self):
        for field, value in (
            ("prompt_version", "2026-11-03"),
            ("model_alias", "gpt"),
            ("strict_model", False),
            ("head_sha", "d" * 40),
            ("diff", "other diff"),
            ("context", "other"),
            ("file_contents", ["f1"]),
        ):
            params = {**_KEY_PARAMS, field: value}
            with self.subTest(field=field):
                self.assertNotEqual(idempotency_key(**params), idempotency_key(**_KEY_PARAMS))

    def test_sha_case_normalized(self):
        params = {**_KEY_PARAMS, "head_sha": "B" * 40}
        self.assertEqual(idempotency_key(**params), idempotency_key(**_KEY_PARAMS))

    def test_no_review_id_or_timestamp_in_signature(self):
        # The key takes no review_id/generated_at parameters at all.
        key = idempotency_key(**_KEY_PARAMS)
        time.sleep(0.001)
        self.assertEqual(idempotency_key(**_KEY_PARAMS), key)

    def test_context_file_contents_boundary_is_unambiguous(self):
        # Plain concatenation collides: "ab"+"c" == "a"+"bc". Canonical
        # JSON keeps the boundary, so the keys must differ.
        first = idempotency_key(**{**_KEY_PARAMS, "context": "ab", "file_contents": ["c"]})
        second = idempotency_key(**{**_KEY_PARAMS, "context": "a", "file_contents": ["bc"]})
        self.assertNotEqual(first, second)


class ArtifactCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cache = ArtifactCache(self.dir, ttl_seconds=3600, max_files=5)
        self.key = idempotency_key(**_KEY_PARAMS)

    def test_miss_on_empty_cache(self):
        self.assertIsNone(self.cache.get(self.key))

    def test_put_and_hit(self):
        self.assertTrue(self.cache.put(self.key, _artifact()))
        hit = self.cache.get(self.key)
        if hit is None:
            self.fail("expected cache hit")
        self.assertTrue(hit["cached"])
        self.assertEqual(hit["status"], "ok")
        stored = json.loads((self.dir / f"{self.key}.json").read_text(encoding="utf-8"))
        self.assertFalse(stored["cached"])

    def test_non_ok_refused(self):
        self.assertFalse(self.cache.put(self.key, _artifact(status="rejected")))
        self.assertFalse(self.cache.put(self.key, _artifact(status="error")))
        self.assertIsNone(self.cache.get(self.key))

    def test_invalid_artifact_refused(self):
        self.assertFalse(self.cache.put(self.key, {"status": "ok"}))
        self.assertFalse(self.cache.put(self.key, "not-a-dict"))

    def test_expired_entry_is_miss_and_removed(self):
        self.assertTrue(self.cache.put(self.key, _artifact()))
        path = self.dir / f"{self.key}.json"
        old = time.time() - 7200
        os.utime(path, (old, old))
        self.assertIsNone(self.cache.get(self.key))
        self.assertFalse(path.exists())

    def test_corrupt_file_is_miss_and_removed(self):
        path = self.dir / f"{self.key}.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.cache.get(self.key))
        self.assertFalse(path.exists())

    def test_invalid_cached_artifact_is_miss(self):
        path = self.dir / f"{self.key}.json"
        path.write_text(json.dumps({"status": "rejected"}), encoding="utf-8")
        self.assertIsNone(self.cache.get(self.key))
        self.assertFalse(path.exists())

    def test_atomic_write_leaves_no_temp_files(self):
        self.assertTrue(self.cache.put(self.key, _artifact()))
        leftovers = [p for p in self.dir.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])
        self.assertTrue((self.dir / f"{self.key}.json").exists())

    def test_prune_bounds_directory(self):
        cache = ArtifactCache(self.dir, ttl_seconds=3600, max_files=3)
        keys = []
        for index in range(6):
            params = {**_KEY_PARAMS, "diff": f"diff-{index}"}
            key = idempotency_key(**params)
            keys.append(key)
            path = self.dir / f"{key}.json"
            path.write_text(json.dumps(_artifact()), encoding="utf-8")
            # Ensure distinct, increasing mtimes.
            stamp = time.time() - (6 - index)
            os.utime(path, (stamp, stamp))
        removed = cache.prune()
        self.assertEqual(removed, 3)
        remaining = sorted(p.name for p in self.dir.glob("*.json"))
        self.assertEqual(len(remaining), 3)
        # Oldest entries are gone, newest survive.
        self.assertFalse((self.dir / f"{keys[0]}.json").exists())
        self.assertTrue((self.dir / f"{keys[5]}.json").exists())

    def test_put_prunes_oldest_beyond_max_files(self):
        cache = ArtifactCache(self.dir, ttl_seconds=3600, max_files=2)
        keys = []
        for index in range(3):
            params = {**_KEY_PARAMS, "diff": f"diff-{index}"}
            key = idempotency_key(**params)
            keys.append(key)
            self.assertTrue(cache.put(key, _artifact()))
            path = self.dir / f"{key}.json"
            stamp = time.time() - (3 - index)
            os.utime(path, (stamp, stamp))
        remaining = sorted(p.name for p in self.dir.glob("*.json"))
        self.assertLessEqual(len(remaining), 2)
        self.assertFalse((self.dir / f"{keys[0]}.json").exists())

    def test_prune_removes_expired(self):
        self.assertTrue(self.cache.put(self.key, _artifact()))
        path = self.dir / f"{self.key}.json"
        old = time.time() - 7200
        os.utime(path, (old, old))
        self.assertEqual(self.cache.prune(), 1)

    def test_prune_ignores_non_cache_files(self):
        # Only files whose stem is a 64-hex key are managed: leftover
        # ".tmp-*.json" files and unrelated JSON are left untouched.
        stray_tmp = self.dir / ".tmp-stuck.json"
        stray_tmp.write_text("{}", encoding="utf-8")
        unrelated = self.dir / "notes.json"
        unrelated.write_text("{}", encoding="utf-8")
        self.assertTrue(self.cache.put(self.key, _artifact()))
        valid = self.dir / f"{self.key}.json"
        old = time.time() - 7200
        os.utime(valid, (old, old))
        removed = self.cache.prune()
        self.assertEqual(removed, 1)
        self.assertFalse(valid.exists())
        self.assertTrue(stray_tmp.exists())
        self.assertTrue(unrelated.exists())

    def test_bad_key_rejected(self):
        with self.assertRaises(ValueError):
            self.cache.get("../escape")
        with self.assertRaises(ValueError):
            self.cache.put("x" * 64, _artifact())


if __name__ == "__main__":
    unittest.main()

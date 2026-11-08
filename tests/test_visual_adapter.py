#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import email.message
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load_tool(name):
    """Load a tools/ script by path, sharing identity via sys.modules.

    visual_adapter imports media_ingest as a sibling module: registering
    the loaded module in sys.modules first guarantees one shared module
    object, so error classes raised by va.* are mi.* classes too.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


mi = _load_tool("media_ingest")
va = _load_tool("visual_adapter")

TEST_KEY = "test-gemini-key-123"
FILE_URI = "https://generativelanguage.googleapis.com/v1beta/files/abc123"
UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/session-xyz"

GOOD_ENTRIES = [
    {"t0": 1.0, "t1": 4.0, "kind": "visual", "text": "Slide: roadmap overview"},
    {"t0": 10, "t1": 12.5, "kind": "object", "text": "Presenter enters frame"},
]


def _segments():
    return [{"t0": 1.0, "t1": 4.0, "speaker": None, "text": "Hello there."}]


def _transcript(kind="local_file", ref="meeting.mp4", segments=None):
    return mi.build_transcript(
        kind=kind,
        ref=ref,
        method="caller_supplied",
        segments=segments if segments is not None else _segments(),
        generated_at="2026-11-08T12:00:00Z",
    )


def _write_transcript(tmp, transcript, name="transcript.json"):
    path = Path(tmp) / name
    path.write_text(json.dumps(transcript, indent=2) + "\n", encoding="utf-8")
    return path


class _FakeResponse:
    """Minimal urlopen stand-in: context manager with headers + read()."""

    def __init__(self, payload=b"{}", headers=None):
        self._payload = payload
        self.headers = headers or {}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(status, message="boom"):
    body = json.dumps({"error": {"message": message}}).encode("utf-8")
    return urllib.error.HTTPError(
        "https://api.invalid", status, "err", email.message.Message(), io.BytesIO(body)
    )


def _json_response(payload):
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def _fake_urlopen_factory(
    *,
    model_text="[]",
    model_parts=None,
    file_states=("ACTIVE",),
    fail_at=None,
    fail_status=500,
    fail_message="internal error",
):
    """Build a urlopen stand-in covering the whole Files-API flow.

    ``fail_at`` names the stage to break: ``start``, ``put``, ``poll``,
    ``generate`` or ``delete``. ``model_parts`` overrides the raw
    candidates[].content.parts payload (for thought-part tests), while
    ``model_text`` wraps a single plain-text part. Recorded requests
    let tests assert the transport contract (methods, URLs, headers).
    """
    calls = []
    states = iter(file_states)

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        url = request.full_url
        method = request.get_method()
        if fail_at == "urlopen":
            raise urllib.error.URLError("connection refused")
        if method == "POST" and "/upload/" in url:
            if fail_at == "start":
                raise _http_error(fail_status, fail_message)
            return _FakeResponse(b"", headers={"X-Goog-Upload-URL": UPLOAD_URL})
        if method == "PUT":
            if fail_at == "put":
                raise _http_error(fail_status, fail_message)
            state = next(states, "ACTIVE")
            return _json_response(
                {"file": {"name": "files/abc123", "uri": FILE_URI, "state": state}}
            )
        if method == "GET":
            if fail_at == "poll":
                raise _http_error(fail_status, fail_message)
            state = next(states, "ACTIVE")
            return _json_response({"name": "files/abc123", "uri": FILE_URI, "state": state})
        if method == "POST" and ":generateContent" in url:
            if fail_at == "generate":
                raise _http_error(fail_status, fail_message)
            parts = model_parts if model_parts is not None else [{"text": model_text}]
            return _json_response({"candidates": [{"content": {"parts": parts}}]})
        if method == "DELETE":
            if fail_at == "delete":
                raise _http_error(fail_status, fail_message)
            return _FakeResponse(b"{}")
        raise AssertionError(f"unexpected request: {method} {url}")

    return fake_urlopen, calls


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
        code = va.main(argv)
    return code, out.getvalue(), err.getvalue()


class AnnotationSchemaTest(unittest.TestCase):
    def test_populated_annotations_accepted(self):
        transcript = _transcript()
        transcript["annotations"] = [
            {
                "kind": "visual",
                "t0": 1.0,
                "t1": 4.0,
                "text": "Slide shown",
                "source": "gemini-files-api",
                "model": "gemini-2.5-flash",
                "generated_at": "2026-11-08T12:30:00Z",
            },
            # Minimal shape: required keys only, int times.
            {"kind": "object", "t0": 5, "t1": 6, "text": "Person appears"},
        ]
        self.assertIsNone(mi.validate(transcript))

    def test_empty_annotations_still_valid(self):
        self.assertIsNone(mi.validate(_transcript()))

    def test_annotation_contract_violations(self):
        good = {"kind": "visual", "t0": 1.0, "t1": 2.0, "text": "x"}
        cases = [
            "not a dict",
            {"kind": "visual", "t0": 1.0, "text": "x"},  # missing t1
            {"kind": "visual", "t0": 1.0, "t1": 2.0},  # missing text
            {**good, "extra": 1},
            {**good, "t0": True},
            {**good, "t0": -1.0},
            {**good, "t0": float("nan")},
            {**good, "t0": 5.0, "t1": 2.0},  # t1 before t0
            {**good, "t0": None},
            {**good, "kind": ""},
            {**good, "kind": 7},
            {**good, "text": "   "},
            {**good, "source": 3},
            {**good, "model": None},
            {**good, "generated_at": ["z"]},
        ]
        for annotation in cases:
            with self.subTest(annotation=repr(annotation)[:80]):
                with self.assertRaises(mi.TranscriptSchemaError) as ctx:
                    mi.validate({**_transcript(), "annotations": [annotation]})
                # The failure names the offending index.
                self.assertIn("annotations[0]", ctx.exception.message)


class ObservationParseTest(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(va.parse_observations(json.dumps(GOOD_ENTRIES)), GOOD_ENTRIES)

    def test_code_fence_and_prose_wrapping(self):
        wrapped = "Here is what I saw:\n```json\n" + json.dumps(GOOD_ENTRIES) + "\n```\nDone."
        self.assertEqual(va.parse_observations(wrapped), GOOD_ENTRIES)
        prose = "observations: " + json.dumps(GOOD_ENTRIES) + " - end of report"
        self.assertEqual(va.parse_observations(prose), GOOD_ENTRIES)

    def test_empty_array_is_valid(self):
        self.assertEqual(va.parse_observations("[]"), [])
        self.assertEqual(va.parse_observations("```\n[]\n```"), [])

    def test_malformed_entries_dropped_good_kept(self):
        payload = json.dumps(
            [
                GOOD_ENTRIES[0],
                {"t0": 1.0, "t1": 2.0, "kind": "sound", "text": "bad kind"},
                {"t0": 9.0, "t1": 2.0, "kind": "visual", "text": "t1 before t0"},
                {"t0": "1", "t1": 2.0, "kind": "visual", "text": "string time"},
                {"t0": 1.0, "t1": 2.0, "kind": "visual"},  # no text
                {"t0": 1.0, "t1": 2.0, "kind": "object", "text": "  "},
                "not an object",
                GOOD_ENTRIES[1],
            ]
        )
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            observations = va.parse_observations(payload)
        self.assertEqual(observations, GOOD_ENTRIES)
        self.assertIn("dropped 6 malformed", err.getvalue())

    def test_garbage_only_output_is_named_error(self):
        for garbage in (
            "no JSON here at all",
            '{"annotations": "not an array"}',
            "```json\n{}\n```",
            "[unclosed",
            "",
        ):
            with self.subTest(garbage=garbage):
                with self.assertRaises(va.ModelOutputError) as ctx:
                    va.parse_observations(garbage)
                self.assertEqual(ctx.exception.code, "model_output_invalid")


class VisualCacheTest(unittest.TestCase):
    def test_key_format_and_inputs(self):
        key = va.visual_cache_key("sha256:" + "a" * 64, "gemini-2.5-flash")
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(key, va.visual_cache_key("sha256:" + "a" * 64, "gemini-2.5-flash"))
        self.assertNotEqual(key, va.visual_cache_key("sha256:" + "b" * 64, "gemini-2.5-flash"))
        self.assertNotEqual(key, va.visual_cache_key("sha256:" + "a" * 64, "other-model"))

    def test_round_trip_and_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = va.VisualCache(tmp)
            key = va.visual_cache_key("sha256:" + "a" * 64, "m")
            self.assertIsNone(cache.get(key))
            cache.put(key, GOOD_ENTRIES)
            self.assertEqual(cache.get(key), GOOD_ENTRIES)
            (path,) = list(Path(tmp).glob("*.json"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_and_tampered_entries_are_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = va.VisualCache(tmp)
            key = va.visual_cache_key("sha256:" + "a" * 64, "m")
            path = cache._path(key)
            path.write_text("{corrupt", encoding="utf-8")
            self.assertIsNone(cache.get(key))
            # A list with one malformed entry counts as tampering.
            path.write_text(json.dumps([*GOOD_ENTRIES, {"t0": "x"}]), encoding="utf-8")
            self.assertIsNone(cache.get(key))
            # Stored entries never carry file URIs or media paths.
            cache.put(key, GOOD_ENTRIES)
            stored = path.read_text(encoding="utf-8")
            self.assertNotIn("file_uri", stored)
            self.assertNotIn(FILE_URI, stored)


class MediaResolutionTest(unittest.TestCase):
    def test_media_flag_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "clip.mp4"
            media.write_bytes(b"fake")
            transcript = _transcript(kind="youtube", ref="https://youtu.be/x")
            resolved = va._resolve_media_path(transcript, str(media), Path(tmp) / "t.json")
            self.assertEqual(resolved, media)
            with self.assertRaises(va.MediaNotFound):
                va._resolve_media_path(transcript, str(Path(tmp) / "missing.mp4"), Path("t.json"))

    def test_local_file_ref_resolved_next_to_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake")
            transcript = _transcript(kind="local_file", ref="meeting.mp4")
            resolved = va._resolve_media_path(transcript, None, Path(tmp) / "transcript.json")
            self.assertEqual(resolved, media)

    def test_url_source_never_redownloaded(self):
        for kind in ("youtube", "web_page"):
            transcript = _transcript(kind=kind, ref="https://example.invalid/v")
            with self.subTest(kind=kind):
                with self.assertRaises(va.MediaNotFound) as ctx:
                    va._resolve_media_path(transcript, None, Path("t.json"))
                self.assertEqual(ctx.exception.code, "media_not_found")
                self.assertIn("--media", ctx.exception.hint)

    def test_unresolvable_local_ref(self):
        transcript = _transcript(kind="local_file", ref="gone.mp4")
        with self.assertRaises(va.MediaNotFound):
            va._resolve_media_path(transcript, None, Path("/nonexistent/t.json"))


class AdapterFlowTest(unittest.TestCase):
    def _workspace(self, tmp, kind="local_file", ref="meeting.mp4"):
        media = Path(tmp) / "meeting.mp4"
        media.write_bytes(b"fake media bytes")
        transcript_path = _write_transcript(tmp, _transcript(kind=kind, ref=ref))
        return media, transcript_path

    def test_end_to_end_writes_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            original = json.loads(transcript_path.read_text(encoding="utf-8"))
            fake, calls = _fake_urlopen_factory(model_text=json.dumps(GOOD_ENTRIES))
            env = {va.ENV_CREDENTIAL: TEST_KEY}
            with (
                patch.dict(os.environ, env),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 0, err)
            merged = json.loads(transcript_path.read_text(encoding="utf-8"))
            mi.validate(merged)
            self.assertEqual(len(merged["annotations"]), 2)
            first = merged["annotations"][0]
            self.assertEqual(first["kind"], "visual")
            self.assertEqual(first["t0"], 1.0)
            self.assertEqual(first["text"], "Slide: roadmap overview")
            self.assertEqual(first["source"], "gemini-files-api")
            self.assertEqual(first["model"], "gemini-2.5-flash")
            self.assertRegex(first["generated_at"], r"Z$")
            # Segments and content_hash are untouched by the merge.
            self.assertEqual(merged["segments"], original["segments"])
            self.assertEqual(merged["content_hash"], original["content_hash"])
            self.assertEqual(stat.S_IMODE(transcript_path.stat().st_mode), 0o600)
            # Full flow ran: upload start, byte upload, generate, delete.
            methods = [r.get_method() for r in calls]
            self.assertEqual(methods, ["POST", "PUT", "POST", "DELETE"])
            # The key is a header only - never in any URL.
            for request in calls:
                self.assertNotIn(TEST_KEY, request.full_url)
            self.assertEqual(calls[0].get_header("X-goog-api-key"), TEST_KEY)
            generate = calls[2]
            self.assertIn(":generateContent", generate.full_url)
            self.assertNotIn(TEST_KEY, generate.full_url)
            # No file URI is persisted anywhere: transcript and cache
            # hold only annotation entries.
            self.assertNotIn(FILE_URI, transcript_path.read_text(encoding="utf-8"))
            cache_file = next((Path(tmp) / "cache").glob("*.json"))
            self.assertNotIn(FILE_URI, cache_file.read_text(encoding="utf-8"))

    def test_cache_hit_skips_upload_and_needs_no_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            cache_dir = Path(tmp) / "cache"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            cache = va.VisualCache(cache_dir)
            key = va.visual_cache_key(transcript["content_hash"], "gemini-2.5-flash")
            cache.put(key, GOOD_ENTRIES)
            # No API key, urlopen must never fire: the cache serves all.
            env = {k: v for k, v in os.environ.items() if k != va.ENV_CREDENTIAL}
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    va.urllib.request,
                    "urlopen",
                    side_effect=AssertionError("network must not be touched"),
                ),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(cache_dir),
                    ]
                )
            self.assertEqual(code, 0, err)
            self.assertIn("cache hit", err)
            merged = json.loads(transcript_path.read_text(encoding="utf-8"))
            mi.validate(merged)
            self.assertEqual(len(merged["annotations"]), 2)

    def test_cache_hit_succeeds_when_media_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A URL-sourced transcript has no resolvable media at all:
            # a cache hit must still merge and write back successfully.
            transcript_path = _write_transcript(
                tmp, _transcript(kind="youtube", ref="https://youtu.be/xyz")
            )
            cache_dir = Path(tmp) / "cache"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            cache = va.VisualCache(cache_dir)
            key = va.visual_cache_key(transcript["content_hash"], "gemini-2.5-flash")
            cache.put(key, GOOD_ENTRIES)
            env = {k: v for k, v in os.environ.items() if k != va.ENV_CREDENTIAL}
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    va.urllib.request,
                    "urlopen",
                    side_effect=AssertionError("network must not be touched"),
                ),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(cache_dir),
                    ]
                )
            self.assertEqual(code, 0, err)
            self.assertIn("cache hit", err)
            merged = json.loads(transcript_path.read_text(encoding="utf-8"))
            mi.validate(merged)
            self.assertEqual(len(merged["annotations"]), 2)

    def test_force_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            cache_dir = Path(tmp) / "cache"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            va.VisualCache(cache_dir).put(
                va.visual_cache_key(transcript["content_hash"], "gemini-2.5-flash"),
                GOOD_ENTRIES,
            )
            fresh = [{"t0": 0.0, "t1": 1.0, "kind": "visual", "text": "fresh"}]
            fake, calls = _fake_urlopen_factory(model_text=json.dumps(fresh))
            with (
                patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(cache_dir),
                        "--force",
                    ]
                )
            self.assertEqual(code, 0, err)
            self.assertEqual(len(calls), 4)
            merged = json.loads(transcript_path.read_text(encoding="utf-8"))
            texts = {a["text"] for a in merged["annotations"]}
            self.assertEqual(texts, {"fresh"})

    def test_dedupe_keeps_existing_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            # Same (kind, t0, t1, model) tuple as the model will return.
            transcript["annotations"] = [
                {
                    "kind": "visual",
                    "t0": 1.0,
                    "t1": 4.0,
                    "text": "older wording kept",
                    "source": "gemini-files-api",
                    "model": "gemini-2.5-flash",
                    "generated_at": "2026-11-01T00:00:00Z",
                }
            ]
            transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
            fake, _ = _fake_urlopen_factory(model_text=json.dumps(GOOD_ENTRIES))
            with (
                patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 0, err)
            merged = json.loads(transcript_path.read_text(encoding="utf-8"))
            self.assertEqual(len(merged["annotations"]), 2)
            self.assertEqual(merged["annotations"][0]["text"], "older wording kept")
            self.assertEqual(merged["annotations"][1]["kind"], "object")

    def test_original_untouched_on_generate_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            before = transcript_path.read_bytes()
            fake, _ = _fake_urlopen_factory(fail_at="generate", fail_message="model blew up")
            with (
                patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, out, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("file_api_error", err)
            # Byte-identical original, no stray temp file left behind.
            self.assertEqual(transcript_path.read_bytes(), before)
            leftovers = [p for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_model_garbage_keeps_original_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            before = transcript_path.read_bytes()
            fake, _ = _fake_urlopen_factory(model_text="sorry, I cannot help")
            with (
                patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("model_output_invalid", err)
            self.assertEqual(transcript_path.read_bytes(), before)

    def test_missing_key_is_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            before = transcript_path.read_bytes()
            env = {k: v for k, v in os.environ.items() if k != va.ENV_CREDENTIAL}
            with patch.dict(os.environ, env, clear=True):
                code, out, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("auth_error", err)
            self.assertIn(va.ENV_CREDENTIAL, err)
            self.assertEqual(transcript_path.read_bytes(), before)

    def test_api_key_never_reaches_error_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            # A hostile/buggy upstream echoes the key inside the error body.
            fake, _ = _fake_urlopen_factory(
                fail_at="generate",
                fail_status=500,
                fail_message=f"upstream rejected key {TEST_KEY}",
            )
            with (
                patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertNotIn(TEST_KEY, err)
            self.assertIn("<redacted>", err)

    def test_quota_failure_is_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            for status in (429, 503):
                fake, _ = _fake_urlopen_factory(
                    fail_at="generate", fail_status=status, fail_message="quota exhausted"
                )
                with self.subTest(status=status):
                    with (
                        patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                        patch.object(va.urllib.request, "urlopen", side_effect=fake),
                    ):
                        code, _, err = _run_main(
                            [
                                "--transcript",
                                str(transcript_path),
                                "--cache-dir",
                                str(Path(tmp) / "cache"),
                                "--force",
                            ]
                        )
                    self.assertEqual(code, 1)
                    self.assertIn("quota_exceeded", err)

    def test_upload_failure_is_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript_path = self._workspace(tmp)
            for fail_at, code_name in (("start", "upload_failed"), ("put", "upload_failed")):
                fake, _ = _fake_urlopen_factory(fail_at=fail_at)
                with self.subTest(fail_at=fail_at):
                    with (
                        patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                        patch.object(va.urllib.request, "urlopen", side_effect=fake),
                    ):
                        code, _, err = _run_main(
                            [
                                "--transcript",
                                str(transcript_path),
                                "--cache-dir",
                                str(Path(tmp) / "cache"),
                            ]
                        )
                    self.assertEqual(code, 1)
                    self.assertIn(code_name, err)

    def test_media_not_found_for_url_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = _write_transcript(
                tmp, _transcript(kind="youtube", ref="https://youtu.be/xyz")
            )
            fake, calls = _fake_urlopen_factory()
            with (
                patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, out, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("media_not_found", err)
            self.assertIn("--media", err)
            self.assertEqual(calls, [])  # never re-downloads, never uploads

    def test_media_flag_used_for_url_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "copy.mp4"
            media.write_bytes(b"local copy")
            transcript_path = _write_transcript(
                tmp, _transcript(kind="youtube", ref="https://youtu.be/xyz")
            )
            fake, _ = _fake_urlopen_factory(model_text=json.dumps(GOOD_ENTRIES))
            with (
                patch.dict(os.environ, {va.ENV_CREDENTIAL: TEST_KEY}),
                patch.object(va.urllib.request, "urlopen", side_effect=fake),
            ):
                code, _, err = _run_main(
                    [
                        "--transcript",
                        str(transcript_path),
                        "--media",
                        str(media),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                    ]
                )
            self.assertEqual(code, 0, err)
            merged = json.loads(transcript_path.read_text(encoding="utf-8"))
            self.assertEqual(len(merged["annotations"]), 2)

    def test_poll_waits_for_active_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake")
            fake, calls = _fake_urlopen_factory(
                model_text="[]",
                file_states=("PROCESSING", "PROCESSING", "ACTIVE"),
            )
            key = TEST_KEY
            with patch.object(va.urllib.request, "urlopen", side_effect=fake):
                entries = va.analyze_media(
                    media,
                    model="gemini-2.5-flash",
                    api_key=key,
                    chunk_seconds=30,
                    poll_interval=0,
                )
            self.assertEqual(entries, [])
            methods = [r.get_method() for r in calls]
            # PUT returned PROCESSING, two GETs polled until ACTIVE.
            self.assertEqual(methods, ["POST", "PUT", "GET", "GET", "POST", "DELETE"])

    def test_delete_failure_is_best_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake")
            fake, _ = _fake_urlopen_factory(model_text=json.dumps(GOOD_ENTRIES), fail_at="delete")
            key = TEST_KEY
            with patch.object(va.urllib.request, "urlopen", side_effect=fake):
                entries = va.analyze_media(
                    media,
                    model="gemini-2.5-flash",
                    api_key=key,
                    chunk_seconds=30,
                    poll_interval=0,
                )
            # Cleanup failure never masks the extracted result.
            self.assertEqual(entries, GOOD_ENTRIES)

    def test_invalid_args_and_bad_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = _write_transcript(tmp, _transcript())
            cases = [
                (["--transcript", str(transcript_path), "--chunk-seconds", "0"], "--chunk-seconds"),
                (["--transcript", str(transcript_path), "--model", "  "], "--model"),
                (["--transcript", str(Path(tmp) / "missing.json")], "cannot read transcript"),
            ]
            for argv, needle in cases:
                with self.subTest(argv=argv):
                    code, out, err = _run_main(argv)
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("invalid_usage", err)
                    self.assertIn(needle, err)
            # Schema-invalid transcript is rejected before any media work.
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            code, _, err = _run_main(["--transcript", str(bad)])
            self.assertEqual(code, 1)
            self.assertIn("unsupported_schema_version", err)


class WriteBackTest(unittest.TestCase):
    def test_sequential_writes_leave_valid_json_and_no_tmp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = _write_transcript(tmp, _transcript())
            stamp = "2026-11-08T12:30:00Z"
            doc_a = _transcript()
            doc_b = dict(_transcript())
            doc_b["annotations"] = [
                {
                    "kind": "visual",
                    "t0": 1.0,
                    "t1": 4.0,
                    "text": "from second writer",
                    "source": "gemini-files-api",
                    "model": "gemini-2.5-flash",
                    "generated_at": stamp,
                }
            ]
            # mkstemp names are unique per call, so two writes (the
            # concurrent-run stand-in) cannot corrupt each other; the
            # second write is the deterministic last writer.
            va._write_back(transcript_path, doc_a)
            va._write_back(transcript_path, doc_b)
            merged = json.loads(transcript_path.read_text(encoding="utf-8"))
            mi.validate(merged)
            self.assertEqual(len(merged["annotations"]), 1)
            self.assertEqual(merged["annotations"][0]["text"], "from second writer")
            leftovers = [p for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])
            self.assertEqual(stat.S_IMODE(transcript_path.stat().st_mode), 0o600)


class UploadTransportTest(unittest.TestCase):
    def test_put_streams_file_object_and_scales_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "big.bin"
            # Sparse file: ~70 MiB apparent size, near-zero disk use.
            with media.open("wb") as handle:
                handle.seek(70 * 1024 * 1024)
                handle.write(b"x")
            seen = {}

            def fake_urlopen(request, timeout=None):
                url, method = request.full_url, request.get_method()
                if method == "POST" and "/upload/" in url:
                    return _FakeResponse(b"", headers={"X-Goog-Upload-URL": UPLOAD_URL})
                if method == "PUT":
                    seen["data"] = request.data
                    seen["timeout"] = timeout
                    seen["content_length"] = request.get_header("Content-length")
                    return _json_response(
                        {"file": {"name": "files/abc123", "uri": FILE_URI, "state": "ACTIVE"}}
                    )
                raise AssertionError(f"unexpected request: {method} {url}")

            with patch.object(va.urllib.request, "urlopen", side_effect=fake_urlopen):
                info = va._upload_file(media, "application/octet-stream", TEST_KEY, 120, [TEST_KEY])
            self.assertEqual(info["name"], "files/abc123")
            size = media.stat().st_size
            self.assertEqual(seen["content_length"], str(size))
            # The body is a file object http.client reads in blocks -
            # never a fully materialized bytes blob.
            self.assertTrue(hasattr(seen["data"], "read"))
            self.assertNotIsInstance(seen["data"], bytes)
            # ~70 MiB at a 512 KiB/s floor needs ~140s > the 120s floor.
            self.assertGreater(seen["timeout"], 120)
            self.assertEqual(seen["timeout"], max(120, -(-size // (512 * 1024))))

    def test_put_small_file_keeps_floor_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "small.bin"
            media.write_bytes(b"tiny")
            seen = {}

            def fake_urlopen(request, timeout=None):
                url, method = request.full_url, request.get_method()
                if method == "POST" and "/upload/" in url:
                    return _FakeResponse(b"", headers={"X-Goog-Upload-URL": UPLOAD_URL})
                if method == "PUT":
                    seen["timeout"] = timeout
                    seen["data"] = request.data
                    return _json_response(
                        {"file": {"name": "files/abc123", "uri": FILE_URI, "state": "ACTIVE"}}
                    )
                raise AssertionError(f"unexpected request: {method} {url}")

            with patch.object(va.urllib.request, "urlopen", side_effect=fake_urlopen):
                va._upload_file(media, "application/octet-stream", TEST_KEY, 120, [TEST_KEY])
            self.assertEqual(seen["timeout"], 120)


class RedirectKeyStripTest(unittest.TestCase):
    def test_cross_host_redirect_drops_api_key_header(self):
        handler = va._KeyStrippingRedirectHandler()
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/files/abc123",
            headers={"x-goog-api-key": TEST_KEY},
            method="GET",
        )
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://attacker.invalid/capture"
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("X-goog-api-key"))

    def test_same_host_redirect_keeps_api_key_header(self):
        handler = va._KeyStrippingRedirectHandler()
        request = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/files/abc123",
            headers={"x-goog-api-key": TEST_KEY},
            method="GET",
        )
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://generativelanguage.googleapis.com/v1beta/files/def456",
        )
        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.get_header("X-goog-api-key"), TEST_KEY)


class ThoughtPartsTest(unittest.TestCase):
    def test_thought_and_signature_parts_are_ignored(self):
        parts = [
            {"text": "Let me inspect the video frames.", "thought": True},
            {"text": "signature blob", "thoughtSignature": "abc123"},
            {"text": json.dumps(GOOD_ENTRIES)},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake")
            fake, _ = _fake_urlopen_factory(model_parts=parts)
            key = TEST_KEY
            with patch.object(va.urllib.request, "urlopen", side_effect=fake):
                entries = va.analyze_media(
                    media,
                    model="gemini-2.5-flash",
                    api_key=key,
                    chunk_seconds=30,
                    poll_interval=0,
                )
            self.assertEqual(entries, GOOD_ENTRIES)

    def test_thought_only_parts_fail_closed(self):
        parts = [
            {"text": "reasoning, not the answer", "thought": True},
            {"thoughtSignature": "abc123"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake")
            fake, _ = _fake_urlopen_factory(model_parts=parts)
            key = TEST_KEY
            with patch.object(va.urllib.request, "urlopen", side_effect=fake):
                with self.assertRaises(va.ModelOutputError):
                    va.analyze_media(
                        media,
                        model="gemini-2.5-flash",
                        api_key=key,
                        chunk_seconds=30,
                        poll_interval=0,
                    )


class DedupeKeyTest(unittest.TestCase):
    def test_model_less_annotations_dedupe_on_source(self):
        # A model-less annotation from another producer must not be
        # swallowed by a shared None key, and a model-less entry from
        # this adapter still dedupes on its source.
        transcript = _transcript()
        transcript["annotations"] = [
            {
                "kind": "visual",
                "t0": 1.0,
                "t1": 4.0,
                "text": "other producer",
                "source": "other-adapter",
            }
        ]
        doc = va._merge_annotations(transcript, [GOOD_ENTRIES[0]], "gemini-2.5-flash")
        self.assertEqual(len(doc["annotations"]), 2)
        # Same slot stamped by this adapter but without a model:
        # an empty-model run dedupes on SCHEMA_SOURCE.
        transcript["annotations"] = [
            {
                "kind": "visual",
                "t0": 1.0,
                "t1": 4.0,
                "text": "prior adapter run",
                "source": "gemini-files-api",
            }
        ]
        doc = va._merge_annotations(transcript, [GOOD_ENTRIES[0]], "")
        self.assertEqual(len(doc["annotations"]), 1)
        self.assertEqual(doc["annotations"][0]["text"], "prior adapter run")


if __name__ == "__main__":
    unittest.main()

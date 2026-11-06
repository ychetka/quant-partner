#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MEDIA_INGEST = ROOT / "tools" / "media_ingest.py"


def load_module():
    spec = importlib.util.spec_from_file_location("media_ingest", MEDIA_INGEST)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {MEDIA_INGEST}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mi = load_module()

SRT = """1
00:00:01,000 --> 00:00:04,000
Hello there.

2
00:00:04,500 --> 00:00:07,250
Second line
continues here.
"""

VTT = """WEBVTT

NOTE this block is a comment

00:00:01.000 --> 00:00:04.000
Hello there.

00:00:05.000 --> 00:00:08.000
Bye now.
"""

PLAIN = "First line.\n\nSecond line.\n"

LOOSE_JSON = json.dumps(
    [
        {"start": 1.0, "end": 4.0, "text": "Hello there."},
        {"text": "No timing on this one"},
    ]
)


def _segments():
    return [{"t0": 1.0, "t1": 4.0, "speaker": None, "text": "Hello there."}]


def _transcript(segments=None, **overrides):
    # ``segments`` participates in the built artifact (and its hash);
    # remaining overrides corrupt the object post-build so validate()
    # has something to reject.
    transcript = mi.build_transcript(
        kind="local_file",
        ref="sample.mp4",
        method="caller_supplied",
        segments=segments if segments is not None else _segments(),
        generated_at="2026-11-06T12:00:00Z",
    )
    transcript.update(overrides)
    return transcript


class SchemaValidationTest(unittest.TestCase):
    def test_valid_transcript_passes(self):
        self.assertIsNone(mi.validate(_transcript()))

    def test_built_transcript_has_full_contract_shape(self):
        transcript = _transcript()
        self.assertEqual(transcript["schema_version"], 1)
        self.assertEqual(transcript["generator_version"], "media_ingest/0.1.0")
        self.assertEqual(transcript["annotations"], [])
        self.assertRegex(transcript["content_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(transcript["source"]["transcription_method"], "caller_supplied")
        self.assertEqual(transcript["source"]["transcription_trust"], "self_transcribed")

    def test_incompatible_schema_version_rejected(self):
        for bad in (2, 0, -1, 99):
            with self.subTest(version=bad):
                with self.assertRaises(mi.UnsupportedSchemaVersion):
                    mi.validate(_transcript(schema_version=bad))

    def test_non_integer_schema_version_rejected(self):
        for bad in ("1", 1.0, True, None):
            with self.subTest(version=bad):
                with self.assertRaises(mi.TranscriptSchemaError):
                    mi.validate(_transcript(schema_version=bad))

    def test_schema_error_subclasses_ingest_error(self):
        self.assertTrue(issubclass(mi.UnsupportedSchemaVersion, mi.TranscriptSchemaError))
        self.assertTrue(issubclass(mi.TranscriptSchemaError, mi.IngestError))

    def test_top_level_contract_violations(self):
        cases = [
            "not a dict",
            {**_transcript(), "extra": 1},
            {k: v for k, v in _transcript().items() if k != "segments"},
            _transcript(content_hash="deadbeef"),
            _transcript(content_hash="sha256:" + "A" * 64),
            _transcript(annotations={}),
            _transcript(annotations=["tag"]),
            _transcript(generated_at="2026-11-06 12:00:00"),
            _transcript(generated_at="2026-11-06T12:00:00+00:00"),
            _transcript(generator_version="other/1.0.0"),
        ]
        for obj in cases:
            with self.subTest(obj=repr(obj)[:80]):
                with self.assertRaises(mi.TranscriptSchemaError):
                    mi.validate(obj)

    def test_source_contract_violations(self):
        base = _transcript()["source"]
        cases = [
            {**base, "kind": "dvd"},
            {**base, "ref": "  "},
            {**base, "transcription_method": "magic"},
            {**base, "transcription_trust": "vendor"},
            {**base, "transcription_method": "yt_dlp_captions"},
            {**base, "extra": 1},
            {k: v for k, v in base.items() if k != "ref"},
            "not a dict",
        ]
        for source in cases:
            with self.subTest(source=repr(source)[:80]):
                with self.assertRaises(mi.TranscriptSchemaError):
                    mi.validate(_transcript(source=source))

    def test_method_trust_consistency(self):
        # platform_captions trust is the only legal pairing for
        # yt_dlp_captions; every other pairing must be rejected.
        for method, trust, ok in (
            ("local_asr", "self_transcribed", True),
            ("local_asr", "platform_captions", False),
            ("caller_supplied", "self_transcribed", True),
            ("caller_supplied", "platform_captions", False),
            ("yt_dlp_captions", "platform_captions", True),
            ("yt_dlp_captions", "self_transcribed", False),
        ):
            source = {
                "kind": "local_file",
                "ref": "sample.mp4",
                "transcription_method": method,
                "transcription_trust": trust,
            }
            with self.subTest(method=method, trust=trust):
                if ok:
                    self.assertIsNone(mi.validate(_transcript(source=source)))
                else:
                    with self.assertRaises(mi.TranscriptSchemaError):
                        mi.validate(_transcript(source=source))

    def test_segment_contract_violations(self):
        cases = [
            "not a dict",
            {"t0": 1.0, "t1": 2.0},  # missing text
            {"t0": 1.0, "t1": 2.0, "text": "   "},
            {"t0": -1.0, "t1": 2.0, "text": "x"},
            {"t0": 5.0, "t1": 2.0, "text": "x"},  # t1 before t0
            {"t0": True, "t1": 2.0, "text": "x"},
            {"t0": 1.0, "t1": 2.0, "text": "x", "speaker": 7},
            {"t0": 1.0, "t1": 2.0, "text": "x", "extra": 1},
        ]
        base = _transcript()
        for segment in cases:
            with self.subTest(segment=repr(segment)[:80]):
                with self.assertRaises(mi.TranscriptSchemaError):
                    mi.validate({**base, "segments": [segment]})

    def test_null_times_and_missing_speaker_are_valid(self):
        segments = [{"t0": None, "t1": None, "text": "untimed line"}]
        self.assertIsNone(mi.validate(_transcript(segments=segments)))


class NormalizerTest(unittest.TestCase):
    def test_srt(self):
        segments = mi.normalize_transcript(SRT)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["t0"], 1.0)
        self.assertEqual(segments[0]["t1"], 4.0)
        self.assertEqual(segments[0]["text"], "Hello there.")
        self.assertIsNone(segments[0]["speaker"])
        self.assertEqual(segments[1]["t0"], 4.5)
        self.assertEqual(segments[1]["t1"], 7.25)
        # Multi-line cue bodies collapse into one normalized text.
        self.assertEqual(segments[1]["text"], "Second line continues here.")

    def test_vtt(self):
        segments = mi.normalize_transcript(VTT)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["t0"], 1.0)
        self.assertEqual(segments[0]["text"], "Hello there.")
        self.assertEqual(segments[1]["t1"], 8.0)
        self.assertEqual(segments[1]["text"], "Bye now.")

    def test_plain_text(self):
        segments = mi.normalize_transcript(PLAIN)
        self.assertEqual(len(segments), 2)
        for segment in segments:
            self.assertIsNone(segment["t0"])
            self.assertIsNone(segment["t1"])
            self.assertIsNone(segment["speaker"])
        self.assertEqual(segments[0]["text"], "First line.")
        self.assertEqual(segments[1]["text"], "Second line.")

    def test_loose_json(self):
        segments = mi.normalize_transcript(LOOSE_JSON)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["t0"], 1.0)
        self.assertEqual(segments[0]["t1"], 4.0)
        self.assertEqual(segments[0]["text"], "Hello there.")
        self.assertIsNone(segments[1]["t0"])
        self.assertIsNone(segments[1]["t1"])

    def test_loose_json_string_times_and_speaker(self):
        raw = json.dumps(
            [{"start": "1.5", "end": "00:00:04,000", "text": " hi ", "speaker": "ann"}]
        )
        (segment,) = mi.normalize_transcript(raw)
        self.assertEqual(segment["t0"], 1.5)
        self.assertEqual(segment["t1"], 4.0)
        self.assertEqual(segment["text"], "hi")
        self.assertEqual(segment["speaker"], "ann")

    def test_empty_input_rejected(self):
        for bad in ("", "   \n  ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(mi.TranscriptFormatError):
                    mi.normalize_transcript(bad)

    def test_malformed_inputs_rejected(self):
        for bad in (
            "[[1, 2]]",  # non-object items
            '[{"start": 1}]',  # no text
            '[{"text": "   "}]',  # empty text
            "WEBVTT\n\nonly a header, no cues",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(mi.TranscriptFormatError):
                    mi.normalize_transcript(bad)


class ContentHashTest(unittest.TestCase):
    def test_format(self):
        digest = mi.content_hash(_segments())
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_deterministic_across_runs(self):
        self.assertEqual(mi.content_hash(_segments()), mi.content_hash(_segments()))

    def test_whitespace_and_key_order_do_not_change_hash(self):
        first = mi.content_hash([{"t0": 1.0, "t1": 4.0, "speaker": None, "text": "Hello there."}])
        # Different key insertion order, float style and inner spacing.
        second = mi.content_hash(
            [{"text": "Hello   there.", "speaker": None, "t1": 4, "t0": 1.000}]
        )
        self.assertEqual(first, second)
        # Same invariant end-to-end through the normalizer: SRT and a
        # loose-JSON twin must land on one hash.
        via_srt = mi.content_hash(mi.normalize_transcript(SRT.split("\n\n")[0] + "\n"))
        via_json = mi.content_hash(
            mi.normalize_transcript('[{"start": 1, "end": 4, "text": "Hello there."}]')
        )
        self.assertEqual(via_srt, via_json)

    def test_content_change_changes_hash(self):
        other = [{"t0": 1.0, "t1": 4.0, "speaker": None, "text": "Different."}]
        self.assertNotEqual(mi.content_hash(_segments()), mi.content_hash(other))

    def test_speaker_participates(self):
        spoken = [{"t0": 1.0, "t1": 4.0, "speaker": "ann", "text": "Hello there."}]
        self.assertNotEqual(mi.content_hash(_segments()), mi.content_hash(spoken))


class TranscriptCacheTest(unittest.TestCase):
    def _cache(self, tmp, **kwargs):
        return mi.TranscriptCache(Path(tmp) / "cache", **kwargs)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            transcript = _transcript()
            self.assertEqual(cache.put(transcript), transcript["content_hash"])
            self.assertEqual(cache.get(transcript["content_hash"]), transcript)

    def test_miss_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            self.assertIsNone(cache.get("sha256:" + "0" * 64))

    def test_key_must_be_content_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            for bad in ("deadbeef", "sha256:xyz", "sample.mp4", "../etc/passwd", ""):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        cache.get(bad)

    def test_files_are_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            transcript = _transcript()
            cache.put(transcript)
            (path,) = list((Path(tmp) / "cache").glob("*.json"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_ttl_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp, ttl_seconds=3600)
            transcript = _transcript()
            cache.put(transcript)
            future = time.time() + 3601
            self.assertIsNone(cache.get(transcript["content_hash"], now=future))
            # The expired file was removed on the miss.
            self.assertEqual(list((Path(tmp) / "cache").glob("*.json")), [])

    def test_max_files_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp, max_files=3)
            for index in range(5):
                transcript = _transcript(
                    segments=[{"t0": 0.0, "t1": 1.0, "speaker": None, "text": f"s{index}"}],
                )
                cache.put(transcript)
                time.sleep(0.01)
            self.assertEqual(len(list((Path(tmp) / "cache").glob("*.json"))), 3)

    def test_invalid_transcript_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            with self.assertRaises(mi.TranscriptSchemaError):
                cache.put({"schema_version": 2})
            self.assertEqual(list((Path(tmp) / "cache").glob("*.json")), [])

    def test_corrupt_and_mismatched_entries_are_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            cache_dir = Path(tmp) / "cache"
            cache_dir.mkdir()
            digest = "1" * 64
            (cache_dir / f"{digest}.json").write_text("{corrupt", encoding="utf-8")
            self.assertIsNone(cache.get(f"sha256:{digest}"))
            # A schema-valid transcript under the wrong hash is dropped too.
            other = _transcript()
            (cache_dir / f"{digest}.json").write_text(json.dumps(other), encoding="utf-8")
            self.assertIsNone(cache.get(f"sha256:{digest}"))
            # Stray files never disturb prune.
            (cache_dir / "keep.me").write_text("x", encoding="utf-8")
            cache.prune()
            self.assertTrue((cache_dir / "keep.me").exists())


class SubprocessContractTest(unittest.TestCase):
    def test_missing_binary_raises_named_error(self):
        with patch.object(mi.shutil, "which", return_value=None):
            with self.assertRaises(mi.AsrUnavailable) as ctx:
                mi.transcribe_local(
                    Path("media.wav"),
                    binary="definitely-missing-whisper",
                    model_path="model.bin",
                    timeout_seconds=5,
                    work_dir=Path("/tmp"),
                )
        self.assertIn("definitely-missing-whisper", str(ctx.exception))
        self.assertEqual(ctx.exception.code, "asr_unavailable")
        self.assertEqual(ctx.exception.binary, "definitely-missing-whisper")

    def test_filenotfound_maps_to_asr_unavailable(self):
        with (
            patch.object(mi.shutil, "which", return_value="/usr/bin/whisper-cli"),
            patch.object(mi.subprocess, "run", side_effect=FileNotFoundError),
        ):
            with self.assertRaises(mi.AsrUnavailable):
                mi.transcribe_local(
                    Path("media.wav"),
                    binary="whisper-cli",
                    model_path="model.bin",
                    timeout_seconds=5,
                    work_dir=Path("/tmp"),
                )

    def test_timeout_is_named_error(self):
        with patch.object(mi.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 5)):
            with self.assertRaises(mi.ExternalToolError) as ctx:
                mi.run_external(["some-bin"], timeout_seconds=5)
        self.assertIn("timed out", str(ctx.exception))

    def test_nonzero_exit_is_named_error(self):
        proc = subprocess.CompletedProcess([], 3, "", "boom")
        with patch.object(mi.subprocess, "run", return_value=proc):
            with self.assertRaises(mi.ExternalToolError) as ctx:
                mi.run_external(["some-bin"], timeout_seconds=5)
        self.assertIn("code 3", str(ctx.exception))
        self.assertIn("boom", ctx.exception.hint)

    def test_run_external_uses_argv_list_and_timeout(self):
        proc = subprocess.CompletedProcess(["some-bin"], 0, "ok", "")
        with patch.object(mi.subprocess, "run", return_value=proc) as run:
            mi.run_external(["some-bin", "-v"], timeout_seconds=7)
        _, kwargs = run.call_args
        self.assertEqual(run.call_args[0][0], ["some-bin", "-v"])
        self.assertEqual(kwargs["timeout"], 7)
        self.assertFalse(kwargs.get("shell", False))


class FromFileCliTest(unittest.TestCase):
    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = mi.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_from_file_caller_supplied_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            transcript_file = Path(tmp) / "meeting.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            out_path = Path(tmp) / "transcript.json"
            cache_dir = Path(tmp) / "cache"
            code, _, err = self._run_main(
                [
                    "from-file",
                    str(media),
                    "--transcript-file",
                    str(transcript_file),
                    "--cache-dir",
                    str(cache_dir),
                    "-o",
                    str(out_path),
                ]
            )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            mi.validate(transcript)
            self.assertEqual(transcript["source"]["kind"], "local_file")
            self.assertEqual(transcript["source"]["ref"], str(media.resolve()))
            self.assertEqual(transcript["source"]["transcription_method"], "caller_supplied")
            self.assertEqual(transcript["source"]["transcription_trust"], "self_transcribed")
            self.assertEqual(len(transcript["segments"]), 2)
            self.assertEqual(stat.S_IMODE(out_path.stat().st_mode), 0o600)
            # The transcript was stored under its content_hash.
            cached = cache_dir / f"{transcript['content_hash'].split(':')[1]}.json"
            self.assertTrue(cached.is_file())

    def test_from_file_local_asr_end_to_end(self):
        def fake_run(argv, **kwargs):
            out_base = Path(argv[argv.index("-of") + 1])
            payload = {
                "transcription": [
                    {
                        "offsets": {"from": 0, "to": 1500},
                        "timestamps": {"from": "00:00:00,000", "to": "00:00:01,500"},
                        "text": " hello world ",
                    }
                ]
            }
            out_base.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.wav"
            media.write_bytes(b"fake audio")
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            out_path = Path(tmp) / "transcript.json"
            with (
                patch.object(mi.shutil, "which", return_value="/usr/bin/whisper-cli"),
                patch.object(mi.subprocess, "run", side_effect=fake_run),
            ):
                code, _, err = self._run_main(
                    [
                        "from-file",
                        str(media),
                        "--whisper-model",
                        str(model),
                        "--cache-dir",
                        str(Path(tmp) / "cache"),
                        "-o",
                        str(out_path),
                    ]
                )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            mi.validate(transcript)
            self.assertEqual(transcript["source"]["transcription_method"], "local_asr")
            self.assertEqual(transcript["source"]["transcription_trust"], "self_transcribed")
            self.assertEqual(
                transcript["segments"],
                [{"t0": 0.0, "t1": 1.5, "speaker": None, "text": "hello world"}],
            )

    def test_from_file_missing_binary_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.wav"
            media.write_bytes(b"fake audio")
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            with patch.object(mi.shutil, "which", return_value=None):
                code, out, err = self._run_main(
                    [
                        "from-file",
                        str(media),
                        "--whisper-model",
                        str(model),
                        "--whisper-binary",
                        "missing-whisper",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("asr_unavailable", err)
            self.assertIn("missing-whisper", err)

    def test_from_file_missing_media_fails(self):
        code, _, err = self._run_main(["from-file", "/nonexistent/file.mp4"])
        self.assertEqual(code, 1)
        self.assertIn("invalid_usage", err)

    def test_reserved_subcommands_fail_named(self):
        for command, arg in (
            ("from-youtube", "https://example.invalid/v"),
            ("from-web-page", "https://example.invalid/page"),
        ):
            with self.subTest(command=command):
                code, _, err = self._run_main([command, arg])
                self.assertEqual(code, 1)
                self.assertIn("invalid_usage", err)
                self.assertIn("not implemented", err)

    def test_validate_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps(_transcript()), encoding="utf-8")
            code, out, _ = self._run_main(["validate", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("ok sha256:", out)

    def test_validate_subcommand_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            code, _, err = self._run_main(["validate", str(path)])
            self.assertEqual(code, 1)
            self.assertIn("unsupported_schema_version", err)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import importlib.util
import io
import json
import os
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

# A realistic Latin-1/Windows-1252 SRT: 0xe9 is 'é' in those encodings
# and an invalid UTF-8 byte.
LATIN1_SRT = b"1\n00:00:01,000 --> 00:00:04,000\nCaf\xe9 break.\n"
LATIN1_VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nCaf\xe9 break.\n"


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
        self.assertEqual(transcript["generator_version"], mi.GENERATOR_VERSION)
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

    def test_cache_hit_is_optional_and_true_only(self):
        self.assertIsNone(mi.validate(_transcript()))
        self.assertIsNone(mi.validate(_transcript(cache_hit=True)))
        for bad in (False, "yes", 1, 0):
            with self.subTest(cache_hit=bad):
                with self.assertRaises(mi.TranscriptSchemaError):
                    mi.validate(_transcript(cache_hit=bad))


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

    def test_vtt_inline_tags_stripped(self):
        # YouTube-style auto-captions: <c> wrappers, an inline timestamp
        # tag mid-cue, voice tags and cue settings on the --> line.
        raw = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000 align:start position:0%\n"
            "<c>Hello</c> <00:00:01.500>there.\n\n"
            "00:00:02.000 --> 00:00:03.000\n"
            "<v Speaker>Second line.</v>\n"
        )
        segments = mi.normalize_transcript(raw)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["text"], "Hello there.")
        self.assertEqual(segments[1]["text"], "Second line.")

    def test_srt_inline_tags_stripped(self):
        raw = "1\n00:00:01,000 --> 00:00:02,000\n<i>Hello</i> <font color='#fff'>there</font>.\n"
        (segment,) = mi.normalize_transcript(raw)
        self.assertEqual(segment["text"], "Hello there.")

    def test_non_tag_angle_brackets_kept(self):
        raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\na < b and 1 > 0\n"
        (segment,) = mi.normalize_transcript(raw)
        self.assertEqual(segment["text"], "a < b and 1 > 0")

    def test_exact_duplicate_cues_dropped_after_normalization(self):
        # Rolling auto-caption exports repeat identical cues; the first
        # occurrence is kept and exact repeats are dropped, in order.
        # The duplicate below also proves dedup runs after tag removal.
        raw = VTT + "\n00:00:01.000 --> 00:00:04.000\n<c>Hello</c> there.\n"
        segments = mi.normalize_transcript(raw)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["text"], "Hello there.")
        self.assertEqual(segments[1]["text"], "Bye now.")

    def test_non_exact_duplicate_cues_are_kept(self):
        # Same text at a different time, and overlapping rolling
        # fragments, are real content - only exact duplicates drop.
        raw = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\nHello\n\n"
            "00:00:02.000 --> 00:00:03.000\nHello\n\n"
            "00:00:02.500 --> 00:00:04.000\nHello world\n"
        )
        segments = mi.normalize_transcript(raw)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[2]["text"], "Hello world")


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

    def test_segment_tamper_recomputes_hash_and_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            digest = cache.put(_transcript())
            path = Path(tmp) / "cache" / f"{digest.split(':', 1)[1]}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            # Declared content_hash is left intact but the segments are
            # forged, so the recomputed digest no longer matches the key.
            payload["segments"][0]["text"] = "forged replacement"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(cache.get(digest))
            self.assertFalse(path.exists())

    def test_store_or_reuse_hit_rewrites_ref_and_flags_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            first = _transcript()  # ref "sample.mp4", generated_at fixed above
            emitted_fresh = mi._store_or_reuse(cache, first)
            self.assertNotIn("cache_hit", emitted_fresh)
            # Same segments, different input: content_hash hits the cache.
            second = mi.build_transcript(
                kind="local_file",
                ref="other-name.mp4",
                method="caller_supplied",
                segments=_segments(),
                generated_at="2026-11-07T09:30:00Z",
            )
            emitted = mi._store_or_reuse(cache, second)
            mi.validate(emitted)
            self.assertIs(emitted["cache_hit"], True)
            self.assertEqual(emitted["source"]["ref"], "other-name.mp4")
            # The cached generated_at is the transcript creation time.
            self.assertEqual(emitted["generated_at"], "2026-11-06T12:00:00Z")
            self.assertEqual(emitted["content_hash"], second["content_hash"])
            # The stored entry keeps its original provenance and no flag.
            stored = cache.get(first["content_hash"])
            self.assertNotIn("cache_hit", stored)
            self.assertEqual(stored["source"]["ref"], "sample.mp4")


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


class EmitOutputTest(unittest.TestCase):
    def test_new_file_is_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "transcript.json"
            mi._emit(_transcript(), str(out_path))
            self.assertEqual(stat.S_IMODE(out_path.stat().st_mode), 0o600)

    def test_overwrite_existing_0644_file_forces_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "transcript.json"
            out_path.write_text("{}", encoding="utf-8")
            os.chmod(out_path, 0o644)
            mi._emit(_transcript(), str(out_path))
            # O_TRUNC alone would keep the old 0644 mode.
            self.assertEqual(stat.S_IMODE(out_path.stat().st_mode), 0o600)
            # The artifact was still written in full.
            mi.validate(json.loads(out_path.read_text(encoding="utf-8")))

    def test_stdout_emit_when_no_output(self):
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            mi._emit(_transcript(), None)
        mi.validate(json.loads(out.getvalue()))


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
            # The artifact may reach a remote partner: only the basename
            # is stored, never the local absolute path.
            self.assertEqual(transcript["source"]["ref"], media.name)
            self.assertFalse(Path(transcript["source"]["ref"]).is_absolute())
            self.assertNotIn(str(tmp), transcript["source"]["ref"])
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

    def test_from_file_ref_label_overrides_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            transcript_file = Path(tmp) / "meeting.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            out_path = Path(tmp) / "transcript.json"
            code, _, err = self._run_main(
                [
                    "from-file",
                    str(media),
                    "--transcript-file",
                    str(transcript_file),
                    "--ref",
                    "standup-notes",
                    "--no-cache",
                    "-o",
                    str(out_path),
                ]
            )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(transcript["source"]["ref"], "standup-notes")

    def test_from_file_blank_ref_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            code, out, err = self._run_main(["from-file", str(media), "--ref", "   "])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("invalid_usage", err)
            self.assertIn("--ref", err)

    def test_from_file_cache_hit_rewrites_ref_and_flags_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_file = Path(tmp) / "meeting.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            cache_dir = Path(tmp) / "cache"
            outputs = []
            for name in ("first.mp4", "second.mp4"):
                media = Path(tmp) / name
                media.write_bytes(b"fake media")
                out_path = Path(tmp) / f"{name}.transcript.json"
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
                outputs.append(json.loads(out_path.read_text(encoding="utf-8")))
            first, second = outputs
            mi.validate(first)
            mi.validate(second)
            self.assertNotIn("cache_hit", first)
            self.assertEqual(first["source"]["ref"], "first.mp4")
            # Same content, different file: one content_hash, one cache hit.
            self.assertEqual(second["content_hash"], first["content_hash"])
            self.assertIs(second["cache_hit"], True)
            # Provenance follows the current input, not the first ingest.
            self.assertEqual(second["source"]["ref"], "second.mp4")
            self.assertEqual(second["generated_at"], first["generated_at"])

    def test_from_file_missing_output_dir_is_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            transcript_file = Path(tmp) / "meeting.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            code, out, err = self._run_main(
                [
                    "from-file",
                    str(media),
                    "--transcript-file",
                    str(transcript_file),
                    "--no-cache",
                    "-o",
                    str(Path(tmp) / "missing-dir" / "transcript.json"),
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("output_write_failed", err)
            self.assertIn("missing-dir", err)

    def test_from_file_numeric_args_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            for extra, needle in (
                (["--timeout-seconds", "0"], "--timeout-seconds"),
                (["--timeout-seconds", "-5"], "--timeout-seconds"),
                (["--cache-ttl-seconds", "-1"], "--cache-ttl-seconds"),
                (["--cache-max-files", "0"], "--cache-max-files"),
            ):
                with self.subTest(extra=extra):
                    code, out, err = self._run_main(["from-file", str(media), *extra, "--no-cache"])
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("invalid_usage", err)
                    self.assertIn(needle, err)

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

    def test_validate_subcommand_non_utf8_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_bytes(LATIN1_SRT)
            code, out, err = self._run_main(["validate", str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("transcript_format_invalid", err)
            self.assertIn("UTF-8", err)

    def test_non_utf8_transcript_file_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            transcript_file = Path(tmp) / "meeting.srt"
            transcript_file.write_bytes(LATIN1_SRT)
            code, out, err = self._run_main(
                [
                    "from-file",
                    str(media),
                    "--transcript-file",
                    str(transcript_file),
                    "--no-cache",
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("transcript_format_invalid", err)
            self.assertIn("UTF-8", err)

    def test_transcript_file_rejects_whisper_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            transcript_file = Path(tmp) / "meeting.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            for extra in (
                ["--whisper-binary", "custom-whisper"],
                ["--whisper-model", "model.bin"],
            ):
                with self.subTest(extra=extra):
                    code, out, err = self._run_main(
                        [
                            "from-file",
                            str(media),
                            "--transcript-file",
                            str(transcript_file),
                            *extra,
                            "--no-cache",
                        ]
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("invalid_usage", err)
                    self.assertIn(extra[0], err)
                    self.assertIn("--transcript-file", err)

    def test_env_binary_vars_are_not_treated_as_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "meeting.mp4"
            media.write_bytes(b"fake media")
            transcript_file = Path(tmp) / "meeting.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            out_path = Path(tmp) / "t.json"
            env = {
                mi.ENV_WHISPER_BINARY: "env-whisper",
                mi.ENV_WHISPER_MODEL: "env-model",
                mi.ENV_YTDLP_BINARY: "env-ytdlp",
            }
            # Environment defaults are not explicit flags: they must not
            # trip the mode-applicability rejection.
            with patch.dict(os.environ, env):
                code, _, err = self._run_main(
                    [
                        "from-file",
                        str(media),
                        "--transcript-file",
                        str(transcript_file),
                        "--no-cache",
                        "-o",
                        str(out_path),
                    ]
                )
            self.assertEqual(code, 0, err)
            mi.validate(json.loads(out_path.read_text(encoding="utf-8")))


def _fake_whisper_payload():
    return {
        "transcription": [
            {
                "offsets": {"from": 0, "to": 1500},
                "timestamps": {"from": "00:00:00,000", "to": "00:00:01,500"},
                "text": " hello world ",
            }
        ]
    }


def _fake_run_factory(*, captions=None, media=b"fake wav", exit_code=0, stderr=""):
    """Build a subprocess.run stand-in covering yt-dlp and whisper-cli.

    ``argv`` inspection keeps the fake honest: a whisper call carries
    ``-of`` (it writes ``<of>.json``), a captions call carries
    ``--skip-download`` (it writes ``<base>.en.vtt``) and a media
    download carries ``--audio-format`` (it writes ``<base>.wav``).
    """

    def fake_run(argv, **kwargs):
        assert kwargs.get("shell") is not True
        if "-of" in argv:  # whisper.cpp: whisper-cli ... -of <base>
            out_base = Path(argv[argv.index("-of") + 1])
            out_base.with_suffix(".json").write_text(
                json.dumps(_fake_whisper_payload()), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        base = Path(argv[argv.index("-o") + 1])
        if exit_code != 0:
            return subprocess.CompletedProcess(argv, exit_code, "", stderr)
        if "--skip-download" in argv:
            if captions is not None:
                Path(str(base) + ".en.vtt").write_text(captions, encoding="utf-8")
        else:
            if media is not None:
                Path(str(base) + ".wav").write_bytes(media)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return fake_run


def _which_all(name):
    return f"/usr/bin/{name}"


def _ingest_workspace_dirs():
    return set(Path(tempfile.gettempdir()).glob("media-ingest-*"))


class FromYoutubeCliTest(unittest.TestCase):
    URL = "https://www.youtube.com/watch?v=demo123"

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = mi.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_captions_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "transcript.json"
            cache_dir = Path(tmp) / "cache"
            before = _ingest_workspace_dirs()
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(
                    mi.subprocess, "run", side_effect=_fake_run_factory(captions=VTT)
                ) as run,
            ):
                code, _, err = self._run_main(
                    [
                        "from-youtube",
                        self.URL,
                        "--cache-dir",
                        str(cache_dir),
                        "-o",
                        str(out_path),
                    ]
                )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            mi.validate(transcript)
            self.assertEqual(transcript["source"]["kind"], "youtube")
            self.assertEqual(transcript["source"]["ref"], self.URL)
            self.assertEqual(transcript["source"]["transcription_method"], "yt_dlp_captions")
            self.assertEqual(transcript["source"]["transcription_trust"], "platform_captions")
            self.assertEqual(len(transcript["segments"]), 2)
            self.assertEqual(transcript["segments"][0]["text"], "Hello there.")
            self.assertEqual(stat.S_IMODE(out_path.stat().st_mode), 0o600)
            cached = cache_dir / f"{transcript['content_hash'].split(':')[1]}.json"
            self.assertTrue(cached.is_file())
            # The captions argv contract: argv list, skip-download, no shell.
            argv = run.call_args[0][0]
            self.assertEqual(argv[0], "yt-dlp")
            self.assertIn("--skip-download", argv)
            self.assertIn("--write-auto-sub", argv)
            self.assertEqual(argv[-1], self.URL)
            self.assertFalse(run.call_args[1].get("shell", False))
            # The TemporaryDirectory workspace is gone after the run.
            self.assertEqual(_ingest_workspace_dirs(), before)

    def test_captions_sub_langs_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(
                    mi.subprocess, "run", side_effect=_fake_run_factory(captions=VTT)
                ) as run,
            ):
                code, _, err = self._run_main(
                    [
                        "from-youtube",
                        self.URL,
                        "--sub-langs",
                        "en.*",
                        "--no-cache",
                        "-o",
                        str(Path(tmp) / "t.json"),
                    ]
                )
            self.assertEqual(code, 0, err)
            argv = run.call_args[0][0]
            self.assertIn("--sub-langs", argv)
            self.assertEqual(argv[argv.index("--sub-langs") + 1], "en.*")

    def test_no_captions_is_named_error(self):
        for captions in (None, "WEBVTT\n\nonly a header, no cues"):
            with self.subTest(captions=captions):
                with (
                    patch.object(mi.shutil, "which", side_effect=_which_all),
                    patch.object(
                        mi.subprocess,
                        "run",
                        side_effect=_fake_run_factory(captions=captions),
                    ),
                ):
                    code, out, err = self._run_main(["from-youtube", self.URL, "--no-cache"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("no_captions_available", err)
                # The hint names both caller escape hatches.
                self.assertIn("--transcript-file", err)
                self.assertIn("--asr", err)

    def test_ytdlp_missing_binary_named_error(self):
        with patch.object(mi.shutil, "which", return_value=None):
            code, out, err = self._run_main(["from-youtube", self.URL, "--no-cache"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("yt_dlp_unavailable", err)
        self.assertIn("yt-dlp", err)

    def test_ytdlp_filenotfound_maps_to_named_error(self):
        with (
            patch.object(mi.shutil, "which", side_effect=_which_all),
            patch.object(mi.subprocess, "run", side_effect=FileNotFoundError),
        ):
            code, _, err = self._run_main(["from-youtube", self.URL, "--no-cache"])
        self.assertEqual(code, 1)
        self.assertIn("yt_dlp_unavailable", err)

    def test_ytdlp_nonzero_exit_is_external_tool_error(self):
        fake = _fake_run_factory(exit_code=2, stderr="ERROR: removed video")
        with (
            patch.object(mi.shutil, "which", side_effect=_which_all),
            patch.object(mi.subprocess, "run", side_effect=fake),
        ):
            code, out, err = self._run_main(["from-youtube", self.URL, "--no-cache"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("external_tool_error", err)
        self.assertIn("removed video", err)

    def test_asr_mode_is_local_asr_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            out_path = Path(tmp) / "transcript.json"
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                return _fake_run_factory()(argv, **kwargs)

            before = _ingest_workspace_dirs()
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=fake_run),
            ):
                code, _, err = self._run_main(
                    [
                        "from-youtube",
                        self.URL,
                        "--asr",
                        "--whisper-model",
                        str(model),
                        "--no-cache",
                        "-o",
                        str(out_path),
                    ]
                )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            mi.validate(transcript)
            self.assertEqual(transcript["source"]["kind"], "youtube")
            self.assertEqual(transcript["source"]["ref"], self.URL)
            self.assertEqual(transcript["source"]["transcription_method"], "local_asr")
            self.assertEqual(transcript["source"]["transcription_trust"], "self_transcribed")
            self.assertEqual(
                transcript["segments"],
                [{"t0": 0.0, "t1": 1.5, "speaker": None, "text": "hello world"}],
            )
            ytdlp_argv, whisper_argv = calls
            self.assertEqual(ytdlp_argv[0], "yt-dlp")
            self.assertIn("--audio-format", ytdlp_argv)
            self.assertNotIn("--skip-download", ytdlp_argv)
            self.assertEqual(whisper_argv[0], "whisper-cli")
            self.assertEqual(whisper_argv[whisper_argv.index("-m") + 1], str(model))
            self.assertEqual(_ingest_workspace_dirs(), before)

    def test_asr_and_transcript_file_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_file = Path(tmp) / "t.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            code, out, err = self._run_main(
                [
                    "from-youtube",
                    self.URL,
                    "--asr",
                    "--transcript-file",
                    str(transcript_file),
                    "--no-cache",
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("invalid_usage", err)
            self.assertIn("mutually exclusive", err)

    def test_asr_requires_whisper_model(self):
        code, out, err = self._run_main(["from-youtube", self.URL, "--asr", "--no-cache"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid_usage", err)
        self.assertIn("--whisper-model", err)

    def test_transcript_file_skips_ytdlp(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_file = Path(tmp) / "t.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            out_path = Path(tmp) / "t.json"
            code, _, err = self._run_main(
                [
                    "from-youtube",
                    self.URL,
                    "--transcript-file",
                    str(transcript_file),
                    "--no-cache",
                    "-o",
                    str(out_path),
                ]
            )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            mi.validate(transcript)
            self.assertEqual(transcript["source"]["kind"], "youtube")
            self.assertEqual(transcript["source"]["ref"], self.URL)
            self.assertEqual(transcript["source"]["transcription_method"], "caller_supplied")
            self.assertEqual(len(transcript["segments"]), 2)

    def test_ref_label_overrides_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "t.json"
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory(captions=VTT)),
            ):
                code, _, err = self._run_main(
                    [
                        "from-youtube",
                        self.URL,
                        "--ref",
                        "keynote-2026",
                        "--no-cache",
                        "-o",
                        str(out_path),
                    ]
                )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(transcript["source"]["ref"], "keynote-2026")

    def test_cache_hit_rewrites_ref_and_flags_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            outputs = []
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory(captions=VTT)),
            ):
                for label in ("first-talk", "second-talk"):
                    out_path = Path(tmp) / f"{label}.json"
                    code, _, err = self._run_main(
                        [
                            "from-youtube",
                            self.URL,
                            "--ref",
                            label,
                            "--cache-dir",
                            str(cache_dir),
                            "-o",
                            str(out_path),
                        ]
                    )
                    self.assertEqual(code, 0, err)
                    outputs.append(json.loads(out_path.read_text(encoding="utf-8")))
            first, second = outputs
            mi.validate(first)
            mi.validate(second)
            self.assertNotIn("cache_hit", first)
            self.assertEqual(first["source"]["ref"], "first-talk")
            # Same caption content, different labels: one content_hash.
            self.assertEqual(second["content_hash"], first["content_hash"])
            self.assertIs(second["cache_hit"], True)
            self.assertEqual(second["source"]["ref"], "second-talk")

    def test_numeric_and_ref_args_validated(self):
        for extra, needle in (
            (["--timeout-seconds", "0"], "--timeout-seconds"),
            (["--cache-ttl-seconds", "-1"], "--cache-ttl-seconds"),
            (["--cache-max-files", "0"], "--cache-max-files"),
            (["--ref", "   "], "--ref"),
        ):
            with self.subTest(extra=extra):
                code, out, err = self._run_main(["from-youtube", self.URL, *extra, "--no-cache"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("invalid_usage", err)
                self.assertIn(needle, err)

    def test_transcript_file_non_utf8_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_file = Path(tmp) / "t.srt"
            transcript_file.write_bytes(LATIN1_SRT)
            code, out, err = self._run_main(
                [
                    "from-youtube",
                    self.URL,
                    "--transcript-file",
                    str(transcript_file),
                    "--no-cache",
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("transcript_format_invalid", err)
            self.assertIn("UTF-8", err)

    def test_transcript_file_rejects_inapplicable_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_file = Path(tmp) / "t.srt"
            transcript_file.write_text(SRT, encoding="utf-8")
            for extra in (
                ["--sub-langs", "en.*"],
                ["--whisper-binary", "custom-whisper"],
                ["--whisper-model", "model.bin"],
                ["--ytdlp-binary", "custom-ytdlp"],
            ):
                with self.subTest(extra=extra):
                    code, out, err = self._run_main(
                        [
                            "from-youtube",
                            self.URL,
                            "--transcript-file",
                            str(transcript_file),
                            *extra,
                            "--no-cache",
                        ]
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("invalid_usage", err)
                    self.assertIn(extra[0], err)

    def test_asr_rejects_sub_langs(self):
        code, out, err = self._run_main(
            ["from-youtube", self.URL, "--asr", "--sub-langs", "en.*", "--no-cache"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid_usage", err)
        self.assertIn("--sub-langs", err)

    def test_asr_binary_overrides_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                return _fake_run_factory()(argv, **kwargs)

            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=fake_run),
            ):
                code, _, err = self._run_main(
                    [
                        "from-youtube",
                        self.URL,
                        "--asr",
                        "--whisper-model",
                        str(model),
                        "--whisper-binary",
                        "my-whisper",
                        "--ytdlp-binary",
                        "my-ytdlp",
                        "--no-cache",
                        "-o",
                        str(Path(tmp) / "t.json"),
                    ]
                )
            self.assertEqual(code, 0, err)
            ytdlp_argv, whisper_argv = calls
            self.assertEqual(ytdlp_argv[0], "my-ytdlp")
            self.assertEqual(whisper_argv[0], "my-whisper")

    def test_asr_missing_binaries_fail_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            argv = [
                "from-youtube",
                self.URL,
                "--asr",
                "--whisper-model",
                str(model),
                "--no-cache",
            ]
            # yt-dlp missing: named error before any subprocess runs.
            with (
                patch.object(mi.shutil, "which", return_value=None),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory()) as run,
            ):
                code, out, err = self._run_main(argv)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("yt_dlp_unavailable", err)
            run.assert_not_called()
            # whisper-cli missing: named error before the download
            # starts, not after it completes.
            with (
                patch.object(
                    mi.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/yt-dlp" if name == "yt-dlp" else None,
                ),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory()) as run,
            ):
                code, out, err = self._run_main(argv)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("asr_unavailable", err)
            run.assert_not_called()


class DownloadCaptionsTest(unittest.TestCase):
    def test_vtt_preferred_over_srt_and_first_parseable_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "captions.en.srt").write_text(SRT, encoding="utf-8")
            (work / "captions.ru.vtt").write_text(VTT, encoding="utf-8")
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(
                    mi.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
            ):
                segments = mi.download_captions(
                    "https://example.invalid/v",
                    binary="yt-dlp",
                    timeout_seconds=5,
                    work_dir=work,
                )
            # VTT wins over SRT even though the SRT sorts first by name.
            self.assertEqual(segments[0]["text"], "Hello there.")
            self.assertEqual(len(segments), 2)

    def test_missing_binary_raises_named_error(self):
        with patch.object(mi.shutil, "which", return_value=None):
            with self.assertRaises(mi.YtDlpUnavailable) as ctx:
                mi.download_captions(
                    "https://example.invalid/v",
                    binary="definitely-missing-ytdlp",
                    timeout_seconds=5,
                    work_dir=Path("/tmp"),
                )
        self.assertEqual(ctx.exception.code, "yt_dlp_unavailable")
        self.assertEqual(ctx.exception.binary, "definitely-missing-ytdlp")

    def test_non_utf8_caption_file_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "captions.en.vtt").write_bytes(LATIN1_VTT)
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(
                    mi.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
            ):
                with self.assertRaises(mi.TranscriptFormatError) as ctx:
                    mi.download_captions(
                        "https://example.invalid/v",
                        binary="yt-dlp",
                        timeout_seconds=5,
                        work_dir=work,
                    )
            self.assertEqual(ctx.exception.code, "transcript_format_invalid")
            self.assertIn("UTF-8", str(ctx.exception))

    def test_valid_candidate_beats_non_utf8_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # Sorted order puts the Latin-1 file first; the readable
            # UTF-8 candidate after it must still be picked up.
            (work / "captions.en.vtt").write_bytes(LATIN1_VTT)
            (work / "captions.fr.vtt").write_text(VTT, encoding="utf-8")
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(
                    mi.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ),
            ):
                segments = mi.download_captions(
                    "https://example.invalid/v",
                    binary="yt-dlp",
                    timeout_seconds=5,
                    work_dir=work,
                )
            self.assertEqual(len(segments), 2)
            self.assertEqual(segments[0]["text"], "Hello there.")


class FromWebPageCliTest(unittest.TestCase):
    URL = "https://videos.example.invalid/talks/keynote"

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = mi.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end_local_asr(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            out_path = Path(tmp) / "transcript.json"
            cache_dir = Path(tmp) / "cache"
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                return _fake_run_factory()(argv, **kwargs)

            before = _ingest_workspace_dirs()
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=fake_run),
            ):
                code, _, err = self._run_main(
                    [
                        "from-web-page",
                        self.URL,
                        "--whisper-model",
                        str(model),
                        "--cache-dir",
                        str(cache_dir),
                        "-o",
                        str(out_path),
                    ]
                )
            self.assertEqual(code, 0, err)
            transcript = json.loads(out_path.read_text(encoding="utf-8"))
            mi.validate(transcript)
            self.assertEqual(transcript["source"]["kind"], "web_page")
            self.assertEqual(transcript["source"]["ref"], self.URL)
            self.assertEqual(transcript["source"]["transcription_method"], "local_asr")
            self.assertEqual(transcript["source"]["transcription_trust"], "self_transcribed")
            self.assertEqual(len(transcript["segments"]), 1)
            self.assertEqual(stat.S_IMODE(out_path.stat().st_mode), 0o600)
            cached = cache_dir / f"{transcript['content_hash'].split(':')[1]}.json"
            self.assertTrue(cached.is_file())
            # yt-dlp downloaded the media first, whisper-cli transcribed it.
            ytdlp_argv, whisper_argv = calls
            self.assertEqual(ytdlp_argv[0], "yt-dlp")
            self.assertIn("-x", ytdlp_argv)
            self.assertEqual(ytdlp_argv[-1], self.URL)
            self.assertEqual(whisper_argv[0], "whisper-cli")
            self.assertTrue(whisper_argv[whisper_argv.index("-f") + 1].endswith(".wav"))
            self.assertEqual(_ingest_workspace_dirs(), before)

    def test_requires_whisper_model(self):
        code, out, err = self._run_main(["from-web-page", self.URL, "--no-cache"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid_usage", err)
        self.assertIn("--whisper-model", err)

    def test_no_media_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            # yt-dlp exits 0 but produces no media file: named, never empty.
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory(media=None)),
            ):
                code, out, err = self._run_main(
                    [
                        "from-web-page",
                        self.URL,
                        "--whisper-model",
                        str(model),
                        "--no-cache",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("no_media_found", err)

    def test_unsupported_page_is_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            fake = _fake_run_factory(exit_code=1, stderr="ERROR: Unsupported URL")
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=fake),
            ):
                code, out, err = self._run_main(
                    [
                        "from-web-page",
                        self.URL,
                        "--whisper-model",
                        str(model),
                        "--no-cache",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("external_tool_error", err)
            self.assertIn("Unsupported URL", err)

    def test_missing_binaries_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            argv = [
                "from-web-page",
                self.URL,
                "--whisper-model",
                str(model),
                "--no-cache",
            ]
            # yt-dlp missing -> yt_dlp_unavailable before any subprocess.
            with (
                patch.object(mi.shutil, "which", side_effect=lambda name: None),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory()) as run,
            ):
                code, _, err = self._run_main(argv)
            self.assertEqual(code, 1)
            self.assertIn("yt_dlp_unavailable", err)
            run.assert_not_called()
            # whisper-cli missing -> asr_unavailable before the download:
            # the pre-flight check fires ahead of the first yt-dlp run.
            with (
                patch.object(
                    mi.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/yt-dlp" if name == "yt-dlp" else None,
                ),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory()) as run,
            ):
                code, _, err = self._run_main(argv)
            self.assertEqual(code, 1)
            self.assertIn("asr_unavailable", err)
            run.assert_not_called()

    def test_cache_hit_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            cache_dir = Path(tmp) / "cache"
            outputs = []
            with (
                patch.object(mi.shutil, "which", side_effect=_which_all),
                patch.object(mi.subprocess, "run", side_effect=_fake_run_factory()),
            ):
                for page in ("first", "second"):
                    url = f"https://videos.example.invalid/{page}"
                    out_path = Path(tmp) / f"{page}.json"
                    code, _, err = self._run_main(
                        [
                            "from-web-page",
                            url,
                            "--whisper-model",
                            str(model),
                            "--cache-dir",
                            str(cache_dir),
                            "-o",
                            str(out_path),
                        ]
                    )
                    self.assertEqual(code, 0, err)
                    outputs.append(json.loads(out_path.read_text(encoding="utf-8")))
            first, second = outputs
            mi.validate(first)
            mi.validate(second)
            self.assertNotIn("cache_hit", first)
            # Identical spoken content from two pages shares one hash.
            self.assertEqual(second["content_hash"], first["content_hash"])
            self.assertIs(second["cache_hit"], True)
            self.assertEqual(second["source"]["ref"], "https://videos.example.invalid/second")

    def test_numeric_and_ref_args_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.bin"
            model.write_bytes(b"fake model")
            for extra, needle in (
                (["--timeout-seconds", "-3"], "--timeout-seconds"),
                (["--cache-max-files", "0"], "--cache-max-files"),
                (["--ref", "  "], "--ref"),
            ):
                with self.subTest(extra=extra):
                    code, out, err = self._run_main(
                        [
                            "from-web-page",
                            self.URL,
                            "--whisper-model",
                            str(model),
                            *extra,
                            "--no-cache",
                        ]
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("invalid_usage", err)
                    self.assertIn(needle, err)


if __name__ == "__main__":
    unittest.main()

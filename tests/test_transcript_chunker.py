#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load_tool(name):
    """Load a tools/ script by path, sharing identity via sys.modules.

    transcript_chunker imports media_ingest as a sibling module:
    registering the loaded module in sys.modules first guarantees one
    shared module object, so error classes raised by tc.* are mi.*
    classes too.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


mi = _load_tool("media_ingest")
tc = _load_tool("transcript_chunker")

CHUNK_KEYS = {"chunk_id", "t0", "t1", "segment_ids", "speaker_set", "text", "char_count"}


def _segments(count, size=40, speaker=None):
    """``count`` timed segments of exactly ``size`` chars each."""
    return [
        {
            "t0": float(i * 10),
            "t1": float(i * 10) + 9.0,
            "speaker": speaker,
            "text": f"w{i}" + "x" * (size - len(f"w{i}")),
        }
        for i in range(count)
    ]


def _transcript(segments, **overrides):
    transcript = mi.build_transcript(
        kind="local_file",
        ref="meeting.mp4",
        method="caller_supplied",
        segments=segments,
        generated_at="2026-11-10T12:00:00Z",
    )
    transcript.update(overrides)
    return transcript


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
        code = tc.main(argv)
    return code, out.getvalue(), err.getvalue()


class ChunkShapeTest(unittest.TestCase):
    def test_chunk_field_contract(self):
        transcript = _transcript(_segments(3, speaker="ann"))
        (chunk,) = tc.chunk_transcript(transcript, max_chars=10_000)
        self.assertEqual(set(chunk), CHUNK_KEYS)
        self.assertEqual(chunk["chunk_id"], 0)
        self.assertEqual(chunk["t0"], 0.0)
        self.assertEqual(chunk["t1"], 29.0)
        self.assertEqual(chunk["segment_ids"], [0, 1, 2])
        self.assertEqual(chunk["speaker_set"], ["ann"])
        self.assertEqual(chunk["char_count"], len(chunk["text"]))

    def test_all_segments_covered_exactly_once_in_order(self):
        segments = _segments(9)
        chunks = tc.chunk_transcript(_transcript(segments), max_chars=100)
        covered = [i for chunk in chunks for i in chunk["segment_ids"]]
        self.assertEqual(covered, list(range(9)))
        self.assertEqual([chunk["chunk_id"] for chunk in chunks], list(range(len(chunks))))
        # No chunk's joined text exceeds the bound (none are oversized).
        for chunk in chunks:
            self.assertLessEqual(chunk["char_count"], 100)

    def test_segment_text_never_split(self):
        segments = _segments(7, size=60)
        chunks = tc.chunk_transcript(_transcript(segments), max_chars=150)
        for chunk in chunks:
            texts = [segments[i]["text"] for i in chunk["segment_ids"]]
            self.assertEqual(chunk["text"], " ".join(texts))
            for i in chunk["segment_ids"]:
                # Each covered segment appears verbatim and whole.
                self.assertIn(segments[i]["text"], chunk["text"])

    def test_two_40_char_segments_fit_three_do_not(self):
        # 40 + 1 + 40 = 81 <= 100; adding a third needs 122 > 100.
        chunks = tc.chunk_transcript(_transcript(_segments(5)), max_chars=100)
        self.assertEqual([chunk["segment_ids"] for chunk in chunks], [[0, 1], [2, 3], [4]])

    def test_single_oversized_segment_becomes_own_chunk(self):
        segments = [
            {"t0": 0.0, "t1": 1.0, "speaker": None, "text": "s" * 40},
            {"t0": 1.0, "t1": 2.0, "speaker": None, "text": "B" * 500},
            {"t0": 2.0, "t1": 3.0, "speaker": None, "text": "s" * 40},
        ]
        chunks = tc.chunk_transcript(_transcript(segments), max_chars=100)
        self.assertEqual([chunk["segment_ids"] for chunk in chunks], [[0], [1], [2]])
        self.assertEqual(chunks[1]["char_count"], 500)
        self.assertEqual(chunks[1]["text"], "B" * 500)

    def test_oversized_then_fitting_segments_regroup(self):
        segments = [
            {"t0": 0.0, "t1": 1.0, "speaker": None, "text": "B" * 500},
            {"t0": 1.0, "t1": 2.0, "speaker": None, "text": "a" * 40},
            {"t0": 2.0, "t1": 3.0, "speaker": None, "text": "b" * 40},
        ]
        chunks = tc.chunk_transcript(_transcript(segments), max_chars=100)
        self.assertEqual([chunk["segment_ids"] for chunk in chunks], [[0], [1, 2]])

    def test_null_timestamps_propagate_as_none(self):
        segments = [
            {"t0": None, "t1": None, "speaker": None, "text": "line one"},
            {"t0": None, "t1": None, "speaker": None, "text": "line two"},
        ]
        (chunk,) = tc.chunk_transcript(_transcript(segments))
        self.assertIsNone(chunk["t0"])
        self.assertIsNone(chunk["t1"])

    def test_mixed_null_timestamps_use_available_bounds(self):
        segments = [
            {"t0": None, "t1": None, "speaker": None, "text": "untimed"},
            {"t0": 5.0, "t1": 8.0, "speaker": None, "text": "timed"},
            {"t0": None, "t1": 30.0, "speaker": None, "text": "tail"},
        ]
        (chunk,) = tc.chunk_transcript(_transcript(segments))
        self.assertEqual(chunk["t0"], 5.0)
        self.assertEqual(chunk["t1"], 30.0)

    def test_speaker_set_sorted_and_deduped(self):
        segments = [
            {"t0": 0.0, "t1": 1.0, "speaker": "bob", "text": "one"},
            {"t0": 1.0, "t1": 2.0, "speaker": "ann", "text": "two"},
            {"t0": 2.0, "t1": 3.0, "speaker": "bob", "text": "three"},
            {"t0": 3.0, "t1": 4.0, "speaker": None, "text": "four"},
        ]
        (chunk,) = tc.chunk_transcript(_transcript(segments))
        self.assertEqual(chunk["speaker_set"], ["ann", "bob"])

    def test_segments_without_optional_speaker_key(self):
        # ``speaker`` is optional in the transcript schema: a valid
        # transcript may omit the key entirely, and chunking must not
        # raise KeyError on it.
        transcript = _transcript(_segments(4))
        for segment in transcript["segments"]:
            del segment["speaker"]
        chunks = tc.chunk_transcript(transcript, max_chars=100)
        covered = [i for chunk in chunks for i in chunk["segment_ids"]]
        self.assertEqual(covered, [0, 1, 2, 3])
        for chunk in chunks:
            self.assertEqual(chunk["speaker_set"], [])


class SpeakerPreferenceTest(unittest.TestCase):
    def _diarized(self):
        # Two 30-char turns per speaker; the 100-char bound falls
        # mid-speaker: [ann,ann,bob] = 92 fits, adding bob#2 = 123.
        return [
            {"t0": 0.0, "t1": 1.0, "speaker": "ann", "text": "a" * 30},
            {"t0": 1.0, "t1": 2.0, "speaker": "ann", "text": "a" * 30},
            {"t0": 2.0, "t1": 3.0, "speaker": "bob", "text": "b" * 30},
            {"t0": 3.0, "t1": 4.0, "speaker": "bob", "text": "b" * 30},
        ]

    def test_flush_prefers_speaker_boundary(self):
        chunks = tc.chunk_transcript(_transcript(self._diarized()), max_chars=100)
        self.assertEqual([c["segment_ids"] for c in chunks], [[0, 1], [2, 3]])
        self.assertEqual(chunks[0]["speaker_set"], ["ann"])
        self.assertEqual(chunks[1]["speaker_set"], ["bob"])

    def test_without_speakers_same_bound_flushes_at_tail(self):
        segments = [dict(s, speaker=None) for s in self._diarized()]
        chunks = tc.chunk_transcript(_transcript(segments), max_chars=100)
        self.assertEqual([c["segment_ids"] for c in chunks], [[0, 1, 2], [3]])
        self.assertEqual(chunks[0]["speaker_set"], [])

    def test_bound_still_wins_when_no_speaker_break_helps(self):
        # The remaining turn is itself over the bound after a speaker
        # cut: the flush must keep going until the new segment fits.
        segments = [
            {"t0": 0.0, "t1": 1.0, "speaker": "ann", "text": "a" * 30},
            {"t0": 1.0, "t1": 2.0, "speaker": "bob", "text": "b" * 60},
            {"t0": 2.0, "t1": 3.0, "speaker": "bob", "text": "b" * 60},
            {"t0": 3.0, "t1": 4.0, "speaker": "ann", "text": "a" * 30},
        ]
        chunks = tc.chunk_transcript(_transcript(segments), max_chars=100)
        covered = [i for chunk in chunks for i in chunk["segment_ids"]]
        self.assertEqual(covered, [0, 1, 2, 3])
        # The first chunk holds only ann's turn; bob's oversized pair
        # could not share a 100-char chunk with anything.
        self.assertEqual(chunks[0]["segment_ids"], [0])

    def test_speaker_appearance_from_null_counts_as_change(self):
        segments = [
            {"t0": 0.0, "t1": 1.0, "speaker": None, "text": "n" * 30},
            {"t0": 1.0, "t1": 2.0, "speaker": "ann", "text": "a" * 30},
            {"t0": 2.0, "t1": 3.0, "speaker": "ann", "text": "a" * 30},
            {"t0": 3.0, "t1": 4.0, "speaker": "ann", "text": "a" * 30},
        ]
        chunks = tc.chunk_transcript(_transcript(segments), max_chars=100)
        self.assertEqual([c["segment_ids"] for c in chunks], [[0], [1, 2, 3]])


class ValidationTest(unittest.TestCase):
    def test_max_chars_must_be_positive_int(self):
        transcript = _transcript(_segments(2))
        for bad in (0, -5, True, "8000", 12.5, None):
            with self.subTest(max_chars=bad):
                with self.assertRaises(mi.UsageError) as ctx:
                    tc.chunk_transcript(transcript, bad)
                self.assertEqual(ctx.exception.code, "invalid_usage")

    def test_schema_invalid_source_is_chunking_error(self):
        transcript = _transcript(_segments(2))
        del transcript["segments"]
        with self.assertRaises(tc.ChunkingError) as ctx:
            tc.chunk_transcript(transcript)
        self.assertEqual(ctx.exception.code, "chunking_error")

    def test_unsupported_schema_version_keeps_dedicated_code(self):
        transcript = _transcript(_segments(2), schema_version=2)
        with self.assertRaises(mi.UnsupportedSchemaVersion) as ctx:
            tc.chunk_transcript(transcript)
        self.assertEqual(ctx.exception.code, "unsupported_schema_version")

    def test_empty_segments_is_chunking_error(self):
        with self.assertRaises(tc.ChunkingError) as ctx:
            tc.chunk_transcript(_transcript([]))
        self.assertEqual(ctx.exception.code, "chunking_error")

    def test_chunking_error_subclasses_ingest_error(self):
        self.assertTrue(issubclass(tc.ChunkingError, mi.IngestError))


class SidecarTest(unittest.TestCase):
    def test_sidecar_path(self):
        self.assertEqual(tc.sidecar_path(Path("/x/meeting.json")), Path("/x/meeting.chunks.json"))
        self.assertEqual(tc.sidecar_path(Path("/x/meeting")), Path("/x/meeting.chunks.json"))

    def test_write_chunks_creates_0600_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = _transcript(_segments(4))
            out = tc.write_chunks(transcript, Path(tmp) / "m.chunks.json", max_chars=100)
            self.assertTrue(out.is_file())
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema_version"], 1)
            self.assertEqual(doc["source_hash"], transcript["content_hash"])
            self.assertEqual(doc["generator_version"], tc.GENERATOR_VERSION)
            self.assertRegex(doc["generated_at"], r"Z$")
            self.assertEqual(doc["chunks"], tc.chunk_transcript(transcript, 100))

    def test_atomic_write_failure_keeps_original_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "m.chunks.json"
            out.write_text("sentinel", encoding="utf-8")
            transcript = _transcript(_segments(2))
            with patch.object(tc.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(mi.OutputWriteError) as ctx:
                    tc.write_chunks(transcript, out)
            self.assertEqual(ctx.exception.code, "output_write_failed")
            self.assertEqual(out.read_text(encoding="utf-8"), "sentinel")
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != out.name]
            self.assertEqual(leftovers, [])

    def test_read_chunk_sidecar_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = _transcript(_segments(3))
            out = tc.write_chunks(transcript, Path(tmp) / "m.chunks.json")
            doc = tc.read_chunk_sidecar(out, source_hash=transcript["content_hash"])
            self.assertIsNotNone(doc)
            self.assertEqual(len(doc["chunks"]), 1)

    def test_read_chunk_sidecar_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = _transcript(_segments(3))
            out = tc.write_chunks(transcript, Path(tmp) / "m.chunks.json")
            self.assertIsNone(tc.read_chunk_sidecar(Path(tmp) / "missing.chunks.json"))
            # Hash mismatch: the sidecar must not shadow a changed transcript.
            self.assertIsNone(tc.read_chunk_sidecar(out, source_hash="sha256:" + "0" * 64))
            doc = json.loads(out.read_text(encoding="utf-8"))
            for corrupt in (
                "{not json",
                json.dumps({**doc, "schema_version": 2}),
                json.dumps({**doc, "chunks": []}),
                json.dumps({**doc, "chunks": [{"chunk_id": 0}]}),
                json.dumps({**doc, "chunks": "nope"}),
                json.dumps(["not", "a", "dict"]),
            ):
                with self.subTest(corrupt=corrupt[:60]):
                    out.write_text(corrupt, encoding="utf-8")
                    self.assertIsNone(
                        tc.read_chunk_sidecar(out, source_hash=transcript["content_hash"])
                    )


class CliTest(unittest.TestCase):
    def _write_transcript(self, tmp, name="meeting.json", segments=None):
        path = Path(tmp) / name
        path.write_text(
            json.dumps(_transcript(segments or _segments(4)), indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def test_default_out_path_beside_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write_transcript(tmp)
            code, _, err = _run_main([str(source), "--max-chars", "100"])
            self.assertEqual(code, 0, err)
            out = Path(tmp) / "meeting.chunks.json"
            self.assertTrue(out.is_file())
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([c["segment_ids"] for c in doc["chunks"]], [[0, 1], [2, 3]])

    def test_stdout_prints_document_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write_transcript(tmp)
            code, out, err = _run_main([str(source), "--stdout"])
            self.assertEqual(code, 0, err)
            doc = json.loads(out)
            self.assertEqual(doc["schema_version"], 1)
            self.assertEqual(len(doc["chunks"]), 1)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["meeting.json"])

    def test_custom_out_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write_transcript(tmp)
            out = Path(tmp) / "custom" / "chunks.json"
            # Missing parent dir is a named error, not a traceback.
            code, _, err = _run_main([str(source), "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertIn("output_write_failed", err)
            out.parent.mkdir()
            code, _, err = _run_main([str(source), "--out", str(out)])
            self.assertEqual(code, 0, err)
            self.assertTrue(out.is_file())

    def test_bad_max_chars_is_invalid_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write_transcript(tmp)
            for bad in ("0", "-10"):
                with self.subTest(bad=bad):
                    code, _, err = _run_main([str(source), "--max-chars", bad])
                    self.assertEqual(code, 1)
                    self.assertIn("invalid_usage", err)

    def test_missing_transcript_is_invalid_usage(self):
        code, _, err = _run_main(["/nonexistent/ghost.json"])
        self.assertEqual(code, 1)
        self.assertIn("invalid_usage", err)

    def test_invalid_transcript_is_chunking_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.json"
            source.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            code, _, err = _run_main([str(source)])
            self.assertEqual(code, 1)
            self.assertIn("chunking_error", err)

    def test_foreign_json_is_invalid_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.json"
            source.write_text("{corrupt", encoding="utf-8")
            code, _, err = _run_main([str(source)])
            self.assertEqual(code, 1)
            self.assertIn("invalid_usage", err)

    def test_out_and_stdout_are_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._write_transcript(tmp)
            err = io.StringIO()
            with patch.object(sys, "stderr", err):
                with self.assertRaises(SystemExit):
                    tc.main([str(source), "--out", "x.json", "--stdout"])


if __name__ == "__main__":
    unittest.main()

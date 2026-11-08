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

    transcript_rag imports media_ingest and transcript_chunker as
    sibling modules: registering each loaded module in sys.modules
    first guarantees one shared module object, so error classes raised
    by rag.* are mi.* classes too.
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
rag = _load_tool("transcript_rag")

RESULT_KEYS = {
    "path",
    "content_hash",
    "chunk_id",
    "t0",
    "t1",
    "speaker_set",
    "score",
    "snippet",
}


def _segments(*texts, start=0.0):
    return [
        {"t0": start + i * 10.0, "t1": start + i * 10.0 + 9.0, "speaker": None, "text": t}
        for i, t in enumerate(texts)
    ]


def _transcript(segments, ref="meeting.mp4"):
    return mi.build_transcript(
        kind="local_file",
        ref=ref,
        method="caller_supplied",
        segments=segments,
        generated_at="2026-11-10T12:00:00Z",
    )


def _write_transcript(tmp, name, texts):
    path = Path(tmp) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    transcript = _transcript(_segments(*texts), ref=name)
    path.write_text(json.dumps(transcript, indent=2) + "\n", encoding="utf-8")
    return path, transcript


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
        code = rag.main(argv)
    return code, out.getvalue(), err.getvalue()


class TokenizeTest(unittest.TestCase):
    def test_lowercase_and_punctuation_split(self):
        self.assertEqual(
            rag.tokenize("Hello, World! Budget-2026 forecast."),
            ["hello", "world", "budget", "2026", "forecast"],
        )

    def test_short_tokens_dropped(self):
        self.assertEqual(rag.tokenize("a I an is it ok"), ["an", "is", "it", "ok"])

    def test_underscore_splits(self):
        self.assertEqual(rag.tokenize("foo_bar baz_qux"), ["foo", "bar", "baz", "qux"])

    def test_unicode_alnum_kept(self):
        self.assertEqual(rag.tokenize("café naïve"), ["café", "naïve"])

    def test_deterministic(self):
        self.assertEqual(rag.tokenize("Some Text 123"), rag.tokenize("Some Text 123"))


class BuildTest(unittest.TestCase):
    def test_build_writes_valid_index_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "a.json", ["alpha beta gamma", "delta epsilon"])
            sub = Path(tmp) / "nested"
            _write_transcript(tmp, "nested/b.json", ["zeta eta theta"])
            index_path = Path(tmp) / "index.json"
            code, _, err = _run_main(["build", tmp, "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            self.assertEqual(stat.S_IMODE(index_path.stat().st_mode), 0o600)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            rag.validate_index(index)
            self.assertEqual(index["schema_version"], 1)
            self.assertEqual(len(index["doc_sources"]), 2)
            paths = {s["path"] for s in index["doc_sources"]}
            self.assertIn(str((Path(tmp) / "a.json").resolve()), paths)
            self.assertIn(str((sub / "b.json").resolve()), paths)
            self.assertGreater(index["avg_doc_length"], 0)
            self.assertIn("alpha", index["postings"])

    def test_incremental_skips_unchanged_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "a.json", ["alpha beta"])
            _write_transcript(tmp, "b.json", ["gamma delta"])
            index_path = Path(tmp) / "index.json"
            self.assertEqual(_run_main(["build", tmp, "--index", str(index_path)])[0], 0)
            calls = []
            original = rag.tc.chunk_transcript

            def counting(*args, **kwargs):
                calls.append(1)
                return original(*args, **kwargs)

            with patch.object(rag.tc, "chunk_transcript", side_effect=counting):
                code, _, err = _run_main(["build", tmp, "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            self.assertEqual(calls, [])
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(len(index["doc_sources"]), 2)

    def test_changed_document_is_reindexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, transcript = _write_transcript(tmp, "a.json", ["alpha beta"])
            old_hash = transcript["content_hash"]
            index_path = Path(tmp) / "index.json"
            self.assertEqual(_run_main(["build", tmp, "--index", str(index_path)])[0], 0)
            _write_transcript(tmp, "a.json", ["alpha beta", "brand new content"])
            code, _, err = _run_main(["build", tmp, "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            (source,) = index["doc_sources"]
            self.assertNotEqual(source["content_hash"], old_hash)
            self.assertIn("brand", index["postings"])
            self.assertNotIn(old_hash, index["chunks"])
            self.assertIn("dropped", err)

    def test_removed_document_dropped_from_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "a.json", ["alpha beta"])
            gone, transcript = _write_transcript(tmp, "b.json", ["gamma delta"])
            index_path = Path(tmp) / "index.json"
            self.assertEqual(_run_main(["build", tmp, "--index", str(index_path)])[0], 0)
            gone.unlink()
            code, _, err = _run_main(["build", tmp, "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(len(index["doc_sources"]), 1)
            self.assertNotIn(transcript["content_hash"], index["chunks"])
            self.assertIn("dropped", err)

    def test_rebuild_rechunks_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "a.json", ["alpha beta"])
            index_path = Path(tmp) / "index.json"
            self.assertEqual(_run_main(["build", tmp, "--index", str(index_path)])[0], 0)
            calls = []
            original = rag.tc.chunk_transcript

            def counting(*args, **kwargs):
                calls.append(1)
                return original(*args, **kwargs)

            with patch.object(rag.tc, "chunk_transcript", side_effect=counting):
                code, _, err = _run_main(["build", tmp, "--index", str(index_path), "--rebuild"])
            self.assertEqual(code, 0, err)
            self.assertEqual(len(calls), 1)

    def test_malformed_files_warned_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "good.json", ["alpha beta"])
            (Path(tmp) / "corrupt.json").write_text("{not json", encoding="utf-8")
            (Path(tmp) / "foreign.json").write_text('{"a": 1}', encoding="utf-8")
            index_path = Path(tmp) / "index.json"
            code, _, err = _run_main(["build", tmp, "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(len(index["doc_sources"]), 1)
            self.assertIn("corrupt.json", err)
            self.assertIn("foreign.json", err)

    def test_empty_corpus_is_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = _run_main(["build", tmp, "--index", str(Path(tmp) / "i.json")])
            self.assertEqual(code, 1)
            self.assertIn("rag_corpus_empty", err)

    def test_missing_path_warned_then_empty_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "gone")
            code, _, err = _run_main(["build", missing, "--index", str(Path(tmp) / "i.json")])
            self.assertEqual(code, 1)
            self.assertIn("not found", err)
            self.assertIn("rag_corpus_empty", err)

    def test_corrupt_existing_index_needs_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "a.json", ["alpha beta"])
            index_path = Path(tmp) / "index.json"
            index_path.write_text("{corrupt", encoding="utf-8")
            code, _, err = _run_main(["build", tmp, "--index", str(index_path)])
            self.assertEqual(code, 1)
            self.assertIn("rag_index_error", err)
            # --rebuild ignores the corrupt file entirely.
            code, _, err = _run_main(["build", tmp, "--index", str(index_path), "--rebuild"])
            self.assertEqual(code, 0, err)

    def test_bad_max_chars_is_invalid_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "a.json", ["alpha beta"])
            code, _, err = _run_main(
                ["build", tmp, "--index", str(Path(tmp) / "i.json"), "--max-chars", "0"]
            )
            self.assertEqual(code, 1)
            self.assertIn("invalid_usage", err)

    def test_foreign_json_inside_scanned_dir_warns_but_builds(self):
        # A foreign *.json inside the corpus dir is skipped with a
        # warning, not a crash.
        with tempfile.TemporaryDirectory() as tmp:
            _write_transcript(tmp, "a.json", ["alpha beta"])
            (Path(tmp) / "foreign.json").write_text("{}", encoding="utf-8")
            code, _, err = _run_main(["build", tmp, "--index", str(Path(tmp) / "index.json")])
            self.assertEqual(code, 0, err)
            self.assertIn("foreign.json", err)


class SidecarUseTest(unittest.TestCase):
    def test_matching_sidecar_used_instead_of_chunking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, transcript = _write_transcript(
                tmp, "meeting.json", ["alpha beta gamma", "delta epsilon zeta"]
            )
            tc.write_chunks(transcript, tc.sidecar_path(path), max_chars=100)
            calls = []

            def boom(*args, **kwargs):
                calls.append(1)
                raise AssertionError("chunk_transcript must not run")

            with patch.object(rag.tc, "chunk_transcript", side_effect=boom):
                code, _, err = _run_main(["build", tmp, "--index", str(Path(tmp) / "i.json")])
            self.assertEqual(code, 0, err)
            self.assertEqual(calls, [])
            index = json.loads((Path(tmp) / "i.json").read_text(encoding="utf-8"))
            sidecar = tc.read_chunk_sidecar(
                tc.sidecar_path(path), source_hash=transcript["content_hash"]
            )
            (stored,) = index["chunks"].values()
            self.assertEqual([c["text"] for c in stored], [c["text"] for c in sidecar["chunks"]])

    def test_mismatched_sidecar_falls_back_to_chunking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = _write_transcript(tmp, "meeting.json", ["alpha beta"])
            sidecar_path = tc.sidecar_path(path)
            tc.write_chunks(_transcript(_segments("stale content")), sidecar_path)
            calls = []
            original = rag.tc.chunk_transcript

            def counting(*args, **kwargs):
                calls.append(1)
                return original(*args, **kwargs)

            with patch.object(rag.tc, "chunk_transcript", side_effect=counting):
                code, _, err = _run_main(["build", tmp, "--index", str(Path(tmp) / "i.json")])
            self.assertEqual(code, 0, err)
            self.assertEqual(len(calls), 1)
            index = json.loads((Path(tmp) / "i.json").read_text(encoding="utf-8"))
            (stored,) = index["chunks"].values()
            self.assertNotIn("stale content", json.dumps(stored))

    def test_sidecar_never_written_implicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = _write_transcript(tmp, "meeting.json", ["alpha beta"])
            self.assertEqual(_run_main(["build", tmp, "--index", str(Path(tmp) / "i.json")])[0], 0)
            self.assertFalse(tc.sidecar_path(path).exists())


class QueryTest(unittest.TestCase):
    def _corpus(self, tmp):
        a, ta = _write_transcript(
            tmp,
            "finance.json",
            [
                "the quarterly budget forecast shows revenue growth",
                "budget review meeting with the finance committee",
            ],
        )
        _write_transcript(
            tmp,
            "kitchen.json",
            ["today's lunch menu features tomato soup", "recipe ideas for dinner"],
        )
        index_path = Path(tmp) / "index.json"
        code, _, err = _run_main(["build", tmp, "--index", str(index_path)])
        self.assertEqual(code, 0, err)
        return index_path, a, ta

    def test_relevant_document_ranks_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path, a, ta = self._corpus(tmp)
            code, out, err = _run_main(["query", "budget forecast", "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            results = json.loads(out)
            self.assertTrue(results)
            self.assertEqual(results[0]["path"], str(a.resolve()))
            self.assertEqual(results[0]["content_hash"], ta["content_hash"])
            self.assertGreater(results[0]["score"], 0.0)
            self.assertIn("budget", results[0]["snippet"].lower())

    def test_result_field_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path, _, _ = self._corpus(tmp)
            code, out, _ = _run_main(["query", "budget", "--index", str(index_path)])
            self.assertEqual(code, 0)
            for hit in json.loads(out):
                self.assertEqual(set(hit), RESULT_KEYS)
                self.assertIsInstance(hit["score"], float)
                self.assertIsInstance(hit["snippet"], str)
                self.assertIsInstance(hit["speaker_set"], list)

    def test_scores_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path, _, _ = self._corpus(tmp)
            _, out, _ = _run_main(["query", "budget", "--index", str(index_path)])
            scores = [hit["score"] for hit in json.loads(out)]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_k_bounds_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path, _, _ = self._corpus(tmp)
            code, out, _ = _run_main(
                ["query", "budget", "--index", str(index_path), "--top-k", "1"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)), 1)

    def test_no_match_returns_empty_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path, _, _ = self._corpus(tmp)
            code, out, err = _run_main(["query", "zzzqqqterm", "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), [])

    def test_missing_index_is_rag_index_error(self):
        code, _, err = _run_main(["query", "budget", "--index", "/nonexistent/i.json"])
        self.assertEqual(code, 1)
        self.assertIn("rag_index_error", err)

    def test_corrupt_index_names_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.json"
            index_path.write_text('{\n"schema_version": 1,\n"chunks": [oops', encoding="utf-8")
            code, _, err = _run_main(["query", "budget", "--index", str(index_path)])
            self.assertEqual(code, 1)
            self.assertIn("rag_index_error", err)
            self.assertIn("line", err)

    def test_stale_index_is_rag_index_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.json"
            index_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            code, _, err = _run_main(["query", "budget", "--index", str(index_path)])
            self.assertEqual(code, 1)
            self.assertIn("rag_index_error", err)
            self.assertIn("99", err)

    def test_empty_question_is_invalid_usage(self):
        code, _, err = _run_main(["query", "   ", "--index", "x.json"])
        self.assertEqual(code, 1)
        self.assertIn("invalid_usage", err)

    def test_untokenizable_question_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path, _, _ = self._corpus(tmp)
            code, out, err = _run_main(["query", "!", "--index", str(index_path)])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), [])

    def test_bad_top_k_is_invalid_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path, _, _ = self._corpus(tmp)
            code, _, err = _run_main(
                ["query", "budget", "--index", str(index_path), "--top-k", "0"]
            )
            self.assertEqual(code, 1)
            self.assertIn("invalid_usage", err)

    def test_index_and_ephemeral_are_exclusive(self):
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit):
                rag.main(["query", "x", "--index", "i.json", "--ephemeral", "a.json"])

    def test_ephemeral_queries_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _write_transcript(tmp, "a.json", ["budget forecast revenue"])
            _write_transcript(tmp, "b.json", ["tomato soup recipe"])
            before = sorted(p.name for p in Path(tmp).iterdir())
            code, out, err = _run_main(
                ["query", "budget", "--ephemeral", str(a), str(Path(tmp) / "b.json")]
            )
            self.assertEqual(code, 0, err)
            results = json.loads(out)
            self.assertTrue(results)
            self.assertEqual(results[0]["path"], str(a.resolve()))
            after = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(before, after)

    def test_ephemeral_empty_corpus_is_named_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "foreign.json").write_text("{}", encoding="utf-8")
            code, _, err = _run_main(["query", "budget", "--ephemeral", tmp])
            self.assertEqual(code, 1)
            self.assertIn("rag_corpus_empty", err)


class SnippetTest(unittest.TestCase):
    def test_short_text_returned_whole(self):
        self.assertEqual(rag._snippet("short text", ["short"]), "short text")

    def test_long_text_without_match_truncates(self):
        snippet = rag._snippet("x" * 300, ["nomatch"])
        self.assertTrue(snippet.endswith("..."))
        self.assertLessEqual(len(snippet), rag.SNIPPET_CHARS + 3)

    def test_match_centers_snippet(self):
        text = "x" * 200 + " needle here " + "y" * 200
        snippet = rag._snippet(text, ["needle"])
        self.assertIn("needle", snippet)
        self.assertTrue(snippet.startswith("..."))
        self.assertTrue(snippet.endswith("..."))


if __name__ == "__main__":
    unittest.main()

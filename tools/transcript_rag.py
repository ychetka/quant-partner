#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Offline BM25 retrieval over transcript.json corpora (issue #61).

This script is NOT part of the MCP server: it builds a deterministic,
stdlib-only BM25 index (``k1=1.5``, ``b=0.75``) over transcript chunks
and answers queries with ranked, citation-backed hits a caller can
paste to ``media-partner`` instead of a whole corpus. There are no
embeddings and no network calls in v1 - retrieval is pure term
matching, which keeps results reproducible run to run.

Chunk sources:

- For every transcript the indexer first looks for a
  ``<name>.chunks.json`` sidecar written by
  ``tools/transcript_chunker.py``; it is used only when it parses,
  declares ``schema_version`` 1 and its ``source_hash`` equals the
  transcript's ``content_hash``. Otherwise the transcript is chunked
  on the fly in memory - sidecars are read, never written, by this
  tool.

Index file (atomic write, mode 0600):

- ``{schema_version: 1, generator_version, generated_at,
  doc_sources: [{path, content_hash}], chunks: {<content_hash>:
  [{chunk fields + token_count}]}, postings: {term: [[content_hash,
  chunk_id, tf], ...]}, doc_lengths: {<content_hash>: {<chunk_id>:
  n}}, avg_doc_length: float}``. Documents are keyed by
  ``content_hash`` - the canonical content identity shared with the
  ingest cache - so identical transcripts share one chunk set while
  every discovered path stays listed in ``doc_sources``.
- ``build <path...> --index PATH`` scans directories recursively for
  ``*.json`` transcripts (``*.chunks.json`` sidecars excluded) and
  accepts files directly. Each candidate is validated through
  ``media_ingest.validate()``; unreadable, non-JSON or schema-invalid
  files are skipped with a stderr warning and never crash the build.
  The build is incremental: documents whose ``content_hash`` is
  already in ``doc_sources`` keep their stored chunks unless
  ``--rebuild`` is passed, and documents no longer discovered are
  dropped from the index.
- ``query --index PATH "question" --top-k N`` prints a JSON array of
  ``{path, content_hash, chunk_id, t0, t1, speaker_set, score,
  snippet}`` sorted by descending BM25 score (ties broken by
  content_hash then chunk_id, so output is stable). ``--ephemeral
  <paths...>`` builds the same index in memory over ad-hoc files and
  writes nothing.
- Tokenizer: lowercase, split on non-alphanumeric runs, drop tokens
  shorter than 2 chars; no stemming (stdlib only), fully
  deterministic.

All user-facing failures are named errors on stderr
(``ERROR <code>: <message>``, exit 1), never tracebacks: a missing,
stale or corrupt index is ``rag_index_error`` (corruption is reported
with the 1-based JSON line or the offending key path), an empty corpus
is ``rag_corpus_empty`` and bad arguments are ``invalid_usage``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# media_ingest and transcript_chunker are sibling tools, not installed
# packages: import them from this script's directory so the schema
# contract, the chunker and the named-error pattern are shared.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_ingest as mi
import transcript_chunker as tc
from media_ingest import IngestError, UsageError, validate

SCHEMA_VERSION = 1
GENERATOR_VERSION = "transcript_rag/0.1.0"

BM25_K1 = 1.5
BM25_B = 0.75
MIN_TOKEN_LENGTH = 2
DEFAULT_TOP_K = 5
SNIPPET_CHARS = 160
# The snippet is centered on the earliest matched query term; this
# fraction of the window leads the match for context.
_SNIPPET_LEAD = SNIPPET_CHARS // 3

_INDEX_KEYS = {
    "schema_version",
    "generator_version",
    "generated_at",
    "doc_sources",
    "chunks",
    "postings",
    "doc_lengths",
    "avg_doc_length",
}
# Word characters minus the underscore connector: unicode-aware
# letters and digits, so non-ASCII transcripts tokenize like ASCII
# ones. Deterministic per Python's re semantics.
_TOKEN_RE = re.compile(r"[^\W_]+")

_REBUILD_HINT = "rebuild it: transcript_rag.py build <paths> --index PATH --rebuild"


class RagError(IngestError):
    """Base class for the retrieval tool's named failures."""

    code = "rag_error"


class RagIndexError(RagError):
    """The persisted index is missing, stale or corrupt."""

    code = "rag_index_error"


class RagCorpusEmpty(RagError):
    """No valid transcripts were found to index or query."""

    code = "rag_corpus_empty"


# --- tokenizer ---------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric runs of >= MIN_TOKEN_LENGTH chars; deterministic."""
    return [token for token in _TOKEN_RE.findall(text.lower()) if len(token) >= MIN_TOKEN_LENGTH]


# --- corpus scanning -----------------------------------------------------------


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _load_transcript(path: Path) -> dict[str, Any] | None:
    """Read and schema-validate one transcript; ``None`` after a warning.

    Every failure mode - unreadable file, non-UTF-8 bytes, foreign or
    corrupt JSON, schema violation - is reported on stderr and skipped,
    so one bad file in a directory never aborts the build.
    """
    try:
        raw = mi._read_utf8_text(path)
    except IngestError as e:
        _warn(f"{path}: skipped ({e.message})")
        return None
    except OSError as e:
        _warn(f"{path}: skipped (cannot read: {e})")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _warn(f"{path}: skipped (not valid JSON at line {e.lineno}: {e.msg})")
        return None
    try:
        validate(data)
    except IngestError as e:
        _warn(f"{path}: skipped (not a valid transcript: {e.message})")
        return None
    return data


def gather_documents(inputs: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Resolve files/dirs to ``(path, transcript)`` pairs; never raises.

    Directories are scanned recursively for ``*.json`` (chunks sidecars
    excluded); explicit file arguments are accepted as-is. Each
    candidate is schema-validated and invalid files are skipped with a
    warning. Paths are stored resolved so index entries stay valid
    regardless of the caller's working directory.
    """
    candidates: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            try:
                candidates.extend(
                    p for p in sorted(path.rglob("*.json")) if not p.name.endswith(".chunks.json")
                )
            except OSError as e:
                _warn(f"{path}: cannot scan directory ({e})")
        elif path.is_file():
            candidates.append(path)
        else:
            _warn(f"path not found, skipped: {path}")
    seen: set[str] = set()
    documents: list[tuple[str, dict[str, Any]]] = []
    for candidate in candidates:
        try:
            resolved = str(candidate.resolve())
        except OSError:
            resolved = str(candidate.absolute())
        if resolved in seen:
            continue
        seen.add(resolved)
        transcript = _load_transcript(candidate)
        if transcript is not None:
            documents.append((resolved, transcript))
    return documents


def _chunks_for(path: str, transcript: dict[str, Any], max_chars: int) -> list[dict[str, Any]]:
    """Reuse a matching ``*.chunks.json`` sidecar, else chunk in memory."""
    sidecar = tc.read_chunk_sidecar(tc.sidecar_path(path), source_hash=transcript["content_hash"])
    if sidecar is not None:
        return [dict(chunk) for chunk in sidecar["chunks"]]
    return tc.chunk_transcript(transcript, max_chars)


# --- index build -------------------------------------------------------------


def _compile_index(
    chunks_map: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[list[Any]]], dict[str, dict[str, int]], float]:
    """Derive postings, doc_lengths and avg_doc_length over ``chunks_map``.

    Iteration order is fixed (sorted document ids, stored chunk order,
    sorted terms) so the emitted index is byte-reproducible for
    identical input. Each chunk's ``token_count`` is (re)computed here
    rather than trusted from the source, keeping sidecar and on-the-fly
    chunks on one code path.
    """
    postings: dict[str, list[list[Any]]] = {}
    doc_lengths: dict[str, dict[str, int]] = {}
    total = 0
    count = 0
    for doc_id in sorted(chunks_map):
        lengths: dict[str, int] = {}
        for chunk in chunks_map[doc_id]:
            counts = Counter(tokenize(chunk["text"]))
            length = sum(counts.values())
            total += length
            count += 1
            lengths[str(chunk["chunk_id"])] = length
            chunk["token_count"] = length
            for term in sorted(counts):
                postings.setdefault(term, []).append([doc_id, chunk["chunk_id"], counts[term]])
        doc_lengths[doc_id] = lengths
    avg = total / count if count else 0.0
    return postings, doc_lengths, avg


def build_index(
    documents: list[tuple[str, dict[str, Any]]],
    *,
    existing: dict[str, Any] | None = None,
    max_chars: int = tc.DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Assemble the index document over ``(path, transcript)`` pairs.

    Incremental contract: when ``existing`` is a previously built index,
    documents whose ``content_hash`` is already present keep their
    stored chunks (no re-chunking, no sidecar consult); documents not
    re-discovered by the caller are simply absent from the new
    ``doc_sources``, i.e. dropped. Postings and lengths are always
    recompiled from the final chunk set so the derived structures can
    never drift from the stored chunks.
    """
    reusable: dict[str, list[dict[str, Any]]] = {}
    if existing is not None:
        old_chunks = existing.get("chunks", {})
        old_sources = existing.get("doc_sources", [])
        if isinstance(old_chunks, dict) and isinstance(old_sources, list):
            for source in old_sources:
                if not isinstance(source, dict):
                    continue
                doc_id = source.get("content_hash")
                stored = old_chunks.get(doc_id) if isinstance(doc_id, str) else None
                if isinstance(stored, list) and doc_id not in reusable:
                    reusable[doc_id] = [dict(chunk) for chunk in stored]
    doc_sources: list[dict[str, str]] = []
    chunks_map: dict[str, list[dict[str, Any]]] = {}
    for path, transcript in documents:
        doc_id = transcript["content_hash"]
        if doc_id not in chunks_map:
            if doc_id in reusable:
                chunks_map[doc_id] = reusable[doc_id]
            else:
                chunks_map[doc_id] = _chunks_for(path, transcript, max_chars)
        doc_sources.append({"path": path, "content_hash": doc_id})
    doc_sources.sort(key=lambda source: source["path"])
    postings, doc_lengths, avg = _compile_index(chunks_map)
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "generated_at": mi._now_iso_z(),
        "doc_sources": doc_sources,
        "chunks": chunks_map,
        "postings": postings,
        "doc_lengths": doc_lengths,
        "avg_doc_length": avg,
    }


# --- index validation and loading ---------------------------------------------


def validate_index(obj: Any) -> None:
    """Raise ``RagIndexError`` unless ``obj`` matches the index schema.

    Failure messages carry a one-based location - an array index or a
    ``key[index]`` path - so a hand-edited or truncated index is
    diagnosable without a debugger. A ``schema_version`` other than 1
    is reported as a stale index.
    """
    if not isinstance(obj, dict):
        raise RagIndexError("index is not a JSON object", _REBUILD_HINT)
    version = obj.get("schema_version")
    if version != SCHEMA_VERSION:
        raise RagIndexError(
            f"index schema_version is {version!r}, expected "
            f"{SCHEMA_VERSION} (stale or foreign index)",
            _REBUILD_HINT,
        )
    for key in sorted(_INDEX_KEYS - {"schema_version"}):
        if key not in obj:
            raise RagIndexError(f"index is missing key: {key}", _REBUILD_HINT)
    doc_sources = obj["doc_sources"]
    if not isinstance(doc_sources, list):
        raise RagIndexError("doc_sources must be an array", _REBUILD_HINT)
    for index, source in enumerate(doc_sources):
        where = f"doc_sources[{index}]"
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("path"), str)
            or not isinstance(source.get("content_hash"), str)
        ):
            raise RagIndexError(
                f"{where} must be an object with string path and content_hash",
                _REBUILD_HINT,
            )
    chunks = obj["chunks"]
    if not isinstance(chunks, dict):
        raise RagIndexError("chunks must be an object keyed by content_hash", _REBUILD_HINT)
    for doc_id in sorted(chunks):
        chunk_list = chunks[doc_id]
        if not isinstance(chunk_list, list):
            raise RagIndexError(f"chunks[{doc_id!r}] must be an array", _REBUILD_HINT)
        for index, chunk in enumerate(chunk_list):
            where = f"chunks[{doc_id!r}][{index}]"
            if not isinstance(chunk, dict):
                raise RagIndexError(f"{where} must be an object", _REBUILD_HINT)
            if not isinstance(chunk.get("text"), str):
                raise RagIndexError(f"{where}.text must be a string", _REBUILD_HINT)
            if isinstance(chunk.get("chunk_id"), bool) or not isinstance(
                chunk.get("chunk_id"), int
            ):
                raise RagIndexError(f"{where}.chunk_id must be an integer", _REBUILD_HINT)
    postings = obj["postings"]
    if not isinstance(postings, dict):
        raise RagIndexError("postings must be an object keyed by term", _REBUILD_HINT)
    for term in sorted(postings):
        entries = postings[term]
        if not isinstance(entries, list):
            raise RagIndexError(f"postings[{term!r}] must be an array", _REBUILD_HINT)
        for index, entry in enumerate(entries):
            where = f"postings[{term!r}][{index}]"
            if (
                not isinstance(entry, list)
                or len(entry) != 3
                or not isinstance(entry[0], str)
                or isinstance(entry[1], bool)
                or not isinstance(entry[1], int)
                or isinstance(entry[2], bool)
                or not isinstance(entry[2], int)
            ):
                raise RagIndexError(f"{where} must be [content_hash, chunk_id, tf]", _REBUILD_HINT)
    doc_lengths = obj["doc_lengths"]
    if not isinstance(doc_lengths, dict):
        raise RagIndexError("doc_lengths must be an object keyed by content_hash", _REBUILD_HINT)
    for doc_id in sorted(doc_lengths):
        lengths = doc_lengths[doc_id]
        if not isinstance(lengths, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in lengths.items()
        ):
            raise RagIndexError(
                f"doc_lengths[{doc_id!r}] must map chunk_id to integer length",
                _REBUILD_HINT,
            )
    avg = obj["avg_doc_length"]
    if (
        isinstance(avg, bool)
        or not isinstance(avg, (int, float))
        or not math.isfinite(avg)
        or avg < 0
    ):
        raise RagIndexError("avg_doc_length must be a non-negative finite number", _REBUILD_HINT)


def load_index(path: Path) -> dict[str, Any]:
    """Read and validate a persisted index; ``RagIndexError`` on any failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise RagIndexError(f"index is not valid UTF-8: {path}", _REBUILD_HINT) from e
    except OSError as e:
        raise RagIndexError(
            f"cannot read index: {e}",
            "build it first: transcript_rag.py build <paths> --index PATH",
        ) from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RagIndexError(
            f"index is corrupt JSON at line {e.lineno}: {e.msg}", _REBUILD_HINT
        ) from e
    validate_index(data)
    return data


# --- BM25 query ----------------------------------------------------------------


def _snippet(text: str, terms: list[str]) -> str:
    """~SNIPPET_CHARS of chunk text centered on the earliest matched term."""
    if len(text) <= SNIPPET_CHARS:
        return text
    lower = text.lower()
    first = min((pos for term in terms if (pos := lower.find(term)) != -1), default=None)
    if first is None:
        return text[:SNIPPET_CHARS] + "..."
    start = max(0, first - _SNIPPET_LEAD)
    end = min(len(text), start + SNIPPET_CHARS)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet += "..."
    return snippet


def query_index(index: dict[str, Any], question: str, top_k: int) -> list[dict[str, Any]]:
    """Score every indexed chunk with BM25 and return up to ``top_k`` hits.

    Only positive-score chunks are returned, so a query with no term
    overlap yields ``[]`` rather than noise. Results are sorted by
    descending score with ``(content_hash, chunk_id)`` tie-breaks, and
    scores are rounded to 6 decimals - output is deterministic for a
    fixed index and question.
    """
    terms = sorted(set(tokenize(question)))
    if not terms or top_k <= 0:
        return []
    doc_lengths = index["doc_lengths"]
    n_docs = sum(len(lengths) for lengths in doc_lengths.values())
    avg = index["avg_doc_length"]
    if n_docs == 0 or avg <= 0:
        return []
    postings = index["postings"]
    # Per-term inverted lookup: term -> {(doc_id, chunk_id): tf}.
    term_hits: dict[str, dict[tuple[str, int], int]] = {}
    doc_freq: dict[str, int] = {}
    for term in terms:
        entries = postings.get(term, [])
        doc_freq[term] = len(entries)
        term_hits[term] = {(doc_id, chunk_id): tf for doc_id, chunk_id, tf in entries}
    path_of: dict[str, str] = {}
    for source in sorted(index["doc_sources"], key=lambda s: s["path"]):
        path_of.setdefault(source["content_hash"], source["path"])
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for doc_id in sorted(index["chunks"]):
        for chunk in index["chunks"][doc_id]:
            dl = doc_lengths.get(doc_id, {}).get(str(chunk["chunk_id"]), 0)
            score = 0.0
            for term in terms:
                tf = term_hits[term].get((doc_id, chunk["chunk_id"]), 0)
                if tf == 0:
                    continue
                idf = math.log((n_docs - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5) + 1.0)
                score += (
                    idf
                    * (tf * (BM25_K1 + 1.0))
                    / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avg))
                )
            if score > 0.0:
                scored.append((score, doc_id, chunk))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]["chunk_id"]))
    return [
        {
            "path": path_of.get(doc_id, ""),
            "content_hash": doc_id,
            "chunk_id": chunk["chunk_id"],
            "t0": chunk["t0"],
            "t1": chunk["t1"],
            "speaker_set": chunk["speaker_set"],
            "score": round(score, 6),
            "snippet": _snippet(chunk["text"], terms),
        }
        for score, doc_id, chunk in scored[:top_k]
    ]


# --- CLI -----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcript_rag",
        description=(
            "Deterministic stdlib-only BM25 retrieval over transcript.json "
            "corpora: build a persistent index or query ad-hoc files."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser(
        "build",
        help="scan transcripts and write the BM25 index (atomic, mode 0600)",
    )
    build.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="transcript.json files or directories scanned recursively for *.json",
    )
    build.add_argument("--index", required=True, metavar="PATH", help="index file to write")
    build.add_argument(
        "--rebuild",
        action="store_true",
        help="re-chunk every document instead of reusing unchanged content_hash entries",
    )
    build.add_argument(
        "--max-chars",
        type=int,
        default=tc.DEFAULT_MAX_CHARS,
        help=(
            "chunk size bound for on-the-fly chunking, a positive integer "
            f"(default {tc.DEFAULT_MAX_CHARS})"
        ),
    )

    query = sub.add_parser("query", help="print ranked hits as a JSON array on stdout")
    query.add_argument("question", help="query text")
    source = query.add_mutually_exclusive_group(required=True)
    source.add_argument("--index", metavar="PATH", help="persisted index to query")
    source.add_argument(
        "--ephemeral",
        nargs="+",
        metavar="PATH",
        help=(
            "ad-hoc transcript files or directories; the index is built "
            "in memory and nothing is written"
        ),
    )
    query.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"maximum hits to return, a positive integer (default {DEFAULT_TOP_K})",
    )
    query.add_argument(
        "--max-chars",
        type=int,
        default=tc.DEFAULT_MAX_CHARS,
        help=(
            "chunk size bound for ephemeral on-the-fly chunking, a positive "
            f"integer (default {tc.DEFAULT_MAX_CHARS})"
        ),
    )
    return parser


def _cmd_build(args: argparse.Namespace) -> int:
    if args.max_chars <= 0:
        raise UsageError("--max-chars must be a positive integer")
    index_path = Path(args.index).expanduser()
    existing = None
    if not args.rebuild and index_path.is_file():
        existing = load_index(index_path)
    documents = gather_documents(args.paths)
    if not documents:
        raise RagCorpusEmpty(
            f"no valid transcripts found under: {', '.join(args.paths)}",
            "pass transcript.json files or directories containing them",
        )
    old_hashes = set()
    if existing is not None:
        old_hashes = {
            source.get("content_hash")
            for source in existing.get("doc_sources", [])
            if isinstance(source, dict)
        }
    index = build_index(documents, existing=existing, max_chars=args.max_chars)
    new_hashes = {source["content_hash"] for source in index["doc_sources"]}
    for dropped in sorted(h for h in old_hashes - new_hashes if isinstance(h, str)):
        _warn(f"dropped from index (no longer discovered): {dropped}")
    tc.atomic_write_json(index, index_path)
    n_chunks = sum(len(chunk_list) for chunk_list in index["chunks"].values())
    print(
        f"indexed {len(index['doc_sources'])} document(s), {n_chunks} chunk(s) -> {index_path}",
        file=sys.stderr,
    )
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    if args.top_k <= 0:
        raise UsageError("--top-k must be a positive integer")
    if args.max_chars <= 0:
        raise UsageError("--max-chars must be a positive integer")
    if not args.question.strip():
        raise UsageError("question must be a non-empty string")
    if args.ephemeral is not None:
        documents = gather_documents(args.ephemeral)
        if not documents:
            raise RagCorpusEmpty(
                "no valid transcripts among the --ephemeral paths",
                "pass transcript.json files or directories containing them",
            )
        index = build_index(documents, max_chars=args.max_chars)
    else:
        index = load_index(Path(args.index).expanduser())
    results = query_index(index, args.question, args.top_k)
    sys.stdout.write(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "build":
            return _cmd_build(args)
        if args.command == "query":
            return _cmd_query(args)
        raise UsageError(f"unknown command: {args.command}")
    except IngestError as e:
        print(e.to_text(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

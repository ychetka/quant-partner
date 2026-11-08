#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Offline transcript chunker for context-window-limited consumers (issue #61).

This script is NOT part of the MCP server: it splits a schema-validated
``transcript.json`` (produced by ``tools/media_ingest.py``) into
segment-aligned chunks sized to fit a partner's context window, so
callers can feed oversized transcripts to ``media-partner`` piece by
piece with intact ``t0``/``t1`` citation anchors.

Contract:

- ``chunk_transcript(transcript, max_chars)`` validates the transcript
  through ``media_ingest.validate()`` (schema violations surface as
  ``ChunkingError``; an unsupported ``schema_version`` keeps its
  dedicated ``UnsupportedSchemaVersion`` code) and accumulates WHOLE
  segments until the next one would exceed ``max_chars``. A single
  oversized segment becomes its own chunk and is never split mid-text.
  When segments carry non-null ``speaker`` values the flusher prefers
  cutting at the last speaker change inside the current group
  (best-effort; the ``max_chars`` bound always wins).
- Every chunk is ``{chunk_id, t0, t1, segment_ids, speaker_set, text,
  char_count}``: ``chunk_id`` is a 0-based sequence, ``segment_ids``
  are the 0-based indices into ``transcript["segments"]`` the chunk
  covers, ``speaker_set`` the sorted distinct non-null speakers, and
  ``t0``/``t1`` the min/max of the covered segments' non-null
  timestamps (``None`` when every covered segment is untimed).
- ``write_chunks`` optionally persists the result as a
  ``<name>.chunks.json`` sidecar ``{schema_version: 1, source_hash,
  generator_version, generated_at, chunks}`` written atomically
  (``tempfile.mkstemp`` + ``os.replace``, mode 0600). The sidecar is a
  pure cache of deterministic output: ``transcript_rag.py`` reuses it
  only when ``source_hash`` matches the transcript's ``content_hash``
  and re-chunks otherwise.
- The CLI ``transcript_chunker.py <transcript.json> [--max-chars N]
  [--out PATH | --stdout]`` writes the sidecar beside the transcript
  by default or prints the same document with ``--stdout``. All
  user-facing failures are named errors on stderr
  (``ERROR <code>: <message>``, exit 1), never tracebacks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# media_ingest is a sibling tool, not an installed package: import it
# from this script's directory so the schema contract and the
# named-error pattern are shared instead of duplicated.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_ingest as mi
from media_ingest import (
    IngestError,
    OutputWriteError,
    TranscriptSchemaError,
    UnsupportedSchemaVersion,
    UsageError,
    validate,
)

SCHEMA_VERSION = 1
GENERATOR_VERSION = "transcript_chunker/0.1.0"

DEFAULT_MAX_CHARS = 8000
_SIDECAR_SUFFIX = ".chunks.json"
_CHUNK_KEYS = {
    "chunk_id",
    "t0",
    "t1",
    "segment_ids",
    "speaker_set",
    "text",
    "char_count",
}


class ChunkingError(IngestError):
    """The source transcript cannot be chunked: invalid or empty."""

    code = "chunking_error"


# --- chunking ----------------------------------------------------------------


def _speaker_changed(prev: dict[str, Any], current: dict[str, Any]) -> bool:
    """True when ``current`` opens a new non-null speaker turn."""
    speaker = current.get("speaker")
    return isinstance(speaker, str) and speaker != prev.get("speaker")


def _group_chars(indices: list[int], segments: list[dict[str, Any]]) -> int:
    """Joined-text length of a group: text lengths plus one space per join."""
    return sum(len(segments[i]["text"]) for i in indices) + max(0, len(indices) - 1)


def _emit_chunk(
    chunks: list[dict[str, Any]], indices: list[int], segments: list[dict[str, Any]]
) -> None:
    covered = [segments[i] for i in indices]
    text = " ".join(segment["text"] for segment in covered)
    t0s = [segment["t0"] for segment in covered if segment["t0"] is not None]
    t1s = [segment["t1"] for segment in covered if segment["t1"] is not None]
    chunks.append(
        {
            "chunk_id": len(chunks),
            "t0": min(t0s) if t0s else None,
            "t1": max(t1s) if t1s else None,
            "segment_ids": list(indices),
            "speaker_set": sorted(
                {segment["speaker"] for segment in covered if isinstance(segment["speaker"], str)}
            ),
            "text": text,
            "char_count": len(text),
        }
    )


def chunk_transcript(
    transcript: dict[str, Any], max_chars: int = DEFAULT_MAX_CHARS
) -> list[dict[str, Any]]:
    """Split a validated transcript into segment-aligned, size-bounded chunks.

    Segments are accumulated whole: a chunk's joined text never exceeds
    ``max_chars`` unless it holds exactly one oversized segment, which
    forms its own chunk and is never split mid-text. When the bound
    forces a flush and the accumulated group contains a speaker change
    (a segment whose non-null ``speaker`` differs from its
    predecessor's), the chunk is cut at the LAST such boundary instead
    of the group's tail, so a speaker turn is not split needlessly.
    The preference is best-effort: the ``max_chars`` bound always wins.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise UsageError("max_chars must be a positive integer")
    try:
        validate(transcript)
    except UnsupportedSchemaVersion:
        raise  # keep the dedicated version-negotiation code
    except TranscriptSchemaError as e:
        raise ChunkingError(f"source transcript failed validation: {e.message}", e.hint) from e
    segments = transcript["segments"]
    if not segments:
        raise ChunkingError("transcript has no segments to chunk")

    chunks: list[dict[str, Any]] = []
    group: list[int] = []
    group_chars = 0
    # Positions inside ``group`` where a new speaker turn starts; each
    # entry >= 1 marks the boundary before ``group[pos]``.
    speaker_breaks: list[int] = []
    for index, segment in enumerate(segments):
        seg_len = len(segment["text"])
        while group and group_chars + seg_len + 1 > max_chars:
            cut = speaker_breaks[-1] if speaker_breaks else len(group)
            _emit_chunk(chunks, group[:cut], segments)
            group = group[cut:]
            group_chars = _group_chars(group, segments)
            speaker_breaks = [pos - cut for pos in speaker_breaks if pos > cut]
        if group and _speaker_changed(segments[group[-1]], segment):
            speaker_breaks.append(len(group))
        group.append(index)
        group_chars += seg_len + (1 if len(group) > 1 else 0)
    if group:
        _emit_chunk(chunks, group, segments)
    return chunks


# --- chunks sidecar ----------------------------------------------------------


def sidecar_path(transcript_path: Path | str) -> Path:
    """Default chunks sidecar: ``<stem>.chunks.json`` beside the transcript."""
    path = Path(transcript_path)
    name = path.name
    stem = name[: -len(".json")] if name.endswith(".json") else name
    return path.with_name(stem + _SIDECAR_SUFFIX)


def sidecar_document(transcript: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """The persistable ``*.chunks.json`` document for a transcript."""
    return {
        "schema_version": SCHEMA_VERSION,
        "source_hash": transcript["content_hash"],
        "generator_version": GENERATOR_VERSION,
        "generated_at": mi._now_iso_z(),
        "chunks": chunks,
    }


def atomic_write_json(doc: dict[str, Any], path: Path) -> None:
    """Atomically write ``doc`` as pretty JSON to ``path`` with mode 0600.

    The temp file comes from ``tempfile.mkstemp`` in the target's own
    directory, so concurrent writers get unique names and ``os.replace``
    stays on one filesystem; any failure unlinks the temp file and
    leaves the previous contents byte-identical.
    """
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1  # the file object owns the descriptor now
            # mkstemp already creates the file 0600; force it anyway.
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(tmp_name, path)
    except OSError as e:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise OutputWriteError(
            f"cannot write JSON artifact: {e}",
            "create the parent directory or pass a writable output path",
        ) from e


def write_chunks(
    transcript: dict[str, Any],
    path: Path | str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Path:
    """Chunk ``transcript`` and atomically write the sidecar; returns path."""
    chunks = chunk_transcript(transcript, max_chars)
    out = Path(path).expanduser()
    atomic_write_json(sidecar_document(transcript, chunks), out)
    return out


def _valid_sidecar_chunk(chunk: Any) -> bool:
    if not isinstance(chunk, dict) or not _CHUNK_KEYS.issubset(chunk):
        return False
    if not isinstance(chunk["text"], str) or not chunk["text"]:
        return False
    if isinstance(chunk["chunk_id"], bool) or not isinstance(chunk["chunk_id"], int):
        return False
    for key in ("t0", "t1"):
        value = chunk[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return False
    if not isinstance(chunk["segment_ids"], list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in chunk["segment_ids"]
    ):
        return False
    return isinstance(chunk["speaker_set"], list) and all(
        isinstance(s, str) for s in chunk["speaker_set"]
    )


def read_chunk_sidecar(
    path: Path | str, *, source_hash: str | None = None
) -> dict[str, Any] | None:
    """Return a parsed ``*.chunks.json`` document, or ``None`` on any problem.

    Consumers treat a missing, unreadable, corrupt, wrong-version or
    hash-mismatched sidecar as absent and re-chunk the transcript
    instead - a stale sidecar must never silently shadow the source.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if source_hash is not None and data.get("source_hash") != source_hash:
        return None
    chunks = data.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return None
    if not all(_valid_sidecar_chunk(chunk) for chunk in chunks):
        return None
    return data


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcript_chunker",
        description=(
            "Split a transcript.json into segment-aligned, size-bounded "
            "chunks for context-window-limited consumers."
        ),
    )
    parser.add_argument("transcript", help="schema-validated transcript.json to chunk")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=(f"maximum characters per chunk, a positive integer (default {DEFAULT_MAX_CHARS})"),
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument(
        "--out",
        metavar="PATH",
        help=("chunks sidecar path (default <name>.chunks.json beside the transcript)"),
    )
    out.add_argument(
        "--stdout",
        action="store_true",
        help="print the chunks document to stdout instead of writing a sidecar",
    )
    return parser


def _load_transcript(path: Path) -> dict[str, Any]:
    try:
        return json.loads(mi._read_utf8_text(path))
    except (OSError, json.JSONDecodeError) as e:
        raise UsageError(f"cannot read transcript JSON: {e}") from e


def _cmd(args: argparse.Namespace) -> int:
    if args.max_chars <= 0:
        raise UsageError("--max-chars must be a positive integer")
    transcript_path = Path(args.transcript).expanduser()
    transcript = _load_transcript(transcript_path)
    doc = sidecar_document(transcript, chunk_transcript(transcript, args.max_chars))
    if args.stdout:
        sys.stdout.write(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0
    out = Path(args.out).expanduser() if args.out else sidecar_path(transcript_path)
    atomic_write_json(doc, out)
    print(f"wrote {len(doc['chunks'])} chunk(s) -> {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _cmd(args)
    except IngestError as e:
        print(e.to_text(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

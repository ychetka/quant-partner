#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Offline media transcript ingest for the media-partner pipeline (issue #37).

This script is NOT part of the MCP server: it runs offline and turns a
media reference into a schema-validated ``transcript.json`` artifact
that callers pass to ``media-partner`` through the ordinary ``context``
or ``files`` arguments of the ask tools.

Stage 1-6 scope implemented here:

- the ``transcript.json`` schema contract and ``validate()``;
- ``content_hash`` - a deterministic sha256 over canonical JSON of the
  normalized segments (stable key order, fixed-precision floats), so
  whitespace and key-ordering differences never miss the cache;
- the caller-transcript normalizer (plain text, SRT, VTT, loose JSON);
- ``TranscriptCache`` - a bounded store keyed ONLY by content_hash
  (never by URI or path), mirroring the atomic-write / TTL / max-files
  pattern of ``partners/review/cache.py`` ``ArtifactCache``. That cache
  cannot be reused directly because it validates review artifacts;
- the ``from-file`` ingest path: canonical transcription is LOCAL ASR
  through a whisper.cpp binary (``AsrUnavailable`` names the missing
  binary), while ``--transcript-file`` keeps the caller-supplied path;
- the subprocess contract for every external binary: argv lists only
  (never ``shell=True``), explicit timeouts and binary-missing as a
  first-class named error;
- ``source.ref`` privacy: the artifact may reach a remote partner, so
  it stores only the media basename (or a caller-supplied ``--ref``
  label) - the resolved local path is used for ASR access only and is
  never serialized;
- cache-hit provenance: a hit re-emits the stored transcript with the
  current input's ``source.ref`` and a ``cache_hit: true`` flag, so the
  artifact never carries another file's provenance;
- ``from-youtube``: the default mode pulls platform captions through
  ``yt-dlp`` (``--write-sub --write-auto-sub --skip-download``) and
  normalizes the downloaded VTT/SRT file - ``yt_dlp_captions`` method
  with the lower ``platform_captions`` trust; ``--asr`` instead
  downloads the audio and runs the same local whisper.cpp path for
  ``local_asr``/``self_transcribed``. A missing binary is named
  ``YtDlpUnavailable`` and a video without usable captions raises
  ``NoCaptionsAvailable``;
- ``from-web-page``: ``yt-dlp`` downloads the page's media through its
  generic extractor into a temp dir and the local whisper.cpp path
  transcribes it - ``web_page``/``local_asr``. A page that yields no
  downloadable media fails loudly as ``NoMediaFound``.

Every remote fetch and transcription runs inside a
``tempfile.TemporaryDirectory`` workspace, so downloaded media and
intermediate files never outlive the command. ``source.ref`` for the
URL-based subcommands stores the URL itself (already remote-safe), or a
caller-supplied ``--ref`` label.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
GENERATOR_VERSION = "media_ingest/0.2.0"

SOURCE_KINDS = ("youtube", "web_page", "local_file")
TRANSCRIPTION_METHODS = ("local_asr", "yt_dlp_captions", "caller_supplied")
_METHOD_TRUST = {
    "local_asr": "self_transcribed",
    "caller_supplied": "self_transcribed",
    "yt_dlp_captions": "platform_captions",
}
TRANSCRIPTION_TRUSTS = tuple(sorted(set(_METHOD_TRUST.values())))

DEFAULT_CACHE_DIR = ".cache/media-ingest"
DEFAULT_CACHE_TTL_SECONDS = 6 * 3600
DEFAULT_CACHE_MAX_FILES = 256
DEFAULT_BINARY_TIMEOUT_SECONDS = 600
DEFAULT_WHISPER_BINARY = "whisper-cli"
ENV_WHISPER_BINARY = "WHISPER_CPP_BINARY"
ENV_WHISPER_MODEL = "WHISPER_CPP_MODEL"
DEFAULT_YTDLP_BINARY = "yt-dlp"
ENV_YTDLP_BINARY = "YT_DLP_BINARY"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CACHE_STEM_RE = re.compile(r"[0-9a-fA-F]{64}")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_GENERATOR_RE = re.compile(r"^media_ingest/\d+\.\d+\.\d+$")
_CUE_LINE_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
    r"\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
_TIMESTAMP_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,6})$")

_TRANSCRIPT_KEYS = {
    "source",
    "content_hash",
    "segments",
    "annotations",
    "generated_at",
    "generator_version",
    "schema_version",
    "cache_hit",
}
# ``cache_hit`` is present only on artifacts re-emitted from the cache;
# a freshly generated transcript must validate without it.
_TRANSCRIPT_OPTIONAL = {"cache_hit"}
_SOURCE_KEYS = {"kind", "ref", "transcription_method", "transcription_trust"}
_SEGMENT_REQUIRED = {"t0", "t1", "text"}
_SEGMENT_KEYS = _SEGMENT_REQUIRED | {"speaker"}


class IngestError(Exception):
    """Human-readable ingest failure; ``code`` is the stable machine code."""

    code = "ingest_error"

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_text(self) -> str:
        text = f"ERROR {self.code}: {self.message}"
        if self.hint:
            text += f" Hint: {self.hint}"
        return text


class UsageError(IngestError):
    """Bad CLI usage or missing local inputs (files, model paths)."""

    code = "invalid_usage"


class OutputWriteError(IngestError):
    """The transcript artifact could not be written to the --output path."""

    code = "output_write_failed"


class TranscriptSchemaError(IngestError):
    """A transcript.json object failed the schema contract."""

    code = "transcript_schema_invalid"


class UnsupportedSchemaVersion(TranscriptSchemaError):
    """The transcript declares a schema_version this tool cannot read."""

    code = "unsupported_schema_version"


class TranscriptFormatError(IngestError):
    """Caller-supplied transcript text matched no supported format."""

    code = "transcript_format_invalid"


class ExternalToolError(IngestError):
    """An external binary is missing, timed out or exited non-zero."""

    code = "external_tool_error"

    def __init__(self, binary: str, message: str, hint: str = "") -> None:
        super().__init__(message, hint)
        self.binary = binary


class AsrUnavailable(ExternalToolError):
    """The local ASR binary required for transcription is unavailable."""

    code = "asr_unavailable"

    def __init__(self, binary: str, hint: str = "") -> None:
        super().__init__(
            binary,
            f"local ASR binary not available: {binary}",
            hint or "install whisper.cpp so the binary is on PATH, or pass --transcript-file",
        )


class YtDlpUnavailable(ExternalToolError):
    """The yt-dlp binary required for remote ingest is unavailable."""

    code = "yt_dlp_unavailable"

    def __init__(self, binary: str, hint: str = "") -> None:
        super().__init__(
            binary,
            f"yt-dlp binary not available: {binary}",
            hint or "install yt-dlp so the binary is on PATH",
        )


class NoCaptionsAvailable(ExternalToolError):
    """yt-dlp ran cleanly but produced no usable caption file."""

    code = "no_captions_available"

    def __init__(
        self,
        binary: str = "yt-dlp",
        message: str = "no captions available for this source",
        hint: str = "pass --transcript-file or retry with --asr",
    ) -> None:
        super().__init__(binary, message, hint)


class NoMediaFound(ExternalToolError):
    """yt-dlp ran cleanly but the page yielded no downloadable media."""

    code = "no_media_found"

    def __init__(
        self,
        binary: str = "yt-dlp",
        message: str = "no downloadable media found at this URL",
        hint: str = "check that the page hosts a video yt-dlp can extract",
    ) -> None:
        super().__init__(binary, message, hint)


# --- transcript.json schema contract -------------------------------------


def _check_segment_time(value: Any, index: int, name: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise TranscriptSchemaError(
            f"segments[{index}].{name} must be null or a non-negative finite number"
        )


def validate(obj: Any) -> None:
    """Raise ``TranscriptSchemaError`` unless ``obj`` is a valid transcript.

    The contract is strict: exact key sets at every level, an integer
    ``schema_version`` that must equal ``SCHEMA_VERSION`` (anything else
    raises ``UnsupportedSchemaVersion``), a ``sha256:<64 hex>``
    ``content_hash``, segment times that are null or non-negative
    finite numbers with ``t1 >= t0`` when both are set, and a
    ``source.transcription_trust`` consistent with the declared
    ``transcription_method``. The only optional key is ``cache_hit``,
    which must be exactly ``true`` when present.
    """
    if not isinstance(obj, dict):
        raise TranscriptSchemaError("transcript must be a JSON object")
    unknown = sorted(set(obj) - _TRANSCRIPT_KEYS)
    if unknown:
        raise TranscriptSchemaError(f"unknown top-level keys: {', '.join(unknown)}")
    version = obj.get("schema_version")
    if version is None:
        raise TranscriptSchemaError("schema_version is required")
    if isinstance(version, bool) or not isinstance(version, int):
        raise TranscriptSchemaError("schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"unsupported schema_version: {version}",
            f"this tool reads schema_version {SCHEMA_VERSION} transcripts only",
        )
    for key in sorted(_TRANSCRIPT_KEYS - {"schema_version"} - _TRANSCRIPT_OPTIONAL):
        if key not in obj:
            raise TranscriptSchemaError(f"missing required key: {key}")
    # ``cache_hit`` exists only to mark a cache reuse; ``false`` would be
    # noise, so the strict contract accepts ``true`` only.
    if "cache_hit" in obj and obj["cache_hit"] is not True:
        raise TranscriptSchemaError("cache_hit, when present, must be exactly true")

    source = obj["source"]
    if not isinstance(source, dict):
        raise TranscriptSchemaError("source must be an object")
    unknown = sorted(set(source) - _SOURCE_KEYS)
    if unknown:
        raise TranscriptSchemaError(f"unknown source keys: {', '.join(unknown)}")
    for key in sorted(_SOURCE_KEYS):
        if key not in source:
            raise TranscriptSchemaError(f"missing source key: {key}")
    if source["kind"] not in SOURCE_KINDS:
        raise TranscriptSchemaError(f"source.kind must be one of {list(SOURCE_KINDS)}")
    if not isinstance(source["ref"], str) or not source["ref"].strip():
        raise TranscriptSchemaError("source.ref must be a non-empty string")
    method = source["transcription_method"]
    if method not in TRANSCRIPTION_METHODS:
        raise TranscriptSchemaError(
            f"transcription_method must be one of {list(TRANSCRIPTION_METHODS)}"
        )
    trust = source["transcription_trust"]
    if trust not in TRANSCRIPTION_TRUSTS:
        raise TranscriptSchemaError(
            f"transcription_trust must be one of {list(TRANSCRIPTION_TRUSTS)}"
        )
    if _METHOD_TRUST[method] != trust:
        raise TranscriptSchemaError(
            f"transcription_trust {trust!r} is inconsistent with transcription_method {method!r}"
        )

    content_hash_value = obj["content_hash"]
    if not isinstance(content_hash_value, str) or _HASH_RE.fullmatch(content_hash_value) is None:
        raise TranscriptSchemaError('content_hash must look like "sha256:<64 lowercase hex>"')

    segments = obj["segments"]
    if not isinstance(segments, list):
        raise TranscriptSchemaError("segments must be an array")
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise TranscriptSchemaError(f"segments[{index}] must be an object")
        unknown = sorted(set(segment) - _SEGMENT_KEYS)
        if unknown:
            raise TranscriptSchemaError(
                f"unknown segment keys at index {index}: {', '.join(unknown)}"
            )
        for key in sorted(_SEGMENT_REQUIRED):
            if key not in segment:
                raise TranscriptSchemaError(f"segments[{index}] missing key: {key}")
        _check_segment_time(segment["t0"], index, "t0")
        _check_segment_time(segment["t1"], index, "t1")
        if (
            segment["t0"] is not None
            and segment["t1"] is not None
            and segment["t1"] < segment["t0"]
        ):
            raise TranscriptSchemaError(f"segments[{index}] has t1 before t0")
        speaker = segment.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise TranscriptSchemaError(f"segments[{index}].speaker must be null or a string")
        if not isinstance(segment["text"], str) or not segment["text"].strip():
            raise TranscriptSchemaError(f"segments[{index}].text must be a non-empty string")

    annotations = obj["annotations"]
    if not isinstance(annotations, list):
        raise TranscriptSchemaError("annotations must be an array")
    for index, annotation in enumerate(annotations):
        if not isinstance(annotation, dict):
            raise TranscriptSchemaError(f"annotations[{index}] must be an object (reserved schema)")

    generated_at = obj["generated_at"]
    if not isinstance(generated_at, str) or _ISO_Z_RE.fullmatch(generated_at) is None:
        raise TranscriptSchemaError("generated_at must be an ISO-8601 UTC timestamp ending in Z")
    generator = obj["generator_version"]
    if not isinstance(generator, str) or _GENERATOR_RE.fullmatch(generator) is None:
        raise TranscriptSchemaError('generator_version must look like "media_ingest/<semver>"')


# --- canonical form and content_hash --------------------------------------


def _normalize_text(text: Any) -> str:
    """Collapse all whitespace runs so cosmetic differences never fork the hash."""
    return " ".join(str(text).split())


def _canonical_time(value: Any) -> float | None:
    """Fixed-precision float canonicalization for hashing and emitting."""
    if value is None:
        return None
    return round(float(value), 6)


def _canonical_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    canonical = []
    for segment in segments:
        speaker = segment.get("speaker")
        canonical.append(
            {
                "t0": _canonical_time(segment.get("t0")),
                "t1": _canonical_time(segment.get("t1")),
                "speaker": speaker if isinstance(speaker, str) else None,
                "text": _normalize_text(segment.get("text", "")),
            }
        )
    return canonical


def content_hash(segments: list[dict[str, Any]]) -> str:
    """Deterministic ``sha256:`` digest over canonical JSON of the segments.

    Key order is stable (``sort_keys``), floats are fixed-precision
    (``_canonical_time``) and text is whitespace-normalized, so the hash
    is identical across ingest runs that differ only cosmetically. The
    source URI/path deliberately never participates.
    """
    blob = json.dumps(
        _canonical_segments(segments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_transcript(
    *,
    kind: str,
    ref: str,
    method: str,
    segments: list[dict[str, Any]],
    generated_at: str | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a transcript.json object and validate it before returning."""
    if method not in _METHOD_TRUST:
        raise UsageError(f"unknown transcription_method: {method}")
    transcript = {
        "source": {
            "kind": kind,
            "ref": ref,
            "transcription_method": method,
            "transcription_trust": _METHOD_TRUST[method],
        },
        "content_hash": content_hash(segments),
        "segments": _canonical_segments(segments),
        "annotations": list(annotations) if annotations else [],
        "generated_at": generated_at or _now_iso_z(),
        "generator_version": GENERATOR_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    # Fail-closed on write: a generator bug must surface here, never in
    # a downstream reader.
    validate(transcript)
    return transcript


# --- caller-transcript normalizer ------------------------------------------


def _fraction_ms(frac: str) -> int:
    # "5" means 500 ms (SRT/VTT fraction of a second), "050" means 50 ms.
    return int(frac.ljust(3, "0")[:3])


def _parse_timestamp(text: str) -> float:
    match = _TIMESTAMP_RE.fullmatch(text.strip())
    if match is None:
        raise TranscriptFormatError(f"cannot parse timestamp: {text.strip()!r}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    return round(hours * 3600 + minutes * 60 + seconds + _fraction_ms(match.group(4)) / 1000, 3)


def _coerce_time(value: Any, index: int, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TranscriptFormatError(f"JSON segment {index}: {name} must be a number or null")
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    if isinstance(value, str):
        try:
            return round(float(value), 3)
        except ValueError:
            return _parse_timestamp(value)
    raise TranscriptFormatError(f"JSON segment {index}: {name} must be a number or null")


def _cue_times(match: re.Match[str]) -> tuple[float, float]:
    def part(base: int) -> float:
        hours = int(match.group(base) or 0)
        minutes = int(match.group(base + 1))
        seconds = int(match.group(base + 2))
        millis = _fraction_ms(match.group(base + 3))
        return round(hours * 3600 + minutes * 60 + seconds + millis / 1000, 3)

    return part(1), part(5)


def _segments_from_json(text: str) -> list[dict[str, Any]] | None:
    """Parse a loose JSON array of {start, end, text} items; None when not JSON."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        raise TranscriptFormatError("JSON transcript must be an array of segment objects")
    segments = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise TranscriptFormatError(f"JSON segment {index} must be an object")
        body = item.get("text")
        if not isinstance(body, str) or not body.strip():
            raise TranscriptFormatError(f"JSON segment {index} needs a non-empty 'text'")
        speaker = item.get("speaker")
        segments.append(
            {
                "t0": _coerce_time(item.get("start", item.get("t0")), index, "start"),
                "t1": _coerce_time(item.get("end", item.get("t1")), index, "end"),
                "speaker": speaker if isinstance(speaker, str) else None,
                "text": _normalize_text(body),
            }
        )
    return segments


def _segments_from_cues(text: str) -> list[dict[str, Any]]:
    """Parse SRT/VTT cue blocks; blocks without a ``-->`` line are skipped."""
    segments = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip().splitlines()
        cue_index = next(
            (i for i, line in enumerate(lines) if _CUE_LINE_RE.search(line)),
            None,
        )
        if cue_index is None:
            continue
        match = _CUE_LINE_RE.search(lines[cue_index])
        if match is None:  # unreachable: cue_index came from the same regex
            continue
        t0, t1 = _cue_times(match)
        body = _normalize_text(" ".join(lines[cue_index + 1 :]))
        if not body:
            continue
        segments.append({"t0": t0, "t1": t1, "speaker": None, "text": body})
    return segments


def _segments_from_plain(text: str) -> list[dict[str, Any]]:
    return [
        {"t0": None, "t1": None, "speaker": None, "text": _normalize_text(line)}
        for line in text.splitlines()
        if line.strip()
    ]


def normalize_transcript(raw: str) -> list[dict[str, Any]]:
    """Normalize caller-supplied transcript text into canonical segments.

    Supported inputs: a loose JSON array of ``{start, end, text}``
    objects, SRT, VTT and plain text (one segment per non-empty line,
    ``t0``/``t1`` null). Raises ``TranscriptFormatError`` on empty or
    unparseable input.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TranscriptFormatError("transcript text is empty")
    text = raw.strip()
    if text.startswith("["):
        parsed = _segments_from_json(text)
        if parsed is not None:
            return parsed
    if text.upper().startswith("WEBVTT") or _CUE_LINE_RE.search(text):
        segments = _segments_from_cues(text)
        if not segments:
            raise TranscriptFormatError("input looks like SRT/VTT but no usable cues were found")
        return segments
    return _segments_from_plain(text)


# --- bounded transcript cache ----------------------------------------------


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class TranscriptCache:
    """Bounded filesystem store for transcript.json keyed by content_hash.

    Mirrors the ``ArtifactCache`` pattern from ``partners/review/cache.py``
    - atomic temp-file + ``os.replace`` writes, mode 0600, TTL expiry
    and a max-files bound - but keyed by transcript ``content_hash``
    only, never by source URI or path, and storing transcripts only
    (never Q&A answers). Corrupt, expired or hash-mismatched files
    count as misses and are removed.
    """

    def __init__(
        self,
        cache_dir: str | Path,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        max_files: int = DEFAULT_CACHE_MAX_FILES,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = int(ttl_seconds)
        self.max_files = int(max_files)

    def _path(self, content_hash_value: str) -> Path:
        if (
            not isinstance(content_hash_value, str)
            or _HASH_RE.fullmatch(content_hash_value) is None
        ):
            raise ValueError('cache key must be a "sha256:<64 hex>" content_hash')
        return self.cache_dir / f"{content_hash_value.split(':', 1)[1]}.json"

    def _expired(self, mtime: float, now: float) -> bool:
        return self.ttl_seconds >= 0 and now - mtime > self.ttl_seconds

    def get(self, content_hash_value: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Return the stored transcript for a content_hash, or ``None`` on a miss."""
        path = self._path(content_hash_value)
        try:
            stat = path.stat()
        except OSError:
            return None
        moment = time.time() if now is None else now
        if self._expired(stat.st_mtime, moment):
            _unlink(path)
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _unlink(path)
            return None
        try:
            validate(data)
        except TranscriptSchemaError:
            _unlink(path)
            return None
        if data["content_hash"] != content_hash_value:
            _unlink(path)
            return None
        # Defense in depth: the declared content_hash is untrusted - the
        # digest is recomputed over the stored segments and a mismatch
        # (tampered or mis-linked entry) counts as a miss.
        if content_hash(data["segments"]) != content_hash_value:
            _unlink(path)
            return None
        return data

    def put(self, transcript: dict[str, Any]) -> str:
        """Validate then atomically store a transcript; return its content_hash."""
        validate(transcript)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(transcript["content_hash"])
        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(transcript, handle, ensure_ascii=False, sort_keys=True)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            _unlink(Path(tmp_name))
            raise
        self.prune()
        return transcript["content_hash"]

    def prune(self, *, now: float | None = None) -> int:
        """Drop expired files, then oldest files beyond ``max_files``."""
        if not self.cache_dir.is_dir():
            return 0
        moment = time.time() if now is None else now
        removed = 0
        entries: list[tuple[float, Path]] = []
        for path in self.cache_dir.glob("*.json"):
            if _CACHE_STEM_RE.fullmatch(path.stem) is None:
                # Only manage own entries: stray ``.tmp-*.json``
                # leftovers and unrelated files stay untouched.
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if self._expired(stat.st_mtime, moment):
                _unlink(path)
                removed += 1
                continue
            entries.append((stat.st_mtime, path))
        if len(entries) > self.max_files:
            entries.sort()
            for _, path in entries[: len(entries) - self.max_files]:
                _unlink(path)
                removed += 1
        return removed


# --- subprocess contract for external binaries ------------------------------


def run_external(
    argv: list[str],
    *,
    timeout_seconds: int,
    missing_error: ExternalToolError | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external binary under the ingest subprocess contract.

    Contract: ``argv`` is always a list (never a shell string, never
    ``shell=True``), an explicit timeout always applies and a missing
    binary raises a first-class named error - never a raw
    ``FileNotFoundError``. ``missing_error`` lets each caller choose
    the domain error (``AsrUnavailable`` for the ASR path).
    """
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise UsageError("external argv must be a non-empty list of strings")
    binary = argv[0]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as e:
        raise missing_error or ExternalToolError(binary, f"binary not found: {binary}") from e
    except subprocess.TimeoutExpired as e:
        raise ExternalToolError(binary, f"{binary} timed out after {timeout_seconds}s") from e
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])[:300]
        raise ExternalToolError(binary, f"{binary} exited with code {proc.returncode}", tail)
    return proc


# --- local ASR via whisper.cpp ----------------------------------------------


def _millis(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value) / 1000.0, 3)


def _segments_from_whisper(payload: Any, binary: str) -> list[dict[str, Any]]:
    """Map whisper.cpp ``-oj`` JSON output to canonical segments."""
    items = payload.get("transcription") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ExternalToolError(binary, f"{binary} output has no 'transcription' array")
    segments = []
    for item in items:
        if not isinstance(item, dict):
            continue
        t0 = t1 = None
        offsets = item.get("offsets")
        if isinstance(offsets, dict):
            t0 = _millis(offsets.get("from"))
            t1 = _millis(offsets.get("to"))
        if t0 is None or t1 is None:
            stamps = item.get("timestamps")
            if isinstance(stamps, dict):
                try:
                    t0 = _parse_timestamp(str(stamps.get("from")))
                    t1 = _parse_timestamp(str(stamps.get("to")))
                except (TranscriptFormatError, TypeError):
                    t0 = t1 = None
        text = _normalize_text(item.get("text", ""))
        if not text:
            continue
        segments.append({"t0": t0, "t1": t1, "speaker": None, "text": text})
    if not segments:
        raise ExternalToolError(binary, f"{binary} returned an empty transcription")
    return segments


def transcribe_local(
    media_path: Path,
    *,
    binary: str,
    model_path: str,
    timeout_seconds: int,
    work_dir: Path,
) -> list[dict[str, Any]]:
    """Run the whisper.cpp CLI on a local media file; return canonical segments.

    ``AsrUnavailable`` names the binary when it cannot be executed, so
    the ASR path fails closed instead of silently degrading.
    """
    if shutil.which(binary) is None:
        raise AsrUnavailable(binary)
    out_base = Path(work_dir) / "transcript"
    argv = [
        binary,
        "-m",
        str(model_path),
        "-f",
        str(media_path),
        "-oj",
        "-of",
        str(out_base),
    ]
    run_external(argv, timeout_seconds=timeout_seconds, missing_error=AsrUnavailable(binary))
    out_json = out_base.with_suffix(".json")
    try:
        payload = json.loads(out_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ExternalToolError(
            binary, f"{binary} produced no readable JSON transcript", str(e)[:200]
        ) from e
    return _segments_from_whisper(payload, binary)


# --- remote ingest via yt-dlp -------------------------------------------------


def download_media(
    url: str,
    *,
    binary: str,
    timeout_seconds: int,
    work_dir: Path,
) -> Path:
    """Download the media behind ``url`` as WAV into ``work_dir``.

    ``yt-dlp`` resolves the extractor (YouTube or a generic page) and
    ``-x --audio-format wav`` leaves a deterministic ``media.wav`` that
    the stock whisper.cpp CLI accepts; the audio conversion itself is
    delegated to ffmpeg by yt-dlp. A clean exit without an output file
    means the extractor found no media and is named ``NoMediaFound`` -
    ingest never degrades to an empty transcript.
    """
    if shutil.which(binary) is None:
        raise YtDlpUnavailable(binary)
    media_base = Path(work_dir) / "media"
    argv = [
        binary,
        "--no-playlist",
        "-x",
        "--audio-format",
        "wav",
        "-o",
        str(media_base),
        url,
    ]
    run_external(argv, timeout_seconds=timeout_seconds, missing_error=YtDlpUnavailable(binary))
    media = media_base.with_suffix(".wav")
    if not media.is_file():
        raise NoMediaFound(binary)
    return media


def download_captions(
    url: str,
    *,
    binary: str,
    timeout_seconds: int,
    work_dir: Path,
    sub_langs: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch platform captions for ``url`` and normalize them to segments.

    Runs ``yt-dlp --write-sub --write-auto-sub --skip-download`` so both
    manual and automatic caption tracks are candidates; VTT files are
    preferred over SRT and candidates are tried in sorted filename
    order, the first file that normalizes wins. When nothing usable is
    produced - no caption files at all, or files the normalizer cannot
    read - ``NoCaptionsAvailable`` names the failure so the caller can
    fall back to ``--transcript-file`` or ``--asr``.
    """
    if shutil.which(binary) is None:
        raise YtDlpUnavailable(binary)
    base = Path(work_dir) / "captions"
    argv = [
        binary,
        "--no-playlist",
        "--write-sub",
        "--write-auto-sub",
        "--skip-download",
        "--sub-format",
        "vtt/srt",
    ]
    if sub_langs:
        argv += ["--sub-langs", sub_langs]
    argv += ["-o", str(base), url]
    run_external(argv, timeout_seconds=timeout_seconds, missing_error=YtDlpUnavailable(binary))
    candidates = sorted(base.parent.glob(f"{base.name}.*.vtt"))
    candidates += sorted(base.parent.glob(f"{base.name}.*.srt"))
    for path in candidates:
        try:
            segments = normalize_transcript(path.read_text(encoding="utf-8"))
        except (OSError, TranscriptFormatError):
            continue
        if segments:
            return segments
    raise NoCaptionsAvailable(binary)


# --- CLI --------------------------------------------------------------------


def _add_output_and_cache_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o",
        "--output",
        help="write transcript.json to this path (mode 0600); default stdout",
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"transcript cache directory (default {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=DEFAULT_CACHE_TTL_SECONDS,
        help=f"cache TTL in seconds, >= 0 (default {DEFAULT_CACHE_TTL_SECONDS})",
    )
    parser.add_argument(
        "--cache-max-files",
        type=int,
        default=DEFAULT_CACHE_MAX_FILES,
        help=f"max cached transcripts, a positive integer (default {DEFAULT_CACHE_MAX_FILES})",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="skip the transcript cache for this run",
    )


def _add_whisper_args(parser: argparse.ArgumentParser, model_help: str) -> None:
    parser.add_argument(
        "--whisper-binary",
        default=os.environ.get(ENV_WHISPER_BINARY, DEFAULT_WHISPER_BINARY),
        help=f"whisper.cpp CLI binary (env {ENV_WHISPER_BINARY}; default whisper-cli)",
    )
    parser.add_argument(
        "--whisper-model",
        default=os.environ.get(ENV_WHISPER_MODEL, ""),
        help=model_help,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_BINARY_TIMEOUT_SECONDS,
        help=(
            "external binary timeout, a positive integer "
            f"(default {DEFAULT_BINARY_TIMEOUT_SECONDS})"
        ),
    )


def _add_ytdlp_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ytdlp-binary",
        default=os.environ.get(ENV_YTDLP_BINARY, DEFAULT_YTDLP_BINARY),
        help=f"yt-dlp binary (env {ENV_YTDLP_BINARY}; default yt-dlp)",
    )


def _add_ref_arg(parser: argparse.ArgumentParser, default_label: str) -> None:
    parser.add_argument(
        "--ref",
        help=f"label stored as source.ref; defaults to {default_label}",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media_ingest",
        description=(
            "Offline media transcript ingest: emits schema-validated "
            "transcript.json artifacts for media-partner."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    local = sub.add_parser(
        "from-file",
        help="ingest a local media file via whisper.cpp ASR or a caller transcript",
    )
    local.add_argument("media_path", help="local media file to transcribe")
    local.add_argument(
        "--transcript-file",
        help=(
            "caller-supplied transcript (plain text, SRT, VTT or loose JSON); "
            "skips ASR and marks transcription_method caller_supplied"
        ),
    )
    _add_ref_arg(
        local,
        "the media basename (the resolved local path is never written "
        "into the artifact - it may reach a remote partner)",
    )
    _add_whisper_args(
        local,
        f"whisper.cpp model file (env {ENV_WHISPER_MODEL}); required without --transcript-file",
    )
    _add_output_and_cache_args(local)

    youtube = sub.add_parser(
        "from-youtube",
        help="ingest a YouTube URL via platform captions or local ASR",
    )
    youtube.add_argument("url", help="YouTube video URL")
    youtube.add_argument(
        "--asr",
        action="store_true",
        help=(
            "skip captions: download the audio with yt-dlp and transcribe it "
            "with local whisper.cpp ASR (self_transcribed trust)"
        ),
    )
    youtube.add_argument(
        "--transcript-file",
        help=(
            "caller-supplied transcript (plain text, SRT, VTT or loose JSON); "
            "skips yt-dlp and marks transcription_method caller_supplied"
        ),
    )
    youtube.add_argument(
        "--sub-langs",
        help=(
            "yt-dlp caption language filter for the captions mode "
            "(for example 'en.*'); default all available caption tracks"
        ),
    )
    _add_ref_arg(youtube, "the URL")
    _add_ytdlp_args(youtube)
    _add_whisper_args(
        youtube,
        f"whisper.cpp model file (env {ENV_WHISPER_MODEL}); required with --asr",
    )
    _add_output_and_cache_args(youtube)

    web = sub.add_parser(
        "from-web-page",
        help="ingest media hosted on a generic web page via yt-dlp plus local ASR",
    )
    web.add_argument("url", help="web page URL hosting a video yt-dlp can extract")
    _add_ref_arg(web, "the URL")
    _add_ytdlp_args(web)
    _add_whisper_args(
        web,
        f"whisper.cpp model file (env {ENV_WHISPER_MODEL}); required - ASR is the only mode",
    )
    _add_output_and_cache_args(web)

    check = sub.add_parser(
        "validate",
        help="validate a transcript.json file and print its content_hash",
    )
    check.add_argument("transcript_path")
    return parser


def _emit(transcript: dict[str, Any], output: str | None) -> None:
    text = json.dumps(transcript, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output:
        # Transcripts may hold confidential meeting content: 0600.
        try:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as e:
            raise OutputWriteError(
                f"cannot write transcript artifact: {e}",
                "create the parent directory or pass a writable --output path",
            ) from e
    else:
        sys.stdout.write(text)


def _store_or_reuse(cache: TranscriptCache | None, transcript: dict[str, Any]) -> dict[str, Any]:
    """Reuse a cached transcript for the same content_hash, else store ours.

    On a hit the stored ``generated_at`` is kept - it is the transcript's
    true creation time - but ``source.ref`` is rewritten to the current
    input's label and a top-level ``cache_hit: true`` flag marks the
    reuse, so the emitted artifact never carries another file's
    provenance. The stored entry itself is left untouched.
    """
    if cache is None:
        return transcript
    cached = cache.get(transcript["content_hash"])
    if cached is not None:
        emitted = dict(cached)
        emitted["source"] = {**cached["source"], "ref": transcript["source"]["ref"]}
        emitted["cache_hit"] = True
        return emitted
    cache.put(transcript)
    return transcript


def _validate_numeric_args(args: argparse.Namespace) -> None:
    """Shared numeric/cache validation for every ingest subcommand."""
    if args.timeout_seconds <= 0:
        raise UsageError("--timeout-seconds must be a positive integer")
    if args.cache_ttl_seconds < 0:
        raise UsageError("--cache-ttl-seconds must be >= 0")
    if args.cache_max_files < 1:
        raise UsageError("--cache-max-files must be a positive integer")


def _resolve_ref_label(args: argparse.Namespace) -> str:
    ref = args.ref.strip() if args.ref is not None else ""
    if args.ref is not None and not ref:
        raise UsageError("--ref must be a non-empty label")
    return ref


def _require_whisper_model(args: argparse.Namespace, alternative: str) -> str:
    if not args.whisper_model:
        raise UsageError(
            "local ASR needs a whisper.cpp model file",
            f"pass --whisper-model or set {ENV_WHISPER_MODEL}{alternative}",
        )
    if not Path(args.whisper_model).expanduser().is_file():
        raise UsageError(f"whisper model file not found: {args.whisper_model}")
    return args.whisper_model


def _resolve_url(args: argparse.Namespace) -> str:
    url = args.url.strip()
    if not url:
        raise UsageError("url must be a non-empty string")
    return url


def _read_caller_transcript(transcript_file: str) -> list[dict[str, Any]]:
    transcript_path = Path(transcript_file).expanduser()
    try:
        raw = transcript_path.read_text(encoding="utf-8")
    except OSError as e:
        raise UsageError(f"cannot read transcript file: {e}") from e
    return normalize_transcript(raw)


def _resolve_cache(args: argparse.Namespace) -> TranscriptCache | None:
    if args.no_cache:
        return None
    return TranscriptCache(
        args.cache_dir,
        ttl_seconds=args.cache_ttl_seconds,
        max_files=args.cache_max_files,
    )


def _transcribe_via_download(
    url: str, args: argparse.Namespace, model_path: str
) -> list[dict[str, Any]]:
    """yt-dlp media download plus local whisper.cpp ASR in one workspace."""
    with tempfile.TemporaryDirectory(prefix="media-ingest-") as work_dir:
        work = Path(work_dir)
        media = download_media(
            url,
            binary=args.ytdlp_binary,
            timeout_seconds=args.timeout_seconds,
            work_dir=work,
        )
        return transcribe_local(
            media,
            binary=args.whisper_binary,
            model_path=model_path,
            timeout_seconds=args.timeout_seconds,
            work_dir=work,
        )


def _cmd_from_file(args: argparse.Namespace) -> int:
    _validate_numeric_args(args)
    ref = _resolve_ref_label(args)
    media = Path(args.media_path).expanduser()
    if not media.is_file():
        raise UsageError(f"media file not found: {media}")
    if args.transcript_file:
        segments = _read_caller_transcript(args.transcript_file)
        method = "caller_supplied"
    else:
        model = _require_whisper_model(args, ", or provide --transcript-file")
        with tempfile.TemporaryDirectory(prefix="media-ingest-") as work_dir:
            segments = transcribe_local(
                media,
                binary=args.whisper_binary,
                model_path=model,
                timeout_seconds=args.timeout_seconds,
                work_dir=Path(work_dir),
            )
        method = "local_asr"
    # The artifact may reach a remote partner: source.ref carries only
    # the media basename (or the --ref label); the resolved path stays
    # local and is used solely for ASR access above.
    transcript = build_transcript(
        kind="local_file", ref=ref or media.name, method=method, segments=segments
    )
    _emit(_store_or_reuse(_resolve_cache(args), transcript), args.output)
    return 0


def _cmd_from_youtube(args: argparse.Namespace) -> int:
    _validate_numeric_args(args)
    ref = _resolve_ref_label(args)
    if args.asr and args.transcript_file:
        raise UsageError("--asr and --transcript-file are mutually exclusive")
    url = _resolve_url(args)
    if args.transcript_file:
        segments = _read_caller_transcript(args.transcript_file)
        method = "caller_supplied"
    elif args.asr:
        model = _require_whisper_model(
            args, ", provide --transcript-file, or drop --asr to use captions"
        )
        segments = _transcribe_via_download(url, args, model)
        method = "local_asr"
    else:
        with tempfile.TemporaryDirectory(prefix="media-ingest-") as work_dir:
            segments = download_captions(
                url,
                binary=args.ytdlp_binary,
                timeout_seconds=args.timeout_seconds,
                work_dir=Path(work_dir),
                sub_langs=args.sub_langs,
            )
        method = "yt_dlp_captions"
    # source.ref is the URL itself (already remote-safe) or the --ref
    # label; no local path ever reaches the artifact.
    transcript = build_transcript(kind="youtube", ref=ref or url, method=method, segments=segments)
    _emit(_store_or_reuse(_resolve_cache(args), transcript), args.output)
    return 0


def _cmd_from_web_page(args: argparse.Namespace) -> int:
    _validate_numeric_args(args)
    ref = _resolve_ref_label(args)
    url = _resolve_url(args)
    model = _require_whisper_model(args, "")
    segments = _transcribe_via_download(url, args, model)
    transcript = build_transcript(
        kind="web_page", ref=ref or url, method="local_asr", segments=segments
    )
    _emit(_store_or_reuse(_resolve_cache(args), transcript), args.output)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.transcript_path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise UsageError(f"cannot read transcript JSON: {e}") from e
    validate(data)
    print(f"ok {data['content_hash']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "from-file":
            return _cmd_from_file(args)
        if args.command == "from-youtube":
            return _cmd_from_youtube(args)
        if args.command == "from-web-page":
            return _cmd_from_web_page(args)
        if args.command == "validate":
            return _cmd_validate(args)
        raise UsageError(f"unknown command: {args.command}")
    except IngestError as e:
        print(e.to_text(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

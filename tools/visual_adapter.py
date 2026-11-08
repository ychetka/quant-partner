#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Offline visual-annotation adapter for transcript.json (issue #58).

This script is NOT part of the MCP server: it is an opt-in offline
companion that enriches an existing ``transcript.json`` (produced by
``tools/media_ingest.py``) with visual observations extracted by a
Gemini model through the Files API. The transcript is updated in place
- new entries land in ``annotations[]`` - so the partner contract and
the ``content_hash`` (computed over segments only) stay intact.

Contract:

- ``--transcript PATH`` points at a schema-validated transcript.json;
  ``--media PATH`` overrides the media location, otherwise
  ``source.ref`` is resolved when ``source.kind == "local_file"`` (the
  ref is tried as given and relative to the transcript's directory).
  URLs are never re-downloaded: a ``youtube``/``web_page`` source
  without ``--media`` fails as ``media_not_found``.
- Authentication is the ``GEMINI_API_KEY`` environment variable only.
  The key is sent exclusively as the ``x-goog-api-key`` header - never
  in a URL, never in argv (there is no subprocess use at all) - and
  every error path redacts it if a message ever echoes it back.
- The media file is uploaded through the resumable-upload protocol of
  the Gemini Files API, polled until ``ACTIVE`` and referenced by
  ``file_data.file_uri`` in one ``generateContent`` call asking for a
  JSON array of ``{t0, t1, kind, text}`` observations. The upload body
  streams straight from disk (an open file object plus an explicit
  ``Content-Length``) and its timeout scales with size at a ~512 KiB/s
  floor. The uploaded file is ephemeral scratch: it is deleted
  best-effort after use and its URI is never persisted.
- Extracted annotations are cached under ``--cache-dir`` (default
  ``~/.cache/quant-partner/visual/``) keyed by sha256 of canonical JSON
  ``{content_hash, model, PROMPT_VERSION}``; the cache is consulted
  before media resolution, so a hit skips the paid API call entirely
  and needs neither an API key nor the media file. ``--force``
  bypasses the read.
- Merge is deduplicated on ``(kind, t0, t1, model or source)`` and
  written back atomically: a ``tempfile.mkstemp`` file in the
  transcript's own directory + ``os.replace`` + mode 0600, with
  ``validate()`` run before the replace - on any failure the original
  file stays byte-identical.
- ``--chunk-seconds N`` bounds the observation window asked for in the
  prompt (stdlib-only: the script cannot slice media itself, so the
  value steers the model's granularity rather than splitting input).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

# media_ingest is a sibling tool, not an installed package: import it
# from this script's directory so the schema contract, the named-error
# pattern and validate() are shared instead of duplicated.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_ingest as mi
from media_ingest import IngestError, OutputWriteError, UsageError, validate

SCHEMA_SOURCE = "gemini-files-api"

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD_ROOT = "https://generativelanguage.googleapis.com/upload/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
ENV_CREDENTIAL = "GEMINI_API_KEY"

PROMPT_VERSION = 1
VISUAL_KINDS = ("visual", "object")

DEFAULT_CACHE_DIR = "~/.cache/quant-partner/visual"
DEFAULT_CHUNK_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 120
POLL_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3.0

_CACHE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_FENCE_RE = re.compile(r"```[A-Za-z]*\s*(.*?)```", re.DOTALL)


class VisualAdapterError(IngestError):
    """Base class for the adapter's named failures."""

    code = "visual_adapter_error"


class AuthError(VisualAdapterError):
    """GEMINI_API_KEY is unset, or the API rejected the credential."""

    code = "auth_error"


class MediaNotFound(VisualAdapterError):
    """No local media file could be resolved for the transcript."""

    code = "media_not_found"


class UploadError(VisualAdapterError):
    """The resumable upload to the Files API failed."""

    code = "upload_failed"


class FileApiError(VisualAdapterError):
    """A Files API operation (poll, generate, delete) failed."""

    code = "file_api_error"


class QuotaError(VisualAdapterError):
    """The API reported rate limiting or quota exhaustion."""

    code = "quota_exceeded"


class ModelOutputError(VisualAdapterError):
    """The model response contained no usable JSON annotation array."""

    code = "model_output_invalid"


class _HttpFailure(Exception):
    """Internal transport failure; mapped to a named error at the edge."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _redact(text: Any, secrets: list[str]) -> str:
    """Strip secrets from any string that could reach logs or stderr."""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "<redacted>")
    return out


def _extract_error_message(raw: bytes, status: int, secrets: list[str]) -> str:
    """Best-effort error message from a Gemini error body, redacted."""
    body = raw.decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return _redact(body[:600] or f"HTTP {status}", secrets)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return _redact(error.get("message") or body[:600], secrets)
    return _redact(body[:600] or f"HTTP {status}", secrets)


class _KeyStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward ``x-goog-api-key`` to a different host.

    urllib re-sends a request's custom headers on redirect; if the API
    ever answers with a redirect to a foreign host, the credential
    must be dropped from the followed request.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        original_host = urllib.parse.urlparse(req.full_url).hostname
        target_host = urllib.parse.urlparse(redirected.full_url).hostname
        if target_host != original_host:
            redirected.remove_header("X-goog-api-key")
        return redirected


# This script owns its process: installing the opener at import time
# makes every urlopen call - including tests patching urlopen - share
# the redirect policy above.
urllib.request.install_opener(urllib.request.build_opener(_KeyStrippingRedirectHandler))


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | BinaryIO | None = None,
    timeout: int = HTTP_TIMEOUT_SECONDS,
    secrets: list[str] | tuple[str, ...] = (),
) -> tuple[Any, bytes]:
    """One urllib request returning ``(response_headers, raw_body)``.

    The API key travels only inside ``headers`` (``x-goog-api-key``);
    the URL is always key-free and every failure channel - HTTP error
    bodies, socket reasons - is redacted before it becomes a message.
    ``body`` may be an open binary file object: http.client then reads
    it in blocks, so large uploads never materialize in memory - the
    caller must set an explicit ``Content-Length`` header for that.
    """
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.headers, response.read()
    except urllib.error.HTTPError as e:
        raise _HttpFailure(e.code, _extract_error_message(e.read(), e.code, secrets)) from e
    except (urllib.error.URLError, OSError) as e:
        reason = _redact(getattr(e, "reason", e), secrets)
        raise _HttpFailure(0, f"network error: {reason}") from e


def _map_http_failure(exc: _HttpFailure, make_error: Any) -> VisualAdapterError:
    """Map a transport failure to the stage's named error.

    Authentication and quota failures keep their identity across every
    stage; anything else is reported as the stage-specific error the
    caller provided (``UploadError``, ``FileApiError``, ...).
    """
    lower = exc.message.lower()
    if exc.status in (401, 403):
        return AuthError(f"API rejected the credential: {exc.message}")
    if (
        exc.status == 429
        or "quota" in lower
        or "rate limit" in lower
        or (exc.status >= 500 and "resource_exhausted" in lower)
    ):
        return QuotaError(f"API quota or rate limit hit: {exc.message}")
    return make_error(exc.message)


# --- Gemini Files API -------------------------------------------------------


def _mime_type(media_path: Path) -> str:
    guessed, _ = mimetypes.guess_type(media_path.name)
    return guessed or "application/octet-stream"


def _upload_file(
    media_path: Path,
    mime_type: str,
    api_key: str,
    timeout: int,
    secrets: list[str],
) -> dict[str, Any]:
    """Resumable-upload ``media_path``; return the file resource dict.

    The media bytes are never buffered in memory: the PUT body is an
    open binary file object with an explicit ``Content-Length``, so
    http.client streams it in blocks. The upload timeout scales with
    the file size at a ~512 KiB/s floor so large media gets the time
    it needs.
    """
    try:
        size_bytes = media_path.stat().st_size
    except OSError as e:
        raise MediaNotFound(f"cannot read media file: {e}") from e
    upload_timeout = max(timeout, math.ceil(size_bytes / (512 * 1024)))
    start_headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size_bytes),
        "X-Goog-Upload-Header-Content-Type": mime_type,
    }
    start_body = json.dumps({"file": {"display_name": media_path.name}}).encode("utf-8")
    try:
        headers, _ = _http_request(
            "POST",
            f"{UPLOAD_ROOT}/files",
            headers=start_headers,
            body=start_body,
            timeout=timeout,
            secrets=secrets,
        )
    except _HttpFailure as e:
        raise _map_http_failure(
            e, lambda m: UploadError(f"upload session start failed: {m}")
        ) from e
    upload_url = headers.get("X-Goog-Upload-URL") or headers.get("x-goog-upload-url")
    if not upload_url:
        raise UploadError("upload session start returned no X-Goog-Upload-URL header")
    put_headers = {
        "x-goog-api-key": api_key,
        "Content-Type": mime_type,
        "Content-Length": str(size_bytes),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    try:
        with media_path.open("rb") as media_file:
            _, raw = _http_request(
                "PUT",
                upload_url,
                headers=put_headers,
                body=media_file,
                timeout=upload_timeout,
                secrets=secrets,
            )
    except _HttpFailure as e:
        raise _map_http_failure(e, lambda m: UploadError(f"media upload failed: {m}")) from e
    except OSError as e:
        raise MediaNotFound(f"cannot read media file: {e}") from e
    try:
        resource = json.loads(raw or b"{}")
    except json.JSONDecodeError as e:
        raise UploadError("media upload returned malformed JSON") from e
    file_info = resource.get("file") if isinstance(resource, dict) else None
    if not isinstance(file_info, dict) or not file_info.get("name"):
        raise UploadError("media upload returned no usable file resource")
    return file_info


def _file_url(file_info: dict[str, Any]) -> str:
    uri = file_info.get("uri")
    if isinstance(uri, str) and uri.startswith("http"):
        return uri
    return f"{API_ROOT}/{file_info.get('name', '')}"


def _poll_until_active(
    file_info: dict[str, Any],
    api_key: str,
    timeout: int,
    poll_timeout: int,
    poll_interval: float,
    secrets: list[str],
) -> dict[str, Any]:
    """Poll the file resource until state ACTIVE; videos need it."""
    name = file_info.get("name", "?")
    deadline = time.monotonic() + poll_timeout
    state = file_info.get("state")
    while state != "ACTIVE":
        if state == "FAILED":
            raise FileApiError(f"file {name} reached state FAILED during processing")
        if time.monotonic() >= deadline:
            raise FileApiError(
                f"file {name} not ACTIVE after {poll_timeout}s (last state {state!r})"
            )
        time.sleep(poll_interval)
        url = f"{_file_url(file_info)}?fields=name,state,uri"
        try:
            _, raw = _http_request(
                "GET",
                url,
                headers={"x-goog-api-key": api_key},
                timeout=timeout,
                secrets=secrets,
            )
            file_info = json.loads(raw or b"{}")
        except _HttpFailure as e:
            raise _map_http_failure(
                e, lambda m: FileApiError(f"file status poll failed: {m}")
            ) from e
        except json.JSONDecodeError as e:
            raise FileApiError("file status poll returned malformed JSON") from e
        if not isinstance(file_info, dict):
            raise FileApiError("file status poll returned a non-object payload")
        state = file_info.get("state")
    return file_info


def _delete_file(file_info: dict[str, Any], api_key: str, timeout: int, secrets: list[str]) -> None:
    """Best-effort DELETE of the ephemeral upload; failures are ignored."""
    try:
        _http_request(
            "DELETE",
            _file_url(file_info),
            headers={"x-goog-api-key": api_key},
            timeout=timeout,
            secrets=secrets,
        )
    except Exception:
        pass


def _visual_prompt(chunk_seconds: int) -> str:
    # PROMPT_VERSION tracks this text: bump it whenever the instructions
    # change so cached annotations from an older prompt are never reused.
    return (
        "Inspect the attached media file and return a JSON array of "
        "timestamped visual observations. Each array element is an object "
        "with exactly these fields:\n"
        '- "t0": observation start as an absolute media timestamp in '
        "seconds (number)\n"
        '- "t1": observation end as an absolute media timestamp in '
        f"seconds (number, >= t0); keep each window at roughly "
        f"{chunk_seconds} seconds or less\n"
        '- "kind": "visual" for scenes, slides, on-screen text and UI '
        'state; "object" for recurring tracked objects or people '
        "appearing or disappearing\n"
        '- "text": one concise English sentence describing the '
        "observation\n"
        "Return the JSON array only - no prose, no markdown code fences. "
        "If the media has no visual track (pure audio), return an empty "
        "array []."
    )


def _generate_text(
    file_uri: str,
    mime_type: str,
    model: str,
    api_key: str,
    chunk_seconds: int,
    timeout: int,
    secrets: list[str],
) -> str:
    """One generateContent call over the uploaded file; returns text."""
    payload = {
        "contents": [
            {
                "parts": [
                    {"file_data": {"file_uri": file_uri, "mime_type": mime_type}},
                    {"text": _visual_prompt(chunk_seconds)},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"},
    }
    url = f"{API_ROOT}/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        _, raw = _http_request(
            "POST",
            url,
            headers=headers,
            body=json.dumps(payload).encode("utf-8"),
            timeout=timeout,
            secrets=secrets,
        )
    except _HttpFailure as e:
        raise _map_http_failure(e, lambda m: FileApiError(f"generateContent failed: {m}")) from e
    try:
        out = json.loads(raw or b"{}")
    except json.JSONDecodeError as e:
        raise ModelOutputError("generateContent returned malformed JSON") from e
    candidates = out.get("candidates") if isinstance(out, dict) else None
    parts = []
    if isinstance(candidates, list) and candidates:
        content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
        if isinstance(content, dict) and isinstance(content.get("parts"), list):
            parts = content["parts"]
    # Join only plain text parts: thinking models can interleave
    # ``thought``/``thoughtSignature`` parts whose text is the model's
    # internal reasoning, not the answer.
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("text"), str)
        and part["text"]
        and "thought" not in part
        and "thoughtSignature" not in part
    ).strip()
    if not text:
        raise ModelOutputError("model returned an empty response")
    return text


def analyze_media(
    media_path: Path,
    *,
    model: str,
    api_key: str,
    chunk_seconds: int,
    http_timeout: int = HTTP_TIMEOUT_SECONDS,
    poll_timeout: int = POLL_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    """Upload, poll and analyze ``media_path``; return valid observations.

    The uploaded file is scratch: whatever happens, a best-effort DELETE
    runs afterwards and no file URI is returned to callers or persisted.
    """
    secrets = [api_key]
    mime_type = _mime_type(media_path)
    file_info = _upload_file(media_path, mime_type, api_key, http_timeout, secrets)
    try:
        file_info = _poll_until_active(
            file_info, api_key, http_timeout, poll_timeout, poll_interval, secrets
        )
        text = _generate_text(
            _file_url(file_info),
            mime_type,
            model,
            api_key,
            chunk_seconds,
            http_timeout,
            secrets,
        )
        return parse_observations(text)
    finally:
        _delete_file(file_info, api_key, http_timeout, secrets)


# --- defensive output parsing ------------------------------------------------


def _valid_observation(item: Any) -> dict[str, Any] | None:
    """Coerce one model entry to a canonical observation, else ``None``."""
    if not isinstance(item, dict):
        return None
    t0, t1 = item.get("t0"), item.get("t1")
    if isinstance(t0, bool) or isinstance(t1, bool):
        return None
    if not isinstance(t0, (int, float)) or not isinstance(t1, (int, float)):
        return None
    if not math.isfinite(t0) or not math.isfinite(t1) or t0 < 0 or t1 < t0:
        return None
    if item.get("kind") not in VISUAL_KINDS:
        return None
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"t0": t0, "t1": t1, "kind": item["kind"], "text": text.strip()}


def _extract_json_array(text: str) -> list[Any] | None:
    """Find a parseable JSON array in model output, or ``None``.

    Order of attempts: code-fence bodies first (models love wrapping
    JSON despite instructions), then the whole text, then the widest
    ``[...]`` slice as a last resort for prose-padded answers.
    """
    candidates = [match.group(1).strip() for match in _CODE_FENCE_RE.finditer(text)]
    candidates.append(text.strip())
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    return None


def parse_observations(text: str) -> list[dict[str, Any]]:
    """Extract validated observations from raw model text.

    Malformed entries are dropped with a stderr warning - model garbage
    must never crash the run - but output with no parseable JSON array
    at all raises ``ModelOutputError``. A valid empty array is fine:
    the media may simply have no visual track.
    """
    data = _extract_json_array(text)
    if data is None:
        raise ModelOutputError("model output contained no usable JSON array")
    observations = []
    dropped = 0
    for item in data:
        observation = _valid_observation(item)
        if observation is None:
            dropped += 1
        else:
            observations.append(observation)
    if dropped:
        print(
            f"warning: dropped {dropped} malformed annotation "
            f"{'entry' if dropped == 1 else 'entries'} from model output",
            file=sys.stderr,
        )
    return observations


# --- visual annotation cache --------------------------------------------------


def visual_cache_key(content_hash_value: str, model: str) -> str:
    """sha256 over canonical JSON of {content_hash, model, PROMPT_VERSION}."""
    blob = json.dumps(
        {"content_hash": content_hash_value, "model": model, "prompt_version": PROMPT_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class VisualCache:
    """Filesystem store for extracted observation lists.

    Keyed by :func:`visual_cache_key`, so re-running the adapter on the
    same transcript with the same model reuses annotations and skips
    the paid API call. Only the raw ``{t0, t1, kind, text}`` entries
    are stored - never file URIs, media paths or transcript content -
    so nothing in the cache can resurrect an expired upload or leak a
    local path. Corrupt or malformed entries count as misses.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir).expanduser()

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or _CACHE_KEY_RE.fullmatch(key) is None:
            raise ValueError("cache key must be a 64-char lowercase hex digest")
        return self.cache_dir / f"{key}.json"

    def get(self, key: str) -> list[dict[str, Any]] | None:
        """Return stored observations, or ``None`` on any miss."""
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        entries = []
        for item in data:
            observation = _valid_observation(item)
            if observation is None:
                # A malformed stored entry is tampering: full miss.
                return None
            entries.append(observation)
        return entries

    def put(self, key: str, entries: list[dict[str, Any]]) -> str:
        """Atomically store observations; returns the key."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, ensure_ascii=False, sort_keys=True)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return key


# --- transcript merge and write-back ------------------------------------------


def _resolve_media_path(
    transcript: dict[str, Any], media_arg: str | None, transcript_path: Path
) -> Path:
    """Resolve the media file: ``--media`` wins, else a local ``source.ref``.

    ``source.ref`` is a basename or label, so it is tried both as given
    (relative to the working directory) and relative to the transcript
    file's directory - the common case of transcript.json sitting next
    to its media. URL sources are never re-downloaded.
    """
    if media_arg is not None:
        path = Path(media_arg).expanduser()
        if not path.is_file():
            raise MediaNotFound(f"--media file not found: {media_arg}")
        return path
    source = transcript["source"]
    if source["kind"] != "local_file":
        raise MediaNotFound(
            f"transcript source.kind is {source['kind']!r}; no local media to analyze",
            "URLs are never re-downloaded: pass --media PATH with a local copy of the media",
        )
    ref = source["ref"]
    candidates = [Path(ref).expanduser()]
    if not candidates[0].is_absolute():
        candidates.append(transcript_path.parent / ref)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise MediaNotFound(
        f"source.ref does not resolve to a local file: {ref}",
        "pass --media PATH with a local copy of the media",
    )


def _annotation_key(annotation: dict[str, Any]) -> tuple[Any, ...]:
    """Dedupe key: ``(kind, t0, t1, model or source or "")``.

    Model-less annotations still carry a producer in ``source``: keying
    on that keeps two different producers from collapsing onto one
    ``None`` key while letting a producer's own entries dedupe.
    """
    return (
        annotation.get("kind"),
        annotation.get("t0"),
        annotation.get("t1"),
        annotation.get("model") or annotation.get("source") or "",
    )


def _merge_annotations(
    transcript: dict[str, Any], entries: list[dict[str, Any]], model: str
) -> dict[str, Any]:
    """Append adapter-stamped annotations, deduped on the annotation key.

    Existing entries win collisions; entries produced by another model
    or another adapter source never collide with this run's output.
    """
    merged = list(transcript["annotations"])
    seen = {_annotation_key(a) for a in merged if isinstance(a, dict)}
    stamp = mi._now_iso_z()
    for entry in entries:
        annotation = {
            "kind": entry["kind"],
            "t0": entry["t0"],
            "t1": entry["t1"],
            "text": entry["text"],
            "source": SCHEMA_SOURCE,
            "model": model,
            "generated_at": stamp,
        }
        key = _annotation_key(annotation)
        if key in seen:
            continue
        seen.add(key)
        merged.append(annotation)
    doc = dict(transcript)
    doc["annotations"] = merged
    # Fail-closed: the merged document is validated before a single byte
    # reaches the filesystem, so a broken merge can never corrupt the
    # caller's transcript.json.
    validate(doc)
    return doc


def _write_back(transcript_path: Path, doc: dict[str, Any]) -> None:
    """Atomically replace transcript.json with the merged document (0600).

    The temp file comes from ``tempfile.mkstemp`` in the transcript's
    own directory: concurrent runs get unique names instead of racing
    on a fixed ``*.tmp`` path, and ``os.replace`` stays on one
    filesystem. Any failure unlinks the temp file and leaves the
    original untouched.
    """
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=transcript_path.parent,
            prefix=f".{transcript_path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1  # the file object owns the descriptor now
            # mkstemp already creates the file 0600; force it anyway.
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(tmp_name, transcript_path)
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
            f"cannot write transcript back: {e}",
            "the original transcript.json was left untouched",
        ) from e


# --- CLI ----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="visual_adapter",
        description=(
            "Enrich a transcript.json with visual annotations extracted "
            "through the Gemini Files API (opt-in, paid API call)."
        ),
    )
    parser.add_argument(
        "--transcript",
        required=True,
        help="schema-validated transcript.json to annotate in place",
    )
    parser.add_argument(
        "--media",
        help=(
            "local media file to analyze; defaults to source.ref when "
            "source.kind is local_file (URLs are never re-downloaded)"
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model name (default {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"visual-annotation cache directory (default {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run the model even when cached annotations exist",
    )
    parser.add_argument(
        "--chunk-seconds",
        type=int,
        default=DEFAULT_CHUNK_SECONDS,
        help=(
            "target observation window in seconds asked for in the prompt "
            f"(default {DEFAULT_CHUNK_SECONDS})"
        ),
    )
    return parser


def _run(args: argparse.Namespace, key: str) -> int:
    if args.chunk_seconds <= 0:
        raise UsageError("--chunk-seconds must be a positive integer")
    if not args.model.strip():
        raise UsageError("--model must be a non-empty model name")
    transcript_path = Path(args.transcript).expanduser()
    try:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise UsageError(f"cannot read transcript: {e}") from e
    except UnicodeDecodeError as e:
        raise UsageError(f"transcript is not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise UsageError(f"transcript is not valid JSON: {e}") from e
    validate(transcript)
    cache = VisualCache(args.cache_dir)
    cache_key = visual_cache_key(transcript["content_hash"], args.model)
    # The cache is consulted before media resolution: a hit needs
    # neither the media file nor the API credential.
    entries = None if args.force else cache.get(cache_key)
    if entries is not None:
        print(
            f"visual cache hit: {len(entries)} stored observation(s); skipping the API call",
            file=sys.stderr,
        )
    else:
        media_path = _resolve_media_path(transcript, args.media, transcript_path)
        if not key:
            raise AuthError(
                f"{ENV_CREDENTIAL} is not set",
                "export a Gemini API key; it is sent only as the x-goog-api-key header",
            )
        entries = analyze_media(
            media_path,
            model=args.model,
            api_key=key,
            chunk_seconds=args.chunk_seconds,
        )
        try:
            cache.put(cache_key, entries)
        except OSError as e:
            # A cache write failure must not lose the paid-for result.
            print(f"warning: visual cache write failed: {e}", file=sys.stderr)
    doc = _merge_annotations(transcript, entries, args.model)
    _write_back(transcript_path, doc)
    print(
        f"annotations[] now holds {len(doc['annotations'])} "
        f"{'entry' if len(doc['annotations']) == 1 else 'entries'} "
        f"({len(entries)} from this model)",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # The key is read exactly once and only ever travels inside the
    # x-goog-api-key header of API requests.
    key = os.environ.get(ENV_CREDENTIAL, "").strip()
    try:
        return _run(args, key)
    except IngestError as e:
        # Defense in depth: even a bug that leaks the key into a message
        # cannot reach stderr unredacted.
        print(_redact(e.to_text(), [key]), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Idempotency key derivation and the bounded atomic artifact cache.

The key hashes canonical inputs only; ``review_id`` and timestamps never
participate, so identical review inputs map to one stored artifact.
Only ``status=ok`` artifacts are written, atomically via a temp file and
``os.replace``. Corrupt or expired files count as misses and are removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import schema

_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
DEFAULT_TTL_SECONDS = 86400
DEFAULT_MAX_FILES = 256


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def idempotency_key(
    *,
    prompt_version: str,
    partner_id: str,
    backend: str,
    model_alias: str,
    model_requested: str,
    strict_model: bool,
    head_sha: str,
    base_sha: str,
    diff: str,
    context: str = "",
    file_contents: Iterable[str] = (),
    skill: str = "",
    skill_version: str = "",
    schema_version: str = schema.SCHEMA_VERSION,
) -> str:
    """Canonical sha256 idempotency key from spec section 8.

    ``review_id`` and timestamps are deliberately absent: they are
    correlation metadata, not review inputs. The resolved ``skill``
    name and its manifest ``skill_version`` participate so identical
    inputs reviewed under different skill packs - or different versions
    of one pack - never share a cache entry.
    """
    # A length-prefixed canonical JSON object removes the concatenation
    # ambiguity between ``context`` and ``file_contents`` boundaries.
    context_blob = json.dumps(
        {
            "context": str(context),
            "file_contents": [str(item) for item in file_contents],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    parts = [
        schema_version,
        prompt_version,
        partner_id,
        backend,
        model_alias,
        model_requested,
        "true" if strict_model else "false",
        f"{skill}@{skill_version}",
        str(head_sha).lower(),
        str(base_sha).lower(),
        _sha256_hex(diff),
        _sha256_hex(context_blob),
    ]
    return _sha256_hex("|".join(parts))


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class ArtifactCache:
    """Filesystem cache storing one validated ``ok`` artifact per key."""

    def __init__(
        self,
        cache_dir: str | Path,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = int(ttl_seconds)
        self.max_files = int(max_files)

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
            raise ValueError("cache key must be a 64-hex idempotency key")
        return self.cache_dir / f"{key}.json"

    def _expired(self, mtime: float, now: float) -> bool:
        return self.ttl_seconds >= 0 and now - mtime > self.ttl_seconds

    def get(self, key: str, *, now: float | None = None) -> dict[str, Any] | None:
        """Return the stored artifact marked ``cached=True``, or ``None`` on a miss."""
        path = self._path(key)
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
        if not isinstance(data, dict) or schema.validate(data) or data.get("status") != "ok":
            _unlink(path)
            return None
        return {**data, "cached": True}

    def put(self, key: str, artifact: Any) -> bool:
        """Atomically store a validated ``status=ok`` artifact; refuse others."""
        if (
            not isinstance(artifact, dict)
            or schema.validate(artifact)
            or artifact.get("status") != "ok"
        ):
            return False
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        payload = {**artifact, "cached": False}
        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            _unlink(Path(tmp_name))
            raise
        self.prune()
        return True

    def prune(self, *, now: float | None = None) -> int:
        """Drop expired files, then oldest files beyond ``max_files``."""
        if not self.cache_dir.is_dir():
            return 0
        moment = time.time() if now is None else now
        removed = 0
        entries: list[tuple[float, Path]] = []
        for path in self.cache_dir.glob("*.json"):
            if _KEY_RE.fullmatch(path.stem) is None:
                # Only manage own cache entries: stray ``.tmp-*.json``
                # leftovers and unrelated files are left untouched.
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

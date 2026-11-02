#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""High-confidence secret detection, injection warnings and sanitization.

Scan results never carry matched text: a hit reports only the pattern
kind and a line number, so results stay safe to embed in ``errors`` or
``warnings``. Prompt-injection-like text produces warnings, not blockers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SANITIZED_CHARS = 4000


@dataclass(frozen=True)
class SecretHit:
    """A secret-looking match identified by pattern ``kind`` and ``line`` only."""

    kind: str
    line: int


_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ),
    (
        "github_token",
        re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    ),
    (
        "openai_key",
        # \x2d spells the dash non-literally so committed source carries no
        # token-shaped prefix; the compiled pattern is unchanged.
        re.compile(r"\bsk\x2d(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?token|"
            r"auth[_-]?token|private[_-]?token|client[_-]?secret|password|passwd)"
            r"\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+\-=]{16,}"
        ),
    ),
)

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"(?i)\bignore\s+(?:all\s+|any\s+|the\s+)?"
            r"(?:previous|prior|above|earlier)\s+(?:instructions?|rules?|prompts?)\b"
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"(?i)\byou\s+are\s+now\b|\bnew\s+system\s+prompt\b|"
            r"\bact\s+as\s+(?:a\s+|an\s+)?(?:different|new|another)\b"
        ),
    ),
    (
        "prompt_exfiltration",
        re.compile(
            r"(?i)\b(?:reveal|print|show|repeat|leak|output)\s+(?:your|the)\s+"
            r"(?:system|full|original|entire)\s+(?:prompt|instructions?)\b"
        ),
    ),
    (
        "schema_bypass",
        re.compile(
            r"(?i)\b(?:do\s+not|don'?t|never)\s+(?:follow|use|output|emit|respect)\s+"
            r"(?:the\s+)?(?:schema|json|format|artifact)\b"
        ),
    ),
)

_TAG_RE = re.compile(r"<[^>\n]{0,200}>")
_MENTION_RE = re.compile(r"(?<![\w./@-])@[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
_UNSAFE_LINK_RE = re.compile(r"(?i)\b(?:javascript|vbscript|data|file)\s*:[^\s)\]}'\"]{0,500}")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_for_secrets(text: object) -> list[SecretHit]:
    """Return high-confidence secret hits; matched values are never reported."""
    if not isinstance(text, str) or not text:
        return []
    hits = [
        SecretHit(kind, _line_of(text, match.start()))
        for kind, pattern in _SECRET_PATTERNS
        for match in pattern.finditer(text)
    ]
    hits.sort(key=lambda hit: (hit.line, hit.kind))
    return hits


def injection_warnings(text: object) -> list[str]:
    """Return static warnings for prompt-injection-like text (never blockers)."""
    if not isinstance(text, str) or not text:
        return []
    warnings = []
    for kind, pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            line = _line_of(text, match.start())
            warnings.append(f"prompt-injection-like text detected ({kind}) at line {line}")
    return warnings


def sanitize_text(text: object, max_chars: int = MAX_SANITIZED_CHARS) -> str:
    """Strip HTML tags, unsafe links and ``@`` mentions; bound the result length."""
    if not isinstance(text, str):
        return ""
    cleaned = _TAG_RE.sub("", text)
    cleaned = _UNSAFE_LINK_RE.sub("[unsafe-link-removed]", cleaned)
    cleaned = _MENTION_RE.sub("[mention]", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + " ... [truncated]"
    return cleaned

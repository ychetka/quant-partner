#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Strict, stdlib-only validator for the advisory review artifact.

The validator never coerces values: wrong types, unknown keys, invalid
enums, oversized strings or inconsistent status combinations all produce
``Issue`` entries. An ``Issue`` carries only a JSON-style path and a
stable code - never the offending value - so validation output stays
safe to log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

SCHEMA_VERSION = "1.0"

STATUSES = ("ok", "rejected", "error")
VERDICTS = ("no_findings", "informational", "needs_changes")
SEVERITIES = ("blocker", "major", "minor", "nit")
CATEGORIES = (
    "correctness",
    "security",
    "concurrency",
    "api",
    "tests",
    "performance",
    "maintainability",
    "spec_drift",
)
CONFIDENCES = ("high", "medium", "low")
SIDES = ("RIGHT",)
DROP_REASONS = (
    "schema",
    "path_not_in_diff",
    "line_not_in_diff",
    "secret",
    "duplicate",
    "over_limit",
)

MAX_SUMMARY_CHARS = 1000
MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 2000
MAX_FIX_CHARS = 2000
MAX_PATH_CHARS = 512
MAX_ID_CHARS = 64
MAX_REVIEW_ID_CHARS = 128
MAX_NAME_CHARS = 128
MAX_WARNING_CHARS = 500
MAX_ERROR_CODE_CHARS = 64
MAX_ERROR_MESSAGE_CHARS = 500
MAX_FINDINGS = 100
MAX_DROPPED = 200
MAX_UNREVIEWED = 500
MAX_WARNINGS = 50
MAX_ERRORS = 20
MAX_LINE_NUMBER = 1_000_000
MAX_FINDING_SPAN = 10_000

_TOP_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "advisory",
        "review_id",
        "idempotency_key",
        "cached",
        "head_sha",
        "base_sha",
        "provenance",
        "usage",
        "verdict",
        "summary",
        "findings",
        "dropped_findings",
        "unreviewed",
        "warnings",
        "errors",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "partner_id",
        "backend",
        "model_alias",
        "model_requested",
        "model_used",
        "strict_model",
        "model_match",
        "discovery_confirmed",
        "prompt_version",
        "partner_version",
        "generated_at_utc",
    }
)
_FINDING_KEYS = frozenset(
    {
        "id",
        "severity",
        "category",
        "path",
        "line",
        "end_line",
        "side",
        "title",
        "body",
        "confidence",
        "suggested_fix",
    }
)
_DROPPED_KEYS = frozenset({"id", "reason"})
_ERROR_KEYS = frozenset({"code", "message"})
_USAGE_KEYS = frozenset({"prompt_tokens", "candidates_tokens", "total_tokens"})

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_IDEMPOTENCY_RE = re.compile(r"[0-9a-fA-F]{64}")
# ``generated_at_utc`` is pinned to one exact shape: no fractional
# seconds, no numeric offsets - the field name promises UTC, so the
# only acceptable designator is ``Z``.
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?")
_FENCE_RE = re.compile(r"\A```[A-Za-z0-9_-]*[ \t]*\r?\n(.*)\r?\n```[ \t]*\Z", re.DOTALL)


@dataclass(frozen=True)
class Issue:
    """One validation failure located by ``path`` and classified by ``code``."""

    path: str
    code: str


class SchemaError(Exception):
    """Raised by :func:`require_valid` and :func:`extract_json` on bad input."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = list(issues)
        preview = "; ".join(f"{issue.path}:{issue.code}" for issue in self.issues[:8])
        super().__init__(f"invalid advisory artifact ({len(self.issues)} issue(s)): {preview}")


class _DuplicateKey(Exception):
    pass


class _NonJsonConstant(Exception):
    pass


def _issue(issues: list[Issue], path: str, code: str) -> None:
    issues.append(Issue(path, code))


def _unknown_keys(
    value: dict[str, Any], allowed: frozenset[str], path: str, issues: list[Issue]
) -> None:
    for key in value:
        if key not in allowed:
            _issue(issues, f"{path}.{key}", "unknown_key")


def _required(value: dict[str, Any], key: str, path: str, issues: list[Issue]) -> Any:
    if key not in value:
        _issue(issues, f"{path}.{key}", "required")
        return None
    return value[key]


def _check_str(
    value: Any,
    path: str,
    issues: list[Issue],
    *,
    max_chars: int,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, str):
        _issue(issues, path, "type")
        return
    if not allow_empty and not value:
        _issue(issues, path, "empty")
        return
    if len(value) > max_chars:
        _issue(issues, path, "too_large")


def _check_str_field(
    value: dict[str, Any],
    key: str,
    path: str,
    issues: list[Issue],
    *,
    max_chars: int,
    allow_empty: bool = False,
) -> None:
    field = _required(value, key, path, issues)
    if field is not None or key in value:
        _check_str(field, f"{path}.{key}", issues, max_chars=max_chars, allow_empty=allow_empty)


def _check_bool_field(value: dict[str, Any], key: str, path: str, issues: list[Issue]) -> None:
    field = _required(value, key, path, issues)
    if key in value and type(field) is not bool:
        _issue(issues, f"{path}.{key}", "type")


def _check_enum_field(
    value: dict[str, Any],
    key: str,
    path: str,
    issues: list[Issue],
    allowed: tuple[str, ...],
) -> None:
    field = _required(value, key, path, issues)
    if key not in value:
        return
    if not isinstance(field, str):
        _issue(issues, f"{path}.{key}", "type")
    elif field not in allowed:
        _issue(issues, f"{path}.{key}", "enum")


def _check_list_field(
    value: dict[str, Any],
    key: str,
    path: str,
    issues: list[Issue],
    *,
    max_items: int,
) -> list[Any]:
    field = _required(value, key, path, issues)
    if key not in value:
        return []
    if not isinstance(field, list):
        _issue(issues, f"{path}.{key}", "type")
        return []
    if len(field) > max_items:
        _issue(issues, f"{path}.{key}", "too_large")
    return field


def _check_sha(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, str):
        _issue(issues, path, "type")
    elif _SHA_RE.fullmatch(value) is None:
        _issue(issues, path, "format")


def _check_idempotency_key(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, str):
        _issue(issues, path, "type")
    elif _IDEMPOTENCY_RE.fullmatch(value) is None:
        _issue(issues, path, "format")


def _check_timestamp(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, str):
        _issue(issues, path, "type")
        return
    if _TIMESTAMP_RE.fullmatch(value) is None:
        _issue(issues, path, "format")
        return
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _issue(issues, path, "format")


def _check_date(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, str):
        _issue(issues, path, "type")
        return
    if _DATE_RE.fullmatch(value) is None:
        _issue(issues, path, "format")
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        _issue(issues, path, "format")


def _check_version(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, str):
        _issue(issues, path, "type")
    elif _VERSION_RE.fullmatch(value) is None:
        _issue(issues, path, "format")


def _provenance_issues(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, dict):
        _issue(issues, path, "type")
        return
    _unknown_keys(value, _PROVENANCE_KEYS, path, issues)
    for key in ("partner_id", "backend", "model_alias"):
        _check_str_field(value, key, path, issues, max_chars=MAX_NAME_CHARS)
    _check_str_field(
        value, "model_requested", path, issues, max_chars=MAX_NAME_CHARS, allow_empty=True
    )
    _check_str_field(value, "model_used", path, issues, max_chars=MAX_NAME_CHARS, allow_empty=True)
    _check_bool_field(value, "strict_model", path, issues)
    _check_bool_field(value, "model_match", path, issues)
    _check_bool_field(value, "discovery_confirmed", path, issues)
    if "prompt_version" in value:
        _check_date(value["prompt_version"], f"{path}.prompt_version", issues)
    else:
        _issue(issues, f"{path}.prompt_version", "required")
    if "partner_version" in value:
        _check_version(value["partner_version"], f"{path}.partner_version", issues)
    else:
        _issue(issues, f"{path}.partner_version", "required")
    if "generated_at_utc" in value:
        _check_timestamp(value["generated_at_utc"], f"{path}.generated_at_utc", issues)
    else:
        _issue(issues, f"{path}.generated_at_utc", "required")
    model_used = value.get("model_used")
    model_requested = value.get("model_requested")
    model_match = value.get("model_match")
    if (
        isinstance(model_used, str)
        and isinstance(model_requested, str)
        and type(model_match) is bool
        and model_match != (bool(model_used) and model_used == model_requested)
    ):
        _issue(issues, f"{path}.model_match", "inconsistent")


def finding_issues(value: Any, path: str = "$.finding") -> list[Issue]:
    """Validate one finding object; used to drop bad findings without rejecting all."""
    issues: list[Issue] = []
    if not isinstance(value, dict):
        _issue(issues, path, "type")
        return issues
    _unknown_keys(value, _FINDING_KEYS, path, issues)
    _check_str_field(value, "id", path, issues, max_chars=MAX_ID_CHARS)
    _check_enum_field(value, "severity", path, issues, SEVERITIES)
    _check_enum_field(value, "category", path, issues, CATEGORIES)
    _check_str_field(value, "path", path, issues, max_chars=MAX_PATH_CHARS)
    line = _required(value, "line", path, issues)
    end_line = _required(value, "end_line", path, issues)
    line_ok = type(line) is int and 1 <= line <= MAX_LINE_NUMBER
    end_ok = type(end_line) is int and 1 <= end_line <= MAX_LINE_NUMBER
    if "line" in value and not line_ok:
        _issue(issues, f"{path}.line", "range" if type(line) is int else "type")
    if "end_line" in value and not end_ok:
        _issue(issues, f"{path}.end_line", "range" if type(end_line) is int else "type")
    if line_ok and end_ok:
        if end_line < line:
            _issue(issues, f"{path}.end_line", "range")
        elif end_line - line > MAX_FINDING_SPAN:
            _issue(issues, f"{path}.end_line", "range")
    _check_enum_field(value, "side", path, issues, SIDES)
    _check_str_field(value, "title", path, issues, max_chars=MAX_TITLE_CHARS)
    _check_str_field(value, "body", path, issues, max_chars=MAX_BODY_CHARS)
    _check_enum_field(value, "confidence", path, issues, CONFIDENCES)
    if "suggested_fix" not in value:
        _issue(issues, f"{path}.suggested_fix", "required")
    elif value["suggested_fix"] is not None:
        _check_str(
            value["suggested_fix"],
            f"{path}.suggested_fix",
            issues,
            max_chars=MAX_FIX_CHARS,
            allow_empty=True,
        )
    return issues


def _dropped_issues(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, dict):
        _issue(issues, path, "type")
        return
    _unknown_keys(value, _DROPPED_KEYS, path, issues)
    _check_str_field(value, "id", path, issues, max_chars=MAX_ID_CHARS)
    _check_enum_field(value, "reason", path, issues, DROP_REASONS)


def _error_issues(value: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(value, dict):
        _issue(issues, path, "type")
        return
    _unknown_keys(value, _ERROR_KEYS, path, issues)
    _check_str_field(value, "code", path, issues, max_chars=MAX_ERROR_CODE_CHARS)
    _check_str_field(value, "message", path, issues, max_chars=MAX_ERROR_MESSAGE_CHARS)


def _usage_issues(value: Any, path: str, issues: list[Issue]) -> None:
    """Validate the optional ``usage`` object: known keys holding non-negative ints."""
    if not isinstance(value, dict):
        _issue(issues, path, "type")
        return
    _unknown_keys(value, _USAGE_KEYS, path, issues)
    for key in _USAGE_KEYS:
        if key not in value:
            continue
        field = value[key]
        if type(field) is not int:
            _issue(issues, f"{path}.{key}", "type")
        elif field < 0:
            _issue(issues, f"{path}.{key}", "range")


def _string_items(values: list[Any], path: str, issues: list[Issue], *, max_chars: int) -> None:
    for index, item in enumerate(values):
        _check_str(item, f"{path}[{index}]", issues, max_chars=max_chars)


def _common_issues(value: Any) -> list[Issue]:
    """Validate every field except the internals of ``findings`` elements."""
    issues: list[Issue] = []
    if not isinstance(value, dict):
        _issue(issues, "$", "type")
        return issues
    _unknown_keys(value, _TOP_KEYS, "$", issues)
    if "schema_version" not in value:
        _issue(issues, "$.schema_version", "required")
    elif value["schema_version"] != SCHEMA_VERSION:
        _issue(issues, "$.schema_version", "unsupported_version")
    _check_enum_field(value, "status", "$", issues, STATUSES)
    if "advisory" not in value:
        _issue(issues, "$.advisory", "required")
    elif type(value["advisory"]) is not bool:
        _issue(issues, "$.advisory", "type")
    elif value["advisory"] is not True:
        _issue(issues, "$.advisory", "inconsistent")
    _check_str_field(value, "review_id", "$", issues, max_chars=MAX_REVIEW_ID_CHARS)
    if "idempotency_key" not in value:
        _issue(issues, "$.idempotency_key", "required")
    else:
        _check_idempotency_key(value["idempotency_key"], "$.idempotency_key", issues)
    _check_bool_field(value, "cached", "$", issues)
    if "head_sha" not in value:
        _issue(issues, "$.head_sha", "required")
    else:
        _check_sha(value["head_sha"], "$.head_sha", issues)
    if "base_sha" not in value:
        _issue(issues, "$.base_sha", "required")
    else:
        _check_sha(value["base_sha"], "$.base_sha", issues)
    if "provenance" not in value:
        _issue(issues, "$.provenance", "required")
    else:
        _provenance_issues(value["provenance"], "$.provenance", issues)
    if "usage" in value:
        _usage_issues(value["usage"], "$.usage", issues)
    _check_enum_field(value, "verdict", "$", issues, VERDICTS)
    _check_str_field(value, "summary", "$", issues, max_chars=MAX_SUMMARY_CHARS, allow_empty=True)
    _check_list_field(value, "findings", "$", issues, max_items=MAX_FINDINGS)
    dropped = _check_list_field(value, "dropped_findings", "$", issues, max_items=MAX_DROPPED)
    for index, item in enumerate(dropped):
        _dropped_issues(item, f"$.dropped_findings[{index}]", issues)
    unreviewed = _check_list_field(value, "unreviewed", "$", issues, max_items=MAX_UNREVIEWED)
    _string_items(unreviewed, "$.unreviewed", issues, max_chars=MAX_PATH_CHARS)
    warnings = _check_list_field(value, "warnings", "$", issues, max_items=MAX_WARNINGS)
    _string_items(warnings, "$.warnings", issues, max_chars=MAX_WARNING_CHARS)
    errors = _check_list_field(value, "errors", "$", issues, max_items=MAX_ERRORS)
    for index, item in enumerate(errors):
        _error_issues(item, f"$.errors[{index}]", issues)
    return issues


def envelope_issues(value: Any) -> list[Issue]:
    """Validate the artifact envelope; ``findings`` elements are not inspected."""
    return _common_issues(value)


def validate(value: Any) -> list[Issue]:
    """Return all schema issues for a fully formed advisory artifact."""
    issues = _common_issues(value)
    if not isinstance(value, dict):
        return issues
    findings = value.get("findings")
    if isinstance(findings, list):
        seen_ids: set[str] = set()
        for index, finding in enumerate(findings):
            issues.extend(finding_issues(finding, f"$.findings[{index}]"))
            if isinstance(finding, dict) and isinstance(finding.get("id"), str):
                if finding["id"] in seen_ids:
                    _issue(issues, f"$.findings[{index}].id", "duplicate_id")
                seen_ids.add(finding["id"])
    status = value.get("status")
    errors = value.get("errors")
    dropped_findings = value.get("dropped_findings")
    cached = value.get("cached")
    verdict = value.get("verdict")
    if isinstance(errors, list) and status in STATUSES:
        if status == "ok" and errors:
            _issue(issues, "$.errors", "inconsistent")
        if status != "ok" and not errors:
            _issue(issues, "$.errors", "inconsistent")
    if status in ("rejected", "error"):
        if isinstance(findings, list) and findings:
            _issue(issues, "$.findings", "inconsistent")
        if isinstance(dropped_findings, list) and dropped_findings:
            _issue(issues, "$.dropped_findings", "inconsistent")
        if cached is True:
            _issue(issues, "$.cached", "inconsistent")
    if isinstance(findings, list) and isinstance(verdict, str) and verdict in VERDICTS:
        if (not findings) != (verdict == "no_findings"):
            _issue(issues, "$.verdict", "inconsistent")
    return issues


def require_valid(value: Any) -> dict[str, Any]:
    """Return ``value`` as a dict or raise :class:`SchemaError` with all issues."""
    issues = validate(value)
    if issues:
        raise SchemaError(issues)
    assert isinstance(value, dict)
    return value


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = item
    return result


def _reject_constant(name: str) -> Any:
    raise _NonJsonConstant(name)


def extract_json(text: Any) -> dict[str, Any]:
    """Parse exactly one JSON object from model text, fail closed otherwise.

    A single surrounding fenced code block may be stripped. Duplicate keys
    and ``NaN``/``Infinity`` constants are rejected instead of coerced.
    """
    if not isinstance(text, str):
        raise SchemaError([Issue("$", "type")])
    stripped = text.strip()
    if not stripped:
        raise SchemaError([Issue("$", "empty")])
    candidate = stripped
    if stripped.startswith("```"):
        match = _FENCE_RE.match(stripped)
        if match is None:
            raise SchemaError([Issue("$", "invalid_json")])
        candidate = match.group(1).strip()
    try:
        data = json.loads(
            candidate,
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        raise SchemaError([Issue("$", "duplicate_key")]) from None
    except _NonJsonConstant:
        raise SchemaError([Issue("$", "non_json_constant")]) from None
    except json.JSONDecodeError:
        raise SchemaError([Issue("$", "invalid_json")]) from None
    if not isinstance(data, dict):
        raise SchemaError([Issue("$", "type")])
    return data

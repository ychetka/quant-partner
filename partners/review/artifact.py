#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Advisory artifact construction, finding filtering and serialization.

Every builder funnels through :func:`schema.require_valid`, so an object
that cannot validate is never returned. Server-owned fields
(``review_id``, ``idempotency_key``, ``cached``, ``provenance``,
``head_sha``, ``base_sha``) always come from the caller, never from
model output.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from typing import Any

from . import schema
from .diffmap import DiffMap
from .scan import injection_warnings, sanitize_text, scan_for_secrets


def new_review_id() -> str:
    """Generate a caller-correlation id when none was supplied."""
    return f"rev-{uuid.uuid4().hex}"


def _base_artifact(
    *,
    status: str,
    review_id: str,
    idempotency_key: str,
    head_sha: str,
    base_sha: str,
    provenance: dict[str, Any],
    verdict: str,
    summary: str = "",
    findings: list[dict[str, Any]] | None = None,
    dropped_findings: list[dict[str, Any]] | None = None,
    unreviewed: list[str] | None = None,
    warnings: list[str] | None = None,
    errors: list[dict[str, str]] | None = None,
    cached: bool = False,
) -> dict[str, Any]:
    artifact = {
        "schema_version": schema.SCHEMA_VERSION,
        "status": status,
        "advisory": True,
        "review_id": review_id,
        "idempotency_key": idempotency_key,
        "cached": cached,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "provenance": provenance,
        "verdict": verdict,
        "summary": summary,
        "findings": findings or [],
        "dropped_findings": dropped_findings or [],
        "unreviewed": unreviewed or [],
        "warnings": warnings or [],
        "errors": errors or [],
    }
    return schema.require_valid(artifact)


def ok_artifact(**kwargs: Any) -> dict[str, Any]:
    """Build a ``status=ok`` artifact; see :func:`_base_artifact` fields."""
    kwargs.pop("status", None)
    kwargs.pop("errors", None)
    return _base_artifact(status="ok", errors=[], **kwargs)


def rejected_artifact(*, code: str, message: str, **kwargs: Any) -> dict[str, Any]:
    """Build a ``status=rejected`` artifact for policy/provenance refusals."""
    kwargs.pop("status", None)
    kwargs.pop("verdict", None)
    return _base_artifact(
        status="rejected",
        verdict="no_findings",
        errors=[{"code": code, "message": message}],
        **kwargs,
    )


def error_artifact(*, code: str, message: str, **kwargs: Any) -> dict[str, Any]:
    """Build a ``status=error`` artifact for malformed output or failures."""
    kwargs.pop("status", None)
    kwargs.pop("verdict", None)
    return _base_artifact(
        status="error",
        verdict="no_findings",
        errors=[{"code": code, "message": message}],
        **kwargs,
    )


def serialize(artifact: dict[str, Any]) -> str:
    """Serialize an artifact to deterministic JSON text after validation."""
    schema.require_valid(artifact)
    return json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def derive_verdict(findings: list[dict[str, Any]]) -> str:
    """Map kept findings to the advisory verdict enum."""
    if not findings:
        return "no_findings"
    if any(finding.get("severity") in ("blocker", "major") for finding in findings):
        return "needs_changes"
    return "informational"


def stale_for_head(artifact: dict[str, Any], current_head_sha: Any) -> bool:
    """True when the artifact's ``head_sha`` no longer matches the PR head.

    The publisher must discard stale artifacts; comparison is
    case-insensitive per spec section 7 and fails closed on bad input.
    """
    head = artifact.get("head_sha") if isinstance(artifact, dict) else None
    if not isinstance(head, str) or not isinstance(current_head_sha, str):
        return True
    return head.lower() != current_head_sha.lower()


def _drop_id(raw: Any) -> str:
    """Best-effort finding id for drop records; secret-like ids never echo."""
    if isinstance(raw, dict) and isinstance(raw.get("id"), str) and raw["id"]:
        if scan_for_secrets(raw["id"]):
            return "?"
        return raw["id"][: schema.MAX_ID_CHARS]
    return "?"


# Model-controlled finding fields that are preserved in the artifact or
# echoed back as drop ids; every one is secret-scanned before use.
_CONTROLLED_FINDING_KEYS = ("id", "path", "title", "body", "suggested_fix")


def _finding_text(finding: dict[str, Any]) -> str:
    fix = finding.get("suggested_fix")
    return "\n".join([finding["title"], finding["body"], fix if isinstance(fix, str) else ""])


def _controlled_text(finding: dict[str, Any]) -> str:
    """Join every model-controlled string field preserved in the artifact."""
    parts = []
    for key in _CONTROLLED_FINDING_KEYS:
        value = finding.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _safe_dropped(entries: Any) -> tuple[list[Any], int]:
    """Keep model-provided drop records whose id carries no secret-like text."""
    safe: list[Any] = []
    unsafe = 0
    if not isinstance(entries, list):
        return safe, unsafe
    for entry in entries:
        if isinstance(entry, dict) and scan_for_secrets(entry.get("id")):
            unsafe += 1
            continue
        safe.append(entry)
    return safe, unsafe


def _safe_paths(paths: Iterable[Any]) -> tuple[list[str], int]:
    """Keep path strings that carry no secret-like text."""
    safe: list[str] = []
    unsafe = 0
    for path in paths:
        if not isinstance(path, str) or scan_for_secrets(path):
            unsafe += 1
            continue
        safe.append(path)
    return safe, unsafe


def filter_findings(
    raw_findings: Any,
    diff_map: DiffMap,
    max_findings: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Split raw findings into kept findings, drops and injection warnings.

    A finding is kept only when it validates, has a unique id, anchors to
    RIGHT-side lines present in ``diff_map`` and contains no secret-like
    content. Everything else lands in ``dropped_findings`` with a reason.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    warnings: list[str] = []
    if not isinstance(raw_findings, list):
        return kept, dropped, warnings
    seen_ids: set[str] = set()
    for raw in raw_findings:
        if schema.finding_issues(raw):
            dropped.append({"id": _drop_id(raw), "reason": "schema"})
            continue
        finding_id = raw["id"]
        # Secret scanning runs before any drop that echoes the id or
        # keeps the finding: id and path are model-controlled too.
        if scan_for_secrets(_controlled_text(raw)):
            dropped.append(
                {"id": "?" if scan_for_secrets(finding_id) else finding_id, "reason": "secret"}
            )
            continue
        if finding_id in seen_ids:
            dropped.append({"id": finding_id, "reason": "duplicate"})
            continue
        seen_ids.add(finding_id)
        if not diff_map.anchorable(raw["path"]):
            reason = "path_not_in_diff" if raw["path"] not in diff_map.files else "line_not_in_diff"
            dropped.append({"id": finding_id, "reason": reason})
            continue
        if not diff_map.contains(raw["path"], raw["line"], raw["end_line"]):
            dropped.append({"id": finding_id, "reason": "line_not_in_diff"})
            continue
        if len(kept) >= max_findings:
            dropped.append({"id": finding_id, "reason": "over_limit"})
            continue
        warnings.extend(injection_warnings(_finding_text(raw)))
        fix = raw["suggested_fix"]
        kept.append(
            {
                **raw,
                "title": sanitize_text(raw["title"], schema.MAX_TITLE_CHARS),
                "body": sanitize_text(raw["body"], schema.MAX_BODY_CHARS),
                "suggested_fix": (
                    sanitize_text(fix, schema.MAX_FIX_CHARS) if isinstance(fix, str) else None
                ),
            }
        )
    return kept, dropped, warnings


def artifact_from_model(
    raw_text: Any,
    *,
    diff_map: DiffMap,
    max_findings: int,
    review_id: str,
    idempotency_key: str,
    head_sha: str,
    base_sha: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Turn raw model output into a validated ``status=ok`` artifact.

    The envelope must be schema-shaped and internally consistent for an
    ``ok`` result; individual malformed findings are dropped with reason
    ``schema`` instead of failing the whole artifact. Caller-supplied
    provenance, SHAs, review_id and idempotency key replace whatever the
    model emitted, and echoed SHAs must match the requested ones.
    """
    data = schema.extract_json(raw_text)
    issues = schema.envelope_issues(data)
    if issues:
        raise schema.SchemaError(issues)
    if data["status"] != "ok" or data["advisory"] is not True or data["cached"] is not False:
        raise schema.SchemaError([schema.Issue("$.status", "inconsistent")])
    if data["errors"]:
        raise schema.SchemaError([schema.Issue("$.errors", "inconsistent")])
    if data["head_sha"].lower() != head_sha.lower() or data["base_sha"].lower() != base_sha.lower():
        raise schema.SchemaError([schema.Issue("$.head_sha", "inconsistent")])
    kept, dropped, extra_warnings = filter_findings(data["findings"], diff_map, max_findings)
    # Model-provided drop records and unreviewed paths are echoed into the
    # artifact: entries carrying secret-like text are omitted, never echoed.
    model_dropped, unsafe_dropped = _safe_dropped(data["dropped_findings"])
    dropped = (model_dropped + dropped)[: schema.MAX_DROPPED]
    safe_unreviewed, unsafe_unreviewed = _safe_paths(
        set(data["unreviewed"]) | set(diff_map.unreviewed())
    )
    unreviewed = sorted(safe_unreviewed)[: schema.MAX_UNREVIEWED]
    unsafe_count = unsafe_dropped + unsafe_unreviewed
    warnings = (
        [f"omitted {unsafe_count} model-provided entries containing secret-like text"]
        if unsafe_count
        else []
    )
    warnings.extend(sanitize_text(w, schema.MAX_WARNING_CHARS) for w in data["warnings"])
    warnings.extend(extra_warnings)
    warnings = warnings[: schema.MAX_WARNINGS]
    summary = sanitize_text(data["summary"], schema.MAX_SUMMARY_CHARS)
    if scan_for_secrets(summary) or scan_for_secrets("\n".join(warnings)):
        raise schema.SchemaError([schema.Issue("$.summary", "secret")])
    return ok_artifact(
        review_id=review_id,
        idempotency_key=idempotency_key,
        head_sha=head_sha,
        base_sha=base_sha,
        provenance=provenance,
        verdict=derive_verdict(kept),
        summary=summary,
        findings=kept,
        dropped_findings=dropped,
        unreviewed=unreviewed,
        warnings=warnings,
    )

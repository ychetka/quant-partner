#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Allowlisted model alias resolution and strict provenance construction.

Every check fails closed: unknown aliases, unconfirmed discovery and
missing or mismatched ``model_used`` raise :class:`ProvenanceError`
carrying the artifact ``code`` and ``status`` from spec section 12.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MATCH_POLICIES = ("exact",)


class ProvenanceError(Exception):
    """Fail-closed provenance failure mapped to an artifact status and code."""

    def __init__(self, code: str, message: str, *, status: str = "rejected") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ResolvedModel:
    """An allowlisted alias resolved to its pinned backend model."""

    alias: str
    backend_model: str
    match: str


def utc_now() -> str:
    """Current UTC time in the artifact's ISO-8601 timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_alias(alias: Any, allowed_models: Any) -> ResolvedModel:
    """Resolve a caller-supplied alias through the configured allowlist.

    Unknown or free-form aliases are rejected before any provider access.
    """
    if not isinstance(alias, str) or not alias:
        raise ProvenanceError(
            "unknown_model",
            "model must be a non-empty allowlisted alias",
            status="error",
        )
    if not isinstance(allowed_models, Mapping):
        raise ProvenanceError(
            "unknown_model",
            "no review allowed_models are configured",
            status="error",
        )
    entry = allowed_models.get(alias)
    if not isinstance(entry, Mapping):
        raise ProvenanceError(
            "unknown_model",
            f"model alias is not allowlisted: {alias}",
            status="error",
        )
    backend_model = entry.get("backend_model")
    if not isinstance(backend_model, str) or not backend_model:
        raise ProvenanceError(
            "invalid_model_config",
            f"allowlisted alias has no backend_model: {alias}",
            status="error",
        )
    match = entry.get("match", "exact")
    if match != "exact":
        raise ProvenanceError(
            "invalid_model_config",
            f"unsupported match policy for alias {alias}; only 'exact' is supported",
            status="error",
        )
    return ResolvedModel(alias=alias, backend_model=backend_model, match="exact")


def confirm_discovery(resolved: ResolvedModel, discovered: Iterable[str] | None) -> None:
    """Fail closed unless live discovery confirms the pinned backend model."""
    if discovered is None:
        raise ProvenanceError(
            "model_unconfirmed",
            f"live model discovery unavailable; cannot confirm {resolved.backend_model}",
        )
    available = {model for model in discovered if isinstance(model, str)}
    if resolved.backend_model not in available:
        raise ProvenanceError(
            "model_unconfirmed",
            f"model not confirmed by live discovery: {resolved.backend_model}",
        )


def check_model_used(model_requested: str, model_used: Any, *, strict_model: bool) -> bool:
    """Compare provider-reported ``model_used`` against the requested model.

    ``model_used`` must come from provider response metadata, never from
    generated text. Strict mode fails closed on missing or mismatched
    values; non-strict mode reports ``model_match`` instead of raising.
    """
    if strict_model:
        if not isinstance(model_used, str) or not model_used:
            raise ProvenanceError(
                "model_unverifiable",
                f"backend did not report model_used for requested model {model_requested}",
            )
        if model_used != model_requested:
            raise ProvenanceError(
                "model_mismatch",
                f"model_used {model_used} differs from requested {model_requested}",
            )
        return True
    return isinstance(model_used, str) and model_used == model_requested


def build_provenance(
    *,
    partner_id: str,
    backend: str,
    model_alias: str,
    model_requested: str,
    model_used: str,
    strict_model: bool,
    discovery_confirmed: bool,
    prompt_version: str,
    partner_version: str,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Assemble the provenance block; ``model_match`` is always derived."""
    return {
        "partner_id": partner_id,
        "backend": backend,
        "model_alias": model_alias,
        "model_requested": model_requested,
        "model_used": model_used,
        "strict_model": bool(strict_model),
        "model_match": bool(model_used) and model_used == model_requested,
        "discovery_confirmed": bool(discovery_confirmed),
        "prompt_version": prompt_version,
        "partner_version": partner_version,
        "generated_at_utc": generated_at_utc or utc_now(),
    }

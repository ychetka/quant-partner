#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Backend interface used by the MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CompletionResult:
    """Assistant text plus honest provider-reported model metadata.

    ``model_used`` is read from provider response metadata only; ``None``
    means the response carried no served-model identity the backend can
    attest to. It is never inferred from the request, generated text or
    configuration.

    ``usage`` carries provider-reported token counts only, normalized to
    a subset of ``{"prompt_tokens", "candidates_tokens", "total_tokens",
    "thoughts_tokens", "cached_tokens"}``; ``None`` means the response
    reported no usable usage data. It is never inferred from request or
    response text.
    """

    text: str
    model_used: str | None = None
    usage: dict[str, int] | None = None


class PartnerBackend(Protocol):
    """Minimal synchronous backend contract."""

    def list_models(self, refresh: bool = False) -> list[str]:
        """Return model IDs available to the configured account.

        ``refresh=True`` asks the backend to bypass any cached catalog
        and perform a fresh discovery call; backends without a catalog
        cache may ignore it.
        """
        raise NotImplementedError

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> str:
        """Return assistant text for one stateless completion."""
        raise NotImplementedError

    def complete_ex(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> CompletionResult:
        """Return text plus provider-reported ``model_used`` metadata.

        Used by the advisory ``review_code`` tool, which verifies the
        served model instead of trusting the requested one.
        """
        raise NotImplementedError

#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Backend interface used by the MCP server."""
from __future__ import annotations

from typing import Any, Protocol


class PartnerBackend(Protocol):
    """Minimal synchronous backend contract."""

    def list_models(self) -> list[str]:
        """Return model IDs available to the configured account."""

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> str:
        """Return assistant text for one stateless completion."""

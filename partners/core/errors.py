#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Shared error types and safe error formatting for partner tools."""
from __future__ import annotations


class PartnerError(Exception):
    """Human-readable partner error mapped to an MCP tool response."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_text(self) -> str:
        text = f"ERROR {self.code}: {self.message}"
        if self.hint:
            text += f" Hint: {self.hint}"
        return text


def sanitize(text: object, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Return text with known secrets removed before it reaches the client."""
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def invalid_config(message: str, hint: str = "") -> PartnerError:
    return PartnerError("invalid_config", message, hint)

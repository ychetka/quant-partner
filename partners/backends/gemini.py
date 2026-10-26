#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Gemini REST backend."""
from __future__ import annotations

import socket
from typing import Any

from core.errors import PartnerError, invalid_config
from core.http import get_json, post_json

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
_IPV4_PATCHED = False


def _force_ipv4() -> None:
    """Keep Google AI POSTs on IPv4 on hosts with broken IPv6 egress."""
    global _IPV4_PATCHED
    if _IPV4_PATCHED:
        return
    original = socket.getaddrinfo

    def ipv4_only(host: str, port: int, *args: Any, **kwargs: Any):
        return [x for x in original(host, port, *args, **kwargs) if x[0] == socket.AF_INET]

    socket.getaddrinfo = ipv4_only
    _IPV4_PATCHED = True


class GeminiBackend:
    def __init__(self, auth: dict[str, Any], options: dict[str, Any], timeout: int) -> None:
        self.api_key = str(auth.get("gemini_api_key", ""))
        self.timeout = timeout
        self.secrets = [self.api_key]
        if options.get("ipv4_only", True):
            _force_ipv4()

    def _require_key(self) -> None:
        if not self.api_key or "HERE" in self.api_key.upper():
            raise invalid_config(
                "gemini_api_key is not configured",
                "set partners[].auth.gemini_api_key in partners/config.json",
            )

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def list_models(self) -> list[str]:
        self._require_key()
        out = get_json(f"{API_ROOT}/models", headers=self._headers(), timeout=self.timeout, secrets=self.secrets)
        return sorted(
            str(model.get("name", "")).removeprefix("models/")
            for model in out.get("models", [])
            if model.get("name")
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> str:
        self._require_key()
        contents = []
        for message in messages:
            role = message.get("role", "user")
            if role == "assistant":
                role = "model"
            if role not in ("user", "model"):
                continue
            contents.append({"role": role, "parts": [{"text": message.get("content", "")}]})
        if not contents:
            raise PartnerError("invalid_request", "messages are empty")
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": generation.get("temperature", 0.3),
                "maxOutputTokens": generation.get("max_tokens", 8192),
            },
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        out = post_json(
            f"{API_ROOT}/models/{model}:generateContent",
            payload,
            headers=self._headers(),
            timeout=self.timeout,
            secrets=self.secrets,
        )
        candidates = out.get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        text = "".join(str(part.get("text", "")) for part in parts).strip()
        if not text:
            reason = candidates[0].get("finishReason", "empty response") if candidates else "no candidates"
            raise PartnerError("backend_error", f"Gemini returned an empty response ({reason})")
        return text

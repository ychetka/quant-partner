#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Gemini REST backend."""

from __future__ import annotations

import socket
from typing import Any
from urllib.parse import urlencode

from core.errors import PartnerError, invalid_config
from core.http import get_json, post_json

from .base import CompletionResult

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

    def list_models(self, refresh: bool = False) -> list[str]:
        # ``refresh`` is accepted for protocol parity; the catalog is
        # fetched on every call and nothing is cached.
        self._require_key()
        models = []
        page_token = ""
        for _ in range(100):
            query = f"?{urlencode({'pageToken': page_token})}" if page_token else ""
            out = get_json(
                f"{API_ROOT}/models{query}",
                headers=self._headers(),
                timeout=self.timeout,
                secrets=self.secrets,
            )
            models.extend(out.get("models", []))
            page_token = str(out.get("nextPageToken") or "")
            if not page_token:
                break
        return sorted(
            str(model.get("name", "")).removeprefix("models/")
            for model in models
            if model.get("name")
        )

    @staticmethod
    def _response_model(out: dict[str, Any]) -> str | None:
        """Served model from ``modelVersion`` stripped of ``models/``; absent means ``None``."""
        version = out.get("modelVersion")
        if not isinstance(version, str) or not version:
            return None
        return version.removeprefix("models/") or None

    @staticmethod
    def _response_usage(out: dict[str, Any]) -> dict[str, int] | None:
        """Token counts from ``usageMetadata``; ``None`` when nothing usable.

        Only integer ``promptTokenCount``/``candidatesTokenCount``/
        ``totalTokenCount`` values are kept, normalized to
        ``prompt_tokens``/``candidates_tokens``/``total_tokens``;
        missing or non-integer counters are omitted.
        """
        raw = out.get("usageMetadata")
        if not isinstance(raw, dict):
            return None
        usage: dict[str, int] = {}
        for provider_key, key in (
            ("promptTokenCount", "prompt_tokens"),
            ("candidatesTokenCount", "candidates_tokens"),
            ("totalTokenCount", "total_tokens"),
        ):
            value = raw.get(provider_key)
            if type(value) is int:
                usage[key] = value
        return usage or None

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> str:
        return self.complete_ex(
            messages, system_prompt=system_prompt, model=model, generation=generation
        ).text

    def complete_ex(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> CompletionResult:
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
            reason = (
                candidates[0].get("finishReason", "empty response")
                if candidates
                else "no candidates"
            )
            raise PartnerError("backend_error", f"Gemini returned an empty response ({reason})")
        return CompletionResult(
            text=text,
            model_used=self._response_model(out),
            usage=self._response_usage(out),
        )

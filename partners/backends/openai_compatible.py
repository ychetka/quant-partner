#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Generic OpenAI-compatible backend."""
from __future__ import annotations

from typing import Any

from core.errors import PartnerError, invalid_config
from core.http import get_json, post_json


class OpenAICompatibleBackend:
    def __init__(self, auth: dict[str, Any], options: dict[str, Any], timeout: int) -> None:
        openai = options.get("openai") if isinstance(options.get("openai"), dict) else {}
        self.base_url = str(openai.get("base_url", "")).rstrip("/")
        self.api_key = str(auth.get("api_key", ""))
        self.timeout = timeout
        self.secrets = [self.api_key] if self.api_key else []

    def _require_base(self) -> None:
        if not self.base_url:
            raise invalid_config("openai base_url is not configured")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def list_models(self) -> list[str]:
        self._require_base()
        out = get_json(f"{self.base_url}/models", headers=self._headers(), timeout=self.timeout, secrets=self.secrets)
        return sorted(str(model.get("id", "")) for model in out.get("data", []) if model.get("id"))

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> str:
        self._require_base()
        wire = [{"role": "system", "content": system_prompt}] if system_prompt else []
        wire += [
            {"role": "assistant" if m.get("role") in ("assistant", "model") else "user", "content": m.get("content", "")}
            for m in messages
        ]
        out = post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": model,
                "messages": wire,
                "stream": False,
                "temperature": generation.get("temperature", 0.2),
                "max_tokens": generation.get("max_tokens", 8192),
            },
            headers=self._headers(),
            timeout=self.timeout,
            secrets=self.secrets,
        )
        choices = out.get("choices") or []
        if not choices:
            raise PartnerError("backend_error", "OpenAI-compatible backend returned no choices")
        text = str(((choices[0].get("message") or {}).get("content")) or "").strip()
        if not text:
            raise PartnerError("backend_error", "OpenAI-compatible backend returned an empty response")
        return text

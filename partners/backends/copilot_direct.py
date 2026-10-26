#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Direct GitHub Copilot backend with OAuth token exchange."""
from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlsplit

from core.errors import PartnerError, invalid_config
from core.http import get_json, post_json

TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"


class CopilotDirectBackend:
    def __init__(self, auth: dict[str, Any], options: dict[str, Any], timeout: int) -> None:
        copilot = options.get("copilot") if isinstance(options.get("copilot"), dict) else {}
        self.oauth_token = str(auth.get("github_oauth_token", ""))
        self.timeout = timeout
        self.integration_id = str(copilot.get("integration_id", "vscode-chat"))
        self.api_mode = str(copilot.get("api", "auto"))
        self.editor_version = str(copilot.get("editor_version", "vscode/1.104.1"))
        self.editor_plugin_version = str(copilot.get("editor_plugin_version", "copilot-chat/0.30.0"))
        self.user_agent = str(copilot.get("user_agent", "GitHubCopilotChat/0.1"))
        self._lock = threading.Lock()
        self._session_token = ""
        self._session_expires_at = 0.0
        self._api_base = ""
        self._models_cache: list[dict[str, Any]] | None = None
        self.secrets = [self.oauth_token]

    def _require_oauth(self) -> None:
        if not self.oauth_token or "HERE" in self.oauth_token.upper():
            raise invalid_config(
                "github_oauth_token is not configured",
                "set partners[].auth.github_oauth_token in partners/config.json",
            )

    def _exchange(self) -> None:
        self._require_oauth()
        out = get_json(
            TOKEN_URL,
            headers={
                "Authorization": f"token {self.oauth_token}",
                "Accept": "application/json",
                "User-Agent": "quant-partner",
            },
            timeout=self.timeout,
            secrets=self.secrets,
        )
        token = str(out.get("token", ""))
        if not token:
            raise PartnerError("unauthorized", "Copilot token exchange returned no session token")
        expires_at = out.get("expires_at", 0)
        try:
            expires = float(expires_at)
        except (TypeError, ValueError):
            expires = time.time() + 1500
        endpoints = out.get("endpoints") if isinstance(out.get("endpoints"), dict) else {}
        api_base = str(endpoints.get("api") or out.get("api_endpoint") or "https://api.githubcopilot.com").rstrip("/")
        parsed = urlsplit(api_base)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "api.githubcopilot.com" or host.endswith(".githubcopilot.com")):
            raise PartnerError("backend_error", f"Copilot token exchange returned an untrusted api_base: {parsed.hostname or api_base}")
        self._session_token = token
        self._session_expires_at = expires
        self._api_base = api_base
        self._models_cache = None
        if self._session_token not in self.secrets:
            self.secrets.append(self._session_token)

    def _session(self, force_refresh: bool = False) -> tuple[str, str]:
        with self._lock:
            if force_refresh or not self._session_token or time.time() >= self._session_expires_at - 60:
                self._exchange()
            return self._api_base, self._session_token

    def _headers(self, session_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {session_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Editor-Version": self.editor_version,
            "Editor-Plugin-Version": self.editor_plugin_version,
            "Copilot-Integration-Id": self.integration_id,
        }

    def _model_entries(self) -> list[dict[str, Any]]:
        if self._models_cache is not None:
            return self._models_cache
        api_base, session = self._session()
        out = get_json(
            f"{api_base}/models",
            headers=self._headers(session),
            timeout=self.timeout,
            secrets=self.secrets,
        )
        data = out.get("data")
        if not isinstance(data, list):
            raise PartnerError("backend_error", "Copilot /models returned an unexpected payload")
        self._models_cache = [x for x in data if isinstance(x, dict)]
        return self._models_cache

    @staticmethod
    def _model_available(item: dict[str, Any]) -> bool:
        policy = item.get("policy") if isinstance(item.get("policy"), dict) else {}
        return str(policy.get("state", "enabled")).lower() != "disabled"

    def list_models(self) -> list[str]:
        return sorted(
            str(model.get("id", ""))
            for model in self._model_entries()
            if model.get("id") and self._model_available(model)
        )

    def _supports(self, model: str, endpoint: str) -> bool:
        for item in self._model_entries():
            if item.get("id") != model or not self._model_available(item):
                continue
            supported = item.get("supported_endpoints")
            if isinstance(supported, list) and supported:
                normalized = {str(x).strip("/").removeprefix("v1/") for x in supported}
                return endpoint.strip("/") in normalized
            return True
        return False

    def _chat_payload(self, messages: list[dict[str, str]], system_prompt: str, model: str, generation: dict[str, Any]) -> dict[str, Any]:
        wire = [{"role": "system", "content": system_prompt}] if system_prompt else []
        wire += [
            {"role": "assistant" if m.get("role") in ("assistant", "model") else "user", "content": m.get("content", "")}
            for m in messages
        ]
        return {
            "model": model,
            "messages": wire,
            "stream": False,
            "temperature": generation.get("temperature", 0.2),
            "max_tokens": generation.get("max_tokens", 8192),
        }

    def _responses_payload(self, messages: list[dict[str, str]], system_prompt: str, model: str, generation: dict[str, Any]) -> dict[str, Any]:
        wire = [
            {"role": "assistant" if m.get("role") in ("assistant", "model") else "user", "content": m.get("content", "")}
            for m in messages
        ]
        payload: dict[str, Any] = {"model": model, "input": wire}
        if system_prompt:
            payload["instructions"] = system_prompt
        if generation.get("max_tokens"):
            payload["max_output_tokens"] = generation["max_tokens"]
        return payload

    @staticmethod
    def _chat_text(out: dict[str, Any]) -> str:
        choices = out.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or choices[0].get("text") or "").strip()

    @staticmethod
    def _responses_text(out: dict[str, Any]) -> str:
        if isinstance(out.get("output_text"), str):
            return out["output_text"].strip()
        parts = []
        for item in out.get("output") or []:
            for content in item.get("content") or []:
                if content.get("type") in ("output_text", "text"):
                    parts.append(str(content.get("text", "")))
        return "".join(parts).strip()

    def _complete_once(self, endpoint: str, messages: list[dict[str, str]], system_prompt: str, model: str, generation: dict[str, Any], force_refresh: bool = False) -> str:
        api_base, session = self._session(force_refresh)
        if endpoint == "responses":
            payload = self._responses_payload(messages, system_prompt, model, generation)
        else:
            payload = self._chat_payload(messages, system_prompt, model, generation)
        out = post_json(
            f"{api_base}/{endpoint}",
            payload,
            headers=self._headers(session),
            timeout=self.timeout,
            secrets=self.secrets,
        )
        return self._responses_text(out) if endpoint == "responses" else self._chat_text(out)

    def _complete_with_auth_retry(self, endpoint: str, messages: list[dict[str, str]], system_prompt: str, model: str, generation: dict[str, Any]) -> str:
        try:
            return self._complete_once(endpoint, messages, system_prompt, model, generation)
        except PartnerError as e:
            if e.code != "unauthorized":
                raise
            return self._complete_once(endpoint, messages, system_prompt, model, generation, force_refresh=True)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        model: str,
        generation: dict[str, Any],
    ) -> str:
        if not model:
            raise PartnerError("invalid_request", "model is empty")
        try:
            models = self.list_models()
            if models and model not in models:
                raise PartnerError(
                    "model_not_supported",
                    f"model is not available for this Copilot account: {model}",
                    "call list_partner_models and choose an available model",
                )
        except PartnerError as e:
            if e.code == "model_not_supported":
                raise
        endpoint = "chat/completions"
        if self.api_mode == "responses":
            endpoint = "responses"
        elif self.api_mode == "auto" and not self._supports(model, "chat/completions") and self._supports(model, "responses"):
            endpoint = "responses"
        try:
            text = self._complete_with_auth_retry(endpoint, messages, system_prompt, model, generation)
        except PartnerError as e:
            if self.api_mode == "auto" and endpoint == "chat/completions" and e.code in ("unsupported_api_for_model", "backend_error"):
                text = self._complete_with_auth_retry("responses", messages, system_prompt, model, generation)
            else:
                raise
        if not text:
            raise PartnerError("backend_error", "Copilot returned an empty response")
        return text

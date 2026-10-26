#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Partner-aware MCP tool dispatch."""
from __future__ import annotations

import json
from typing import Any

from backends.copilot_direct import CopilotDirectBackend
from backends.gemini import GeminiBackend
from backends.openai_compatible import OpenAICompatibleBackend

from .config import (
    PartnerConfig,
    Skill,
    compose_system_prompt,
    list_skills,
    load_partner_config,
    load_skill,
)
from .errors import PartnerError, invalid_config


class PartnerMCPServer:
    def __init__(self, partner_id: str) -> None:
        self.partner_id = partner_id
        self._config: PartnerConfig | None = None
        self._config_error: PartnerError | None = None
        self._backend: Any = None
        try:
            self._config = load_partner_config(partner_id)
        except PartnerError as e:
            self._config_error = e

    def server_info(self) -> dict[str, str]:
        return {"name": self.partner_id or "quant-partner", "version": "0.1.0"}

    def secrets(self) -> list[str]:
        values = self._config.secrets() if self._config else []
        backend_secrets = getattr(self._backend, "secrets", []) if self._backend else []
        return values + [str(x) for x in backend_secrets if x]

    def _cfg(self) -> PartnerConfig:
        if self._config_error:
            raise self._config_error
        if not self._config:
            raise invalid_config("partner config is not loaded")
        return self._config

    def _backend_instance(self) -> Any:
        cfg = self._cfg()
        if self._backend is None:
            if cfg.backend == "copilot-direct":
                self._backend = CopilotDirectBackend(cfg.auth, cfg.options, cfg.timeout_seconds)
            elif cfg.backend == "gemini":
                self._backend = GeminiBackend(cfg.auth, cfg.options, cfg.timeout_seconds)
            elif cfg.backend == "openai-compatible":
                self._backend = OpenAICompatibleBackend(cfg.auth, cfg.options, cfg.timeout_seconds)
            else:
                raise invalid_config(f"unsupported backend: {cfg.backend}")
        return self._backend

    def _tool_names(self) -> tuple[str, str]:
        cfg = self._config
        toolset = cfg.toolset if cfg else ("code" if "copilot" in self.partner_id else "product")
        if toolset == "code":
            return "ask_code_partner", "ask_code_partner_dialog"
        if toolset == "product":
            return "ask_product_partner", "ask_product_partner_dialog"
        return "ask_partner", "ask_partner_dialog"

    @staticmethod
    def _string_arg(description: str) -> dict[str, str]:
        return {"type": "string", "description": description}

    def tools(self) -> list[dict[str, Any]]:
        ask, dialog = self._tool_names()
        common_properties = {
            "skill": self._string_arg("Optional skill name from partners/skills/<partner>/<skill>"),
            "model": self._string_arg("Optional model override"),
            "role": self._string_arg("Optional named model role from config.models"),
        }
        return [
            {
                "name": "partner_info",
                "description": "Show partner backend, default model, active skill and auth status without exposing secrets.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_partner_models",
                "description": "List models actually available to the configured account and plan.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_partner_skills",
                "description": "List pluggable skill packs installed for this partner.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": ask,
                "description": "Ask this partner a single stateless question. Pass enough context; there is no memory between calls.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "question": self._string_arg("Question, hypothesis or review request"),
                        "context": self._string_arg("Relevant architecture, constraints, snippets and prior findings"),
                        "files": {
                            "type": "array",
                            "description": "Optional file excerpts: [{name, content}]",
                            "items": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                                "required": ["name", "content"],
                            },
                        },
                        **common_properties,
                    },
                    "required": ["question"],
                },
            },
            {
                "name": dialog,
                "description": "Continue a partner dialog with message history [{role:'user'|'assistant'|'model', text:'...'}].",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "messages": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string", "enum": ["user", "assistant", "model"]},
                                    "text": {"type": "string"},
                                },
                                "required": ["role", "text"],
                            },
                        },
                        **common_properties,
                    },
                    "required": ["messages"],
                },
            },
        ]

    def _partner_info(self) -> str:
        cfg = self._cfg()
        skills = list_skills(cfg)
        try:
            active = load_skill(cfg).name
        except PartnerError:
            active = cfg.default_skill
        info = {
            "partner_id": cfg.partner_id,
            "toolset": cfg.toolset,
            "backend": cfg.backend,
            "model": cfg.default_model(),
            "default_skill": cfg.default_skill,
            "active_skill": active,
            "installed_skills": [s.name for s in skills],
            "auth_configured": cfg.auth_configured(),
            "config": str(cfg.config_path),
            "harness": str(cfg.harness_path),
        }
        return json.dumps(info, ensure_ascii=False, indent=2)

    def _skills_text(self) -> str:
        cfg = self._cfg()
        skills = list_skills(cfg)
        if not skills:
            return "No skills installed."
        lines = []
        for skill in skills:
            suffix = f" When: {skill.when_to_use}" if skill.when_to_use else ""
            lines.append(f"{skill.name} - {skill.description}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _question_content(question: str, context: str, files: list[dict[str, str]]) -> str:
        parts = [question]
        if context:
            parts.append("## Context\n" + context)
        for item in files:
            name = str(item.get("name", "file"))
            content = str(item.get("content", ""))
            if content:
                parts.append(f"## File: {name}\n```\n{content}\n```")
        return "\n\n".join(parts)

    def _candidate_models(self, cfg: PartnerConfig, skill: Skill, args: dict[str, Any]) -> list[str]:
        explicit = str(args.get("model") or "")
        role = str(args.get("role") or "")
        primary = explicit or skill.model or cfg.default_model(role) or cfg.model
        candidates = [primary]
        candidates.extend(skill.fallback_models)
        candidates.extend(cfg.model_fallbacks())
        result = []
        for model in candidates:
            if model and model not in result:
                result.append(model)
        return result

    def _complete(self, messages: list[dict[str, str]], args: dict[str, Any]) -> str:
        cfg = self._cfg()
        backend = self._backend_instance()
        skill = load_skill(cfg, str(args.get("skill") or ""))
        system_prompt = compose_system_prompt(cfg, skill)
        generation = dict(cfg.generation)
        generation.update(skill.generation)
        last_error: PartnerError | None = None
        for model in self._candidate_models(cfg, skill, args):
            try:
                return backend.complete(
                    messages,
                    system_prompt=system_prompt,
                    model=model,
                    generation=generation,
                )
            except PartnerError as e:
                last_error = e
                if e.code in ("unauthorized", "invalid_config", "invalid_request"):
                    break
        if last_error:
            raise last_error
        raise PartnerError("backend_error", "no model candidates configured")

    def _ask(self, args: dict[str, Any]) -> str:
        question = str(args.get("question") or "").strip()
        if not question:
            raise PartnerError("invalid_request", "question is required")
        files = args.get("files") or []
        if not isinstance(files, list):
            raise PartnerError("invalid_request", "files must be an array")
        content = self._question_content(question, str(args.get("context") or ""), files)
        return self._complete([{"role": "user", "content": content}], args)

    def _dialog(self, args: dict[str, Any]) -> str:
        raw = args.get("messages")
        if not isinstance(raw, list) or not raw:
            raise PartnerError("invalid_request", "messages must be a non-empty array")
        messages = []
        for item in raw:
            if not isinstance(item, dict):
                raise PartnerError("invalid_request", "each message must be an object")
            role = str(item.get("role") or "")
            text = str(item.get("text") or item.get("content") or "")
            if role not in ("user", "assistant", "model") or not text:
                raise PartnerError("invalid_request", "messages require role user|assistant|model and text")
            messages.append({"role": "assistant" if role == "model" else role, "content": text})
        return self._complete(messages, args)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "partner_info":
            return self._partner_info()
        if name == "list_partner_models":
            models = self._backend_instance().list_models()
            return "\n".join(models) if models else "No models returned by backend."
        if name == "list_partner_skills":
            return self._skills_text()
        ask, dialog = self._tool_names()
        if name == ask or name == "ask_partner":
            return self._ask(arguments)
        if name == dialog or name == "ask_partner_dialog":
            return self._dialog(arguments)
        raise PartnerError("invalid_request", f"unknown tool: {name}")

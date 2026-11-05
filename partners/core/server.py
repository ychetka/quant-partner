#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Partner-aware MCP tool dispatch."""

from __future__ import annotations

import json
from typing import Any

from backends.base import CompletionResult
from backends.copilot_direct import CopilotDirectBackend
from backends.gemini import GeminiBackend
from backends.openai_compatible import OpenAICompatibleBackend
from review.service import PARTNER_VERSION, ReviewService, failure_text

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
        return {"name": self.partner_id or "quant-partner", "version": PARTNER_VERSION}

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

    def _toolset(self) -> str:
        cfg = self._config
        if cfg:
            return cfg.toolset
        return "code" if "copilot" in self.partner_id else "product"

    def _tool_names(self) -> tuple[str, str]:
        toolset = self._toolset()
        if toolset == "code":
            return "ask_code_partner", "ask_code_partner_dialog"
        if toolset == "product":
            return "ask_product_partner", "ask_product_partner_dialog"
        return "ask_partner", "ask_partner_dialog"

    @staticmethod
    def _string_arg(description: str) -> dict[str, str]:
        return {"type": "string", "description": description}

    def _review_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "partner_info",
                "description": (
                    "Show partner backend, default model, active skill and auth status "
                    "without exposing secrets."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_partner_skills",
                "description": "List pluggable skill packs installed for this partner.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_reviewer_models",
                "description": (
                    "List allowlisted reviewer model aliases, their pinned backend "
                    "model IDs and configured limits. Advisory metadata only: no "
                    "provider access and no secrets."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "review_code",
                "description": (
                    "Run one advisory second-opinion review of a unified diff and "
                    "return a strict JSON artifact. Output is advisory only and "
                    "never authorizes merge or publishes anything."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "head_sha": self._string_arg(
                            "PR head commit SHA, exactly 40 hex characters"
                        ),
                        "base_sha": self._string_arg(
                            "PR base commit SHA, exactly 40 hex characters"
                        ),
                        "diff": self._string_arg("Unified diff of the reviewed change"),
                        "model": self._string_arg(
                            "Allowlisted model alias from list_reviewer_models "
                            "(free-form provider IDs are rejected)"
                        ),
                        "skill": self._string_arg(
                            "Optional installed review skill name from "
                            "list_partner_skills (default: configured default_skill)"
                        ),
                        "strict_model": {
                            "type": "boolean",
                            "description": (
                                "Fail closed unless provider metadata confirms the "
                                "requested model served the response (default true)"
                            ),
                        },
                        "context": self._string_arg(
                            "Optional review context; treated as untrusted content"
                        ),
                        "files": {
                            "type": "array",
                            "description": (
                                "Optional bounded context entries: safe repo-relative "
                                "path strings used as labels, or objects "
                                "{name: relative/path, content: inline text}. "
                                "Contents are caller-supplied; the server never reads "
                                "the filesystem."
                            ),
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "content": {"type": "string"},
                                        },
                                        "required": ["name"],
                                    },
                                ]
                            },
                        },
                        "review_id": self._string_arg(
                            "Optional caller correlation id; regenerated when absent or invalid"
                        ),
                    },
                    "required": ["head_sha", "base_sha", "diff", "model"],
                },
            },
        ]

    def tools(self) -> list[dict[str, Any]]:
        if self._toolset() == "review":
            return self._review_tools()
        ask, dialog = self._tool_names()
        common_properties = {
            "skill": self._string_arg("Optional skill name from partners/skills/<partner>/<skill>"),
            "model": self._string_arg("Optional model override"),
            "role": self._string_arg("Optional named model role from config.models"),
        }
        ask_schema = {
            "type": "object",
            "properties": {
                "question": self._string_arg("Question, hypothesis or review request"),
                "context": self._string_arg(
                    "Relevant architecture, constraints, snippets and prior findings"
                ),
                "files": {
                    "type": "array",
                    "description": "Optional file excerpts: [{name, content}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["name", "content"],
                    },
                },
                **common_properties,
            },
            "required": ["question"],
        }
        return [
            {
                "name": "partner_info",
                "description": (
                    "Show partner backend, default model, active skill and auth status "
                    "without exposing secrets."
                ),
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
                "description": (
                    "Ask this partner a single stateless question. Pass enough context; "
                    "there is no memory between calls."
                ),
                "inputSchema": ask_schema,
            },
            {
                "name": f"{ask}_v2",
                "description": (
                    "Ask this partner a single stateless question like the base ask "
                    "tool, but return a JSON object with the answer text plus model "
                    "provenance (model_requested, model_used, model_match)."
                ),
                "inputSchema": ask_schema,
            },
            {
                "name": dialog,
                "description": (
                    "Continue a partner dialog with message history "
                    "[{role:'user'|'assistant'|'model', text:'...'}]."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "messages": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {
                                        "type": "string",
                                        "enum": ["user", "assistant", "model"],
                                    },
                                    "text": {"type": "string"},
                                },
                                "required": ["role", "text"],
                            },
                        },
                        "context": self._string_arg(
                            "Optional caller-supplied context/context-pack brief; "
                            "added as a leading user context turn before the "
                            "message history"
                        ),
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

    def _candidate_models(
        self, cfg: PartnerConfig, skill: Skill, args: dict[str, Any]
    ) -> list[str]:
        explicit = str(args.get("model") or "")
        role = str(args.get("role") or "")
        if role and role not in cfg.models:
            raise PartnerError("invalid_request", f"unknown model role: {role}")
        role_model = cfg.default_model(role) if role else ""
        primary = explicit or role_model or skill.model or cfg.default_model() or cfg.model
        candidates = [primary]
        candidates.extend(skill.fallback_models)
        candidates.extend(cfg.model_fallbacks())
        result = []
        for model in candidates:
            if model and model not in result:
                result.append(model)
        return result

    def _complete_ex(
        self, messages: list[dict[str, str]], args: dict[str, Any]
    ) -> tuple[str, str, str | None, Skill]:
        """One completion pass returning text plus model provenance.

        ``model_requested`` is the candidate that produced the answer;
        ``model_used`` is provider-reported via ``complete_ex`` when the
        backend exposes it and stays ``None`` for plain ``complete``
        backends. Candidate order and the error break conditions match
        the legacy ask loop exactly.
        """
        cfg = self._cfg()
        backend = self._backend_instance()
        skill = load_skill(cfg, str(args.get("skill") or ""))
        system_prompt = compose_system_prompt(cfg, skill)
        generation = dict(cfg.generation)
        generation.update(skill.generation)
        complete_ex = getattr(backend, "complete_ex", None)
        last_error: PartnerError | None = None
        for model in self._candidate_models(cfg, skill, args):
            try:
                if not callable(complete_ex):
                    return (
                        backend.complete(
                            messages,
                            system_prompt=system_prompt,
                            model=model,
                            generation=generation,
                        ),
                        model,
                        None,
                        skill,
                    )
                result = complete_ex(
                    messages,
                    system_prompt=system_prompt,
                    model=model,
                    generation=generation,
                )
                # No str() coercion: anything but a CompletionResult with
                # non-empty str text is the same backend_error a plain
                # ``complete`` backend raises for an empty response, and
                # text=None must never become the literal string "None".
                text = getattr(result, "text", None)
                if (
                    not isinstance(result, CompletionResult)
                    or not isinstance(text, str)
                    or not text.strip()
                ):
                    raise PartnerError("backend_error", "backend returned an empty response")
                model_used = getattr(result, "model_used", None)
                return text, model, model_used if isinstance(model_used, str) else None, skill
            except PartnerError as e:
                last_error = e
                if e.code in ("unauthorized", "invalid_config", "invalid_request"):
                    break
        if last_error:
            raise last_error
        raise PartnerError("backend_error", "no model candidates configured")

    def _complete(self, messages: list[dict[str, str]], args: dict[str, Any]) -> str:
        return self._complete_ex(messages, args)[0]

    def _ask_messages(self, args: dict[str, Any]) -> list[dict[str, str]]:
        """Validated ``ask_*`` input rendered as one user message."""
        question = str(args.get("question") or "").strip()
        if not question:
            raise PartnerError("invalid_request", "question is required")
        files = args.get("files") or []
        if not isinstance(files, list):
            raise PartnerError("invalid_request", "files must be an array")
        content = self._question_content(question, str(args.get("context") or ""), files)
        return [{"role": "user", "content": content}]

    def _ask(self, args: dict[str, Any]) -> str:
        return self._complete(self._ask_messages(args), args)

    def _ask_v2(self, args: dict[str, Any]) -> str:
        """Structured ask answer: text plus honest model provenance."""
        cfg = self._cfg()
        text, model_requested, model_used, skill = self._complete_ex(self._ask_messages(args), args)
        model_used = model_used or None
        payload = {
            "partner_id": cfg.partner_id,
            "skill": skill.name or None,
            "model_requested": model_requested,
            "model_used": model_used,
            "model_match": None if model_used is None else model_used == model_requested,
            "text": text,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

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
                raise PartnerError(
                    "invalid_request", "messages require role user|assistant|model and text"
                )
            messages.append({"role": "assistant" if role == "model" else role, "content": text})
        context = str(args.get("context") or "")
        if context:
            messages.insert(0, {"role": "user", "content": "## Context\n" + context})
        return self._complete(messages, args)

    def _review_service(self) -> ReviewService:
        cfg = self._cfg()
        backend = self._backend_instance()
        return ReviewService(cfg, backend, secrets=self.secrets())

    def _list_reviewer_models(self) -> str:
        cfg = self._cfg()
        return ReviewService(cfg).list_reviewer_models()

    def _review_code(self, arguments: Any) -> str:
        try:
            cfg = self._cfg()
        except PartnerError as e:
            return failure_text(code=e.code, message=e.message, partner_id=self.partner_id)
        except Exception:
            return failure_text(
                code="internal_error",
                message="partner configuration failed",
                partner_id=self.partner_id,
            )
        try:
            service = self._review_service()
        except PartnerError as e:
            return failure_text(
                code=e.code, message=e.message, partner_id=cfg.partner_id, backend=cfg.backend
            )
        except Exception:
            return failure_text(
                code="internal_error",
                message="review service construction failed",
                partner_id=cfg.partner_id,
                backend=cfg.backend,
            )
        try:
            return service.review_code(arguments)
        except Exception:
            # review_code already never raises by contract; this is the
            # last-resort guard that keeps the always-artifact contract.
            return failure_text(
                code="internal_error",
                message="review pipeline failed",
                partner_id=cfg.partner_id,
                backend=cfg.backend,
            )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "partner_info":
            return self._partner_info()
        if name == "list_partner_skills":
            return self._skills_text()
        if self._toolset() == "review":
            if name == "list_reviewer_models":
                return self._list_reviewer_models()
            if name == "review_code":
                return self._review_code(arguments)
            raise PartnerError("invalid_request", f"unknown tool: {name}")
        if name == "review_code" and self._config_error is not None:
            # Config could not be loaded, so the toolset is unknown; still
            # answer with a serialized failure artifact, never an exception.
            return self._review_code(arguments)
        if name == "list_partner_models":
            models = self._backend_instance().list_models()
            return "\n".join(models) if models else "No models returned by backend."
        ask, dialog = self._tool_names()
        if name == ask or name == "ask_partner":
            return self._ask(arguments)
        if name == f"{ask}_v2" or name == "ask_partner_v2":
            return self._ask_v2(arguments)
        if name == dialog or name == "ask_partner_dialog":
            return self._dialog(arguments)
        raise PartnerError("invalid_request", f"unknown tool: {name}")

#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Partner-aware MCP tool dispatch."""

from __future__ import annotations

import json
import sys
import time
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
    config_path,
    list_skills,
    load_partner_config,
    load_skill,
)
from .errors import PartnerError, invalid_config, sanitize


class PartnerMCPServer:
    def __init__(self, partner_id: str) -> None:
        self.partner_id = partner_id
        self._config: PartnerConfig | None = None
        self._config_error: PartnerError | None = None
        self._config_reload_error: str | None = None
        self._backend: Any = None
        self._call_meta: dict[str, Any] = {}
        # The resolved config path is stored once: hot-reload watches this
        # exact file, so a later PARTNER_CONFIG env change cannot redirect
        # a running process at a different config.
        self._config_path = config_path()
        self._config_stamp = self._stat_config_stamp()
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

    def _log_event(self, record: dict[str, Any]) -> None:
        """Emit one sanitized JSON event line to stderr; logging never fails a call."""
        try:
            line = sanitize(json.dumps(record, ensure_ascii=False, sort_keys=True), self.secrets())
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass

    def _stat_config_stamp(self) -> tuple[int, int] | None:
        """``(st_mtime_ns, st_size)`` of the resolved config path.

        Both fields form the change key so timestamp-preserving writes
        that alter the file size still trigger a reload; ``None`` means
        the file cannot be stat-ed (missing or unreadable).
        """
        try:
            stat = self._config_path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    @staticmethod
    def _reload_error_reason(error: BaseException | None) -> str:
        """Stable short reason code for a failed config reload.

        The value is also what ``partner_info`` reports as
        ``config_reload_error``:

        - ``unreadable``: the file is missing or cannot be stat-ed/read
          (``error`` is ``None`` or an ``OSError``);
        - ``decode_error``: the file bytes are not UTF-8
          (``UnicodeDecodeError``) or fail JSON decoding before the
          config validator can wrap them;
        - the ``PartnerError.code`` itself (for example
          ``invalid_config``) for a readable file that fails validation;
        - ``internal_error``: any other unexpected exception.
        """
        if error is None or isinstance(error, OSError):
            return "unreadable"
        if isinstance(error, PartnerError):
            return error.code or "invalid_config"
        if isinstance(error, (UnicodeDecodeError, json.JSONDecodeError)):
            return "decode_error"
        return "internal_error"

    def _warn_config_reload(self, error: BaseException | None = None) -> None:
        """Record and emit a structured warning for a failed config reload.

        ``reason`` carries the stable code from ``_reload_error_reason``;
        ``error_class`` names the exception type for debugging and is
        ``None`` when the file simply cannot be stat-ed.
        """
        reason = self._reload_error_reason(error)
        self._config_reload_error = reason
        self._log_event(
            {
                "event": "config_reload",
                "partner_id": self.partner_id,
                "config": str(self._config_path),
                "reason": reason,
                "error_class": type(error).__name__ if error is not None else None,
                "outcome": "error",
            }
        )

    def _maybe_reload(self) -> None:
        """Hot-reload the partner config when the file stamp changed.

        The stored ``PARTNER_CONFIG`` path is stat-ed once per check; a
        changed ``(st_mtime_ns, st_size)`` pair re-runs
        ``load_partner_config`` on that exact path. A successful reload
        swaps in the new config and drops the cached backend so fresh
        auth/options take effect immediately; ANY failed reload -
        invalid JSON, non-UTF-8 bytes, permission errors or unexpected
        exceptions - keeps the last valid config, records
        ``_config_reload_error`` and emits one ``config_reload`` warning
        line to stderr - it never switches the server into an error
        state. A missing or unreadable file keeps the cached config and
        warns once per transition (``_config_stamp`` is reset to None
        so a reappearing file is picked up again).
        """
        stamp = self._stat_config_stamp()
        if stamp == self._config_stamp:
            return
        self._config_stamp = stamp
        if stamp is None:
            self._warn_config_reload()
            return
        try:
            cfg = load_partner_config(self.partner_id, self._config_path)
        except Exception as e:
            # Broad catch by design: ``load_partner_config`` can raise
            # beyond PartnerError/OSError (for example UnicodeDecodeError
            # on non-UTF-8 bytes), and any reload failure must keep
            # serving the previous valid config instead of breaking
            # every subsequent call.
            self._warn_config_reload(e)
            return
        self._config = cfg
        self._config_error = None
        self._config_reload_error = None
        self._backend = None

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
        """Configured toolset, or a degraded guess when config failed to load.

        Without a loaded config the real toolset is unknown, so the server
        falls back to ``code`` for partner ids containing ``copilot`` and
        to ``product`` for every other id. The fallback keeps ``tools()``
        and ``call_tool`` dispatching on a stable surface in degraded mode.
        """
        cfg = self._config
        if cfg:
            return cfg.toolset
        return "code" if "copilot" in self.partner_id else "product"

    def _legacy_ask_prefix(self) -> str | None:
        """Legacy question-tool prefix of the current toolset.

        Every non-review toolset answers on the canonical ``ask_partner*``
        names. The ``code`` and ``product`` toolsets additionally keep
        their historical ``ask_code_partner*`` / ``ask_product_partner*``
        names callable as legacy aliases of the canonical tools; other
        configured toolsets return ``None``.

        The prefix follows ``_toolset()``, so when the config fails to
        load it derives from the degraded partner-id fallback (``code``
        for ids containing ``copilot``, ``product`` otherwise) instead of
        returning ``None`` - the degraded ``tools()`` surface still
        advertises the corresponding legacy aliases.
        """
        toolset = self._toolset()
        if toolset == "code":
            return "ask_code_partner"
        if toolset == "product":
            return "ask_product_partner"
        return None

    def _canonical_ask_name(self, name: str) -> str:
        """Map a legacy ask alias of this toolset to its canonical name.

        ``ask_code_partner_dialog`` on a ``code`` toolset becomes
        ``ask_partner_dialog``; names outside this toolset's alias family
        pass through unchanged and fail as unknown tools downstream.
        """
        legacy = self._legacy_ask_prefix()
        if legacy and name.startswith(legacy):
            return "ask_partner" + name[len(legacy) :]
        return name

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
        self._maybe_reload()
        if self._toolset() == "review":
            return self._review_tools()
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
        dialog_schema = {
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
        }
        ask_tools = [
            (
                "ask_partner",
                "Ask this partner a single stateless question. Pass enough context; "
                "there is no memory between calls.",
                ask_schema,
            ),
            (
                "ask_partner_v2",
                "Ask this partner a single stateless question like the base ask "
                "tool, but return a JSON object with the answer text plus model "
                "provenance (model_requested, model_used, model_match).",
                ask_schema,
            ),
            (
                "ask_partner_dialog",
                "Continue a partner dialog with message history "
                "[{role:'user'|'assistant'|'model', text:'...'}].",
                dialog_schema,
            ),
        ]
        tools = [
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
        ]
        for name, description, schema in ask_tools:
            tools.append({"name": name, "description": description, "inputSchema": schema})
        legacy = self._legacy_ask_prefix()
        if legacy:
            for name, description, schema in ask_tools:
                tools.append(
                    {
                        "name": legacy + name[len("ask_partner") :],
                        "description": f"{description} Legacy alias of {name}.",
                        "inputSchema": schema,
                    }
                )
        return tools

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
            "config_reload_error": self._config_reload_error,
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
        # Resolved skill name for the per-call request log line; model
        # fields are filled in by the successful return paths below.
        self._call_meta["skill"] = skill.name
        system_prompt = compose_system_prompt(cfg, skill)
        generation = dict(cfg.generation)
        generation.update(skill.generation)
        complete_ex = getattr(backend, "complete_ex", None)
        last_error: PartnerError | None = None
        for model in self._candidate_models(cfg, skill, args):
            try:
                if not callable(complete_ex):
                    text = backend.complete(
                        messages,
                        system_prompt=system_prompt,
                        model=model,
                        generation=generation,
                    )
                    self._call_meta.update({"model_requested": model, "model_used": None})
                    return text, model, None, skill
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
                served = model_used if isinstance(model_used, str) else None
                self._call_meta.update({"model_requested": model, "model_used": served})
                return text, model, served, skill
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

    @staticmethod
    def _review_outcome(text: str) -> str:
        """Map a serialized ``review_code`` artifact to the call outcome.

        ``review_code`` keeps an always-artifact contract: even failures
        return a JSON payload instead of raising, so the transport-level
        outcome is derived from the artifact ``status``. Known statuses
        (``ok``, ``error``, ``rejected``) pass through unchanged; an
        unparseable payload or a missing/unknown status is logged as
        ``unknown``.
        """
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return "unknown"
        status = payload.get("status") if isinstance(payload, dict) else None
        return status if status in ("ok", "error", "rejected") else "unknown"

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch one tool call: config hot-reload, timing, request log.

        Exactly one ``partner_call`` JSON line goes to stderr per call
        with the tool name, resolved skill/model metadata when
        determinable, latency and outcome. Outcome is ``ok`` on success,
        the ``PartnerError.code`` on a raised partner error and
        ``internal_error`` on unexpected exceptions; ``review_code``
        never raises, so its outcome is mapped from the returned
        artifact ``status`` (``ok``/``error``/``rejected``, or
        ``unknown`` for an unrecognizable payload). Question, context
        and file contents are never logged; the line is sanitized
        against ``secrets()`` before writing.
        """
        self._maybe_reload()
        self._call_meta = {}
        start = time.monotonic()
        outcome = "ok"
        try:
            text = self._dispatch(name, arguments)
        except PartnerError as e:
            outcome = e.code
            raise
        except Exception:
            outcome = "internal_error"
            raise
        else:
            if name == "review_code":
                outcome = self._review_outcome(text)
            return text
        finally:
            self._log_event(
                {
                    "event": "partner_call",
                    "partner_id": self.partner_id,
                    "tool": name,
                    "skill": self._call_meta.get("skill"),
                    "model_requested": self._call_meta.get("model_requested"),
                    "model_used": self._call_meta.get("model_used"),
                    "latency_ms": int((time.monotonic() - start) * 1000),
                    "outcome": outcome,
                }
            )

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> str:
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
        canonical = self._canonical_ask_name(name)
        if canonical == "ask_partner":
            return self._ask(arguments)
        if canonical == "ask_partner_v2":
            return self._ask_v2(arguments)
        if canonical == "ask_partner_dialog":
            return self._dialog(arguments)
        raise PartnerError("invalid_request", f"unknown tool: {name}")

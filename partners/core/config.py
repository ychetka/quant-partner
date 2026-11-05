#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Partner JSON config, harness and pluggable skill loading."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PartnerError, invalid_config

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config.json"
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PLACEHOLDERS = ("REPLACE", "TOKEN_HERE", "KEY_HERE", "YOUR_", "EXAMPLE")
_ENV_AUTH_PREFIX = "QP_AUTH_"
_ENV_MODEL_PREFIX = "QP_MODEL_"
_KNOWN_AUTH_KEYS = ("github_oauth_token", "gemini_api_key", "api_key")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    when_to_use: str
    prompt: str
    manifest_path: Path
    model: str = ""
    version: str = ""
    fallback_models: list[str] = field(default_factory=list)
    generation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PartnerConfig:
    partner_id: str
    toolset: str
    backend: str
    enabled: bool
    harness_path: Path
    default_skill: str
    skills_root: str
    auth: dict[str, Any] = field(repr=False)
    options: dict[str, Any]
    model: str
    models: dict[str, Any]
    fallback_models: list[str]
    generation: dict[str, Any]
    timeout_seconds: int
    config_path: Path
    package_root: Path = PACKAGE_ROOT

    def secrets(self) -> list[str]:
        values: list[str] = []
        for value in self.auth.values():
            if isinstance(value, str) and value and not is_placeholder(value):
                values.append(value)
        return values

    def auth_configured(self) -> bool:
        return bool(self.secrets())

    def default_model(self, role: str = "") -> str:
        if role and isinstance(self.models.get(role), str):
            return str(self.models[role])
        if isinstance(self.models.get("default"), str):
            return str(self.models["default"])
        return self.model

    def model_fallbacks(self) -> list[str]:
        values = list(self.fallback_models)
        extra = self.models.get("fallback")
        if isinstance(extra, list):
            values.extend(str(x) for x in extra if x)
        return values


def is_placeholder(value: str) -> bool:
    """True for empty values, explicit marker texts and env-var-style names.

    Besides the marker substrings in ``_PLACEHOLDERS``, a bare
    environment-variable-style name - uppercase letters, digits,
    underscores or hyphens only, at least six characters, with at least
    one letter - counts as a placeholder (for example ``GITHUB_TOKEN``
    or ``MY_API_KEY`` left over from a template). Real provider
    credentials are almost never shaped like that: GitHub tokens start
    ``ghp_``/``github_pat_``, OpenAI keys start ``sk`` and Google keys are
    mixed-case (``AIza...``), so the conservative all-uppercase rule does
    not swallow them. A rare genuinely all-uppercase token would be a
    false positive; supply it through a ``QP_AUTH_*`` env override or
    rename it in that case.
    """
    upper = value.upper()
    if not value or any(marker in upper for marker in _PLACEHOLDERS):
        return True
    # Env-var-name heuristic: all-uppercase identifier-shaped values are
    # placeholder names, not secrets. ``value == upper`` guarantees every
    # letter is uppercase; the charset check rejects tokens containing
    # punctuation such as ``.`` or ``=``.
    return (
        len(value) >= 6
        and value == upper
        and any(c.isalpha() for c in value)
        and all(c.isalnum() or c in "_-" for c in value)
    )


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("PARTNER_CONFIG", str(DEFAULT_CONFIG_PATH))).expanduser()


def _expect_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid_config(f"{name} must be an object")
    return value


def _expect_string(value: Any, name: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise invalid_config(f"{name} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise invalid_config(f"{name} must be an array of non-empty strings")
    return list(value)


def _safe_relative_path(value: str, name: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise invalid_config(f"{name} must be a relative path inside partners/")
    return str(path)


def _env_token(name: str) -> str:
    """Uppercase a partner id or auth key for use in an environment variable name."""
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def _env_token_collisions(names: list[str], what: str) -> None:
    """Reject names that normalize to the same env token, naming only the names."""
    seen: dict[str, str] = {}
    for name in names:
        token = _env_token(name)
        other = seen.get(token)
        if other is not None and other != name:
            raise invalid_config(f"{what} normalize to the same env token: {other} and {name}")
        seen[token] = name


def _auth_env_name_collisions(partners: Mapping[str, Any]) -> None:
    """Reject ``(partner_id, auth_key)`` pairs that share one ``QP_AUTH_`` env name.

    Concatenating two normalized tokens can collide across partners even when
    neither token collides on its own: ``foo`` + ``bar_baz`` and ``foo_bar``
    + ``baz`` both yield ``QP_AUTH_FOO_BAR_BAZ``. Every configured partner's
    known and declared auth keys are therefore checked together. A missing
    ``auth`` is treated as ``{}``; a non-object ``auth`` is skipped because
    loading that partner already rejects it. Only identifiers are reported,
    never values.
    """
    seen: dict[str, tuple[str, str]] = {}
    for pid, raw in partners.items():
        auth = raw.get("auth", {}) if isinstance(raw, dict) else {}
        if not isinstance(auth, dict):
            auth = {}
        auth_keys: list[Any] = list(_KNOWN_AUTH_KEYS)
        for key in auth:
            if key not in auth_keys:
                auth_keys.append(key)
        scope = _env_token(str(pid))
        for key in auth_keys:
            env_name = f"{_ENV_AUTH_PREFIX}{scope}_{_env_token(str(key))}"
            pair = (str(pid), str(key))
            other = seen.get(env_name)
            if other is not None and other != pair:
                raise invalid_config(
                    "auth env name collision: "
                    f"{other[0]}.{other[1]} and {pair[0]}.{pair[1]}"
                    f" both map to {env_name}"
                )
            seen[env_name] = pair


def _env_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _apply_env_overrides(
    partner_id: str,
    auth: dict[str, Any],
    model: str,
    models: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Overlay ``QP_AUTH_``/``QP_MODEL_`` env values on file config without mutating it.

    A non-empty value wins over the file config; empty or whitespace-only
    values are ignored. Auth overrides use ``QP_AUTH_<PARTNER_ID>_<AUTH_KEY>``
    and cover the known backend keys plus any key already declared in the
    file auth object. A non-empty ``QP_MODEL_<PARTNER_ID>`` also overlays
    ``models.default`` so that ``PartnerConfig.default_model()`` sees it,
    while role-specific entries such as ``models.utility`` and
    ``models.fallback`` are preserved.
    """
    env = os.environ if environ is None else environ
    scope = _env_token(partner_id)
    auth_keys: list[str] = list(_KNOWN_AUTH_KEYS)
    for key in auth:
        if key not in auth_keys:
            auth_keys.append(key)
    _env_token_collisions([str(key) for key in auth_keys], "auth keys")
    merged_auth = dict(auth)
    for key in auth_keys:
        value = _env_value(env, f"{_ENV_AUTH_PREFIX}{scope}_{_env_token(str(key))}")
        if value:
            merged_auth[key] = value
    merged_models = dict(models)
    env_model = _env_value(env, f"{_ENV_MODEL_PREFIX}{scope}")
    if env_model:
        merged_models["default"] = env_model
    return merged_auth, env_model or model, merged_models


def load_partner_config(
    partner_id: str,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> PartnerConfig:
    path = path or config_path(environ)
    if not partner_id:
        raise invalid_config("PARTNER_ID is not set")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise invalid_config(
            f"config file not found: {path}",
            "copy partners/config.example.json to partners/config.json and fill secrets",
        ) from e
    except json.JSONDecodeError as e:
        raise invalid_config(f"config file is not valid JSON: {e}") from e

    data = _expect_mapping(data, "config root")
    partners = _expect_mapping(data.get("partners"), "partners")
    _env_token_collisions([str(pid) for pid in partners], "partner ids")
    _auth_env_name_collisions(partners)
    raw = _expect_mapping(partners.get(partner_id), f"partners.{partner_id}")
    enabled = bool(raw.get("enabled", True))
    if not enabled:
        raise invalid_config(f"partner is disabled: {partner_id}")
    backend = _expect_string(raw.get("backend"), "backend")
    toolset = _expect_string(raw.get("toolset"), "toolset")
    harness = _safe_relative_path(_expect_string(raw.get("harness"), "harness"), "harness")
    default_skill = _expect_string(raw.get("default_skill"), "default_skill")
    skills_root_raw = raw.get("skills_root", "")
    if not isinstance(skills_root_raw, str):
        raise invalid_config("skills_root must be a string")
    skills_root = _safe_relative_path(skills_root_raw, "skills_root") if skills_root_raw else ""
    auth = _expect_mapping(raw.get("auth", {}), "auth")
    generation = _expect_mapping(raw.get("generation", {}), "generation")
    models = _expect_mapping(raw.get("models", {}), "models")
    options = _expect_mapping(raw.get("options", {}), "options")
    timeout = raw.get("timeout_seconds", 240)
    if not isinstance(timeout, int) or timeout <= 0:
        raise invalid_config("timeout_seconds must be a positive integer")
    auth, model, models = _apply_env_overrides(
        partner_id,
        auth,
        _expect_string(raw.get("model"), "model", ""),
        models,
        environ,
    )

    return PartnerConfig(
        partner_id=partner_id,
        toolset=toolset,
        backend=backend,
        enabled=enabled,
        harness_path=(PACKAGE_ROOT / harness).resolve(),
        default_skill=default_skill,
        skills_root=skills_root,
        auth=auth,
        options=options,
        model=model,
        models=models,
        fallback_models=_string_list(raw.get("fallback_models"), "fallback_models"),
        generation=generation,
        timeout_seconds=timeout,
        config_path=path,
    )


def strip_license_header(text: str) -> str:
    """Remove repository license comments before a prompt reaches a model."""
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip().lower()
        if not line or line in ("<!--", "-->"):
            idx += 1
            continue
        if "copyright" in line or "spdx-license-identifier" in line:
            idx += 1
            continue
        break
    return "\n".join(lines[idx:]).strip()


def load_harness(cfg: PartnerConfig) -> str:
    try:
        text = cfg.harness_path.read_text(encoding="utf-8")
    except OSError as e:
        raise invalid_config(f"harness not found: {cfg.harness_path}") from e
    text = strip_license_header(text)
    if not text:
        raise invalid_config(f"harness is empty: {cfg.harness_path}")
    return text


def _skills_root(cfg: PartnerConfig) -> Path:
    if cfg.skills_root:
        return cfg.package_root / cfg.skills_root
    return cfg.package_root / "skills" / cfg.partner_id


def _safe_skill_name(name: str) -> str:
    name = name.strip()
    if not _SAFE_NAME.match(name):
        raise PartnerError("invalid_config", f"invalid skill name: {name}")
    return name


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise invalid_config(f"cannot load skill manifest {path}: {e}") from e
    return _expect_mapping(manifest, f"skill manifest {path}")


def list_skills(cfg: PartnerConfig) -> list[Skill]:
    root = _skills_root(cfg)
    if not root.is_dir():
        return []
    skills = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            skills.append(load_skill(cfg, directory.name))
    return skills


def load_skill(cfg: PartnerConfig, name: str | None = None) -> Skill:
    skill_name = _safe_skill_name(name or cfg.default_skill)
    directory = _skills_root(cfg) / skill_name
    manifest_path = directory / "manifest.json"
    prompt_path = directory / "prompt.txt"
    manifest = _load_manifest(manifest_path)
    try:
        prompt = strip_license_header(prompt_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise invalid_config(f"skill prompt not found: {prompt_path}") from e
    if not prompt:
        raise invalid_config(f"skill prompt is empty: {prompt_path}")
    return Skill(
        name=skill_name,
        description=str(manifest.get("description", "")),
        when_to_use=str(manifest.get("when_to_use", "")),
        prompt=prompt,
        manifest_path=manifest_path,
        model=str(manifest.get("model", "")),
        version=str(manifest.get("version") or ""),
        fallback_models=_string_list(manifest.get("fallback_models"), "skill fallback_models"),
        generation=_expect_mapping(manifest.get("generation", {}), "skill generation"),
    )


def compose_system_prompt(cfg: PartnerConfig, skill: Skill) -> str:
    return load_harness(cfg) + "\n\n" + skill.prompt

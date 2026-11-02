#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from core.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    compose_system_prompt,
    config_path,
    list_skills,
    load_partner_config,
    load_skill,
    strip_license_header,
)
from core.errors import PartnerError  # noqa: E402


def _partner_section(**overrides: Any) -> dict[str, Any]:
    section = {
        "enabled": True,
        "toolset": "code",
        "backend": "openai-compatible",
        "harness": "harness/gh-copilot-partner.txt",
        "default_skill": "software-architect",
        "auth": {},
        "model": "file-model",
    }
    section.update(overrides)
    return section


def _write_config(directory: Path, partners: dict[str, Any]) -> Path:
    path = directory / "config.json"
    path.write_text(json.dumps({"partners": partners}), encoding="utf-8")
    return path


class ConfigTest(unittest.TestCase):
    def test_copilot_example_config(self):
        cfg = load_partner_config(
            "gh-copilot-partner", ROOT / "partners" / "config.example.json", environ={}
        )
        self.assertEqual(cfg.backend, "copilot-direct")
        self.assertEqual(cfg.toolset, "code")
        self.assertEqual(cfg.default_skill, "software-architect")
        self.assertEqual(cfg.default_model(), "claude-sonnet-5")
        self.assertFalse(cfg.auth_configured())

    def test_gemini_example_config(self):
        cfg = load_partner_config(
            "gemini-product", ROOT / "partners" / "config.example.json", environ={}
        )
        self.assertEqual(cfg.backend, "gemini")
        self.assertEqual(cfg.default_model(), "gemini-4-argon")
        self.assertEqual(cfg.default_model("utility"), "gemini-3.5-flash")

    def test_skills_and_prompt_composition(self):
        cfg = load_partner_config(
            "gh-copilot-partner", ROOT / "partners" / "config.example.json", environ={}
        )
        names = [skill.name for skill in list_skills(cfg)]
        self.assertIn("software-architect", names)
        self.assertIn("polyglot-developer", names)
        skill = load_skill(cfg, "software-architect")
        prompt = compose_system_prompt(cfg, skill)
        self.assertIn("senior software architect", prompt)
        self.assertIn("Skill: Software Architect", prompt)
        self.assertNotIn("SPDX-License-Identifier", prompt)

    def test_non_object_config_root_is_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(PartnerError) as ctx:
                load_partner_config("gh-copilot-partner", path, environ={})
        self.assertEqual(ctx.exception.code, "invalid_config")

    def test_non_object_skill_manifest_is_invalid_config(self):
        cfg = load_partner_config(
            "gh-copilot-partner", ROOT / "partners" / "config.example.json", environ={}
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "gh-copilot-partner" / "broken"
            skill_dir.mkdir(parents=True)
            (skill_dir / "manifest.json").write_text("[]", encoding="utf-8")
            (skill_dir / "prompt.txt").write_text("prompt", encoding="utf-8")
            cfg = replace(cfg, package_root=root)
            with self.assertRaises(PartnerError) as ctx:
                load_skill(cfg, "broken")
        self.assertEqual(ctx.exception.code, "invalid_config")

    def test_local_openai_partner_uses_shared_skills_root(self):
        data = json.loads((ROOT / "partners" / "config.example.json").read_text(encoding="utf-8"))
        data["partners"]["local-openai-partner"]["enabled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            cfg = load_partner_config("local-openai-partner", path, environ={})
        self.assertEqual(cfg.skills_root, "skills/gh-copilot-partner")
        self.assertIn("software-architect", [skill.name for skill in list_skills(cfg)])

    def test_strip_license_keeps_prompt_heading(self):
        text = "# Copyright x\n# SPDX-License-Identifier: MIT\n\n# Skill: Test\nBody"
        self.assertEqual(strip_license_header(text), "# Skill: Test\nBody")


class EnvOverrideTest(unittest.TestCase):
    def _load(
        self,
        partner_id: str,
        section: dict[str, Any],
        environ: dict[str, str],
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp), {partner_id: section})
            return load_partner_config(partner_id, path, environ=environ)

    def test_env_provides_known_auth_key_absent_from_file(self):
        cfg = self._load(
            "gh-copilot-partner",
            _partner_section(),
            {"QP_AUTH_GH_COPILOT_PARTNER_GITHUB_OAUTH_TOKEN": "env-oauth-token"},
        )
        self.assertEqual(cfg.auth["github_oauth_token"], "env-oauth-token")
        self.assertTrue(cfg.auth_configured())

    def test_env_auth_overrides_file_value(self):
        cfg = self._load(
            "local-openai-partner",
            _partner_section(auth={"api_key": "file-key"}),
            {"QP_AUTH_LOCAL_OPENAI_PARTNER_API_KEY": "env-key"},
        )
        self.assertEqual(cfg.auth["api_key"], "env-key")

    def test_env_overrides_declared_custom_auth_key(self):
        cfg = self._load(
            "local-openai-partner",
            _partner_section(auth={"custom_header": "file-value"}),
            {"QP_AUTH_LOCAL_OPENAI_PARTNER_CUSTOM_HEADER": "env-value"},
        )
        self.assertEqual(cfg.auth["custom_header"], "env-value")

    def test_env_auth_key_not_declared_is_not_added(self):
        cfg = self._load(
            "local-openai-partner",
            _partner_section(auth={"api_key": "file-key"}),
            {"QP_AUTH_LOCAL_OPENAI_PARTNER_SOMETHING_ELSE": "env-value"},
        )
        self.assertNotIn("something_else", cfg.auth)
        self.assertEqual(cfg.auth["api_key"], "file-key")

    def test_env_model_overrides_file_model(self):
        cfg = self._load(
            "gh-copilot-partner",
            _partner_section(),
            {"QP_MODEL_GH_COPILOT_PARTNER": "env-model"},
        )
        self.assertEqual(cfg.model, "env-model")
        self.assertEqual(cfg.default_model(), "env-model")

    def test_env_model_overrides_models_default(self):
        cfg = self._load(
            "gemini-product",
            _partner_section(
                models={
                    "default": "file-default",
                    "utility": "file-utility",
                    "fallback": ["file-fallback"],
                }
            ),
            {"QP_MODEL_GEMINI_PRODUCT": "env-model"},
        )
        self.assertEqual(cfg.model, "env-model")
        self.assertEqual(cfg.default_model(), "env-model")
        self.assertEqual(cfg.default_model("utility"), "file-utility")
        self.assertEqual(cfg.models["fallback"], ["file-fallback"])

    def test_no_env_keeps_models_default(self):
        cfg = self._load(
            "gemini-product",
            _partner_section(models={"default": "file-default", "utility": "file-utility"}),
            {},
        )
        self.assertEqual(cfg.model, "file-model")
        self.assertEqual(cfg.default_model(), "file-default")
        self.assertEqual(cfg.default_model("utility"), "file-utility")

    def test_empty_env_values_are_ignored(self):
        cfg = self._load(
            "local-openai-partner",
            _partner_section(auth={"api_key": "file-key"}),
            {
                "QP_AUTH_LOCAL_OPENAI_PARTNER_API_KEY": "   ",
                "QP_MODEL_LOCAL_OPENAI_PARTNER": "",
            },
        )
        self.assertEqual(cfg.auth["api_key"], "file-key")
        self.assertEqual(cfg.model, "file-model")

    def test_env_overrides_are_partner_scoped(self):
        cfg = self._load(
            "gh-copilot-partner",
            _partner_section(auth={"api_key": "file-key"}),
            {
                "QP_AUTH_GEMINI_PRODUCT_GEMINI_API_KEY": "env-key",
                "QP_MODEL_GEMINI_PRODUCT": "env-model",
            },
        )
        self.assertNotIn("gemini_api_key", cfg.auth)
        self.assertEqual(cfg.auth["api_key"], "file-key")
        self.assertEqual(cfg.model, "file-model")

    def test_repr_does_not_leak_secrets(self):
        cfg = self._load(
            "gh-copilot-partner",
            _partner_section(auth={"api_key": "file-secret-value"}),
            {"QP_AUTH_GH_COPILOT_PARTNER_GITHUB_OAUTH_TOKEN": "env-secret-value"},
        )
        text = repr(cfg)
        self.assertNotIn("file-secret-value", text)
        self.assertNotIn("env-secret-value", text)
        self.assertNotIn("auth=", text)

    def test_no_env_keeps_file_values(self):
        cfg = self._load(
            "local-openai-partner",
            _partner_section(auth={"api_key": "file-key"}),
            {},
        )
        self.assertEqual(cfg.auth["api_key"], "file-key")
        self.assertEqual(cfg.model, "file-model")

    def test_missing_auth_allows_env_only_auth(self):
        section = _partner_section()
        del section["auth"]
        cfg = self._load(
            "local-openai-partner",
            section,
            {"QP_AUTH_LOCAL_OPENAI_PARTNER_API_KEY": "env-key"},
        )
        self.assertEqual(cfg.auth["api_key"], "env-key")
        self.assertTrue(cfg.auth_configured())

    def test_missing_auth_without_env_is_empty(self):
        section = _partner_section()
        del section["auth"]
        cfg = self._load("local-openai-partner", section, {})
        self.assertEqual(cfg.auth, {})
        self.assertFalse(cfg.auth_configured())

    def test_non_object_auth_is_invalid_config(self):
        with self.assertRaises(PartnerError) as ctx:
            self._load("local-openai-partner", _partner_section(auth="nope"), {})
        self.assertEqual(ctx.exception.code, "invalid_config")

    def test_partner_id_env_token_collision_is_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                Path(tmp),
                {
                    "foo-bar": _partner_section(),
                    "foo_bar": _partner_section(),
                },
            )
            with self.assertRaises(PartnerError) as ctx:
                load_partner_config("foo-bar", path, environ={})
        self.assertEqual(ctx.exception.code, "invalid_config")
        self.assertIn("foo-bar", ctx.exception.message)
        self.assertIn("foo_bar", ctx.exception.message)
        self.assertNotIn("file-model", ctx.exception.message)

    def test_auth_key_env_token_collision_is_invalid_config(self):
        with self.assertRaises(PartnerError) as ctx:
            self._load(
                "local-openai-partner",
                _partner_section(auth={"my-key": "value-a", "my_key": "value-b"}),
                {},
            )
        self.assertEqual(ctx.exception.code, "invalid_config")
        self.assertIn("my-key", ctx.exception.message)
        self.assertIn("my_key", ctx.exception.message)
        self.assertNotIn("value-a", ctx.exception.message)
        self.assertNotIn("value-b", ctx.exception.message)

    def test_os_environ_is_used_when_environ_is_not_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp), {"local-openai-partner": _partner_section()})
            with patch.dict(
                os.environ,
                {
                    "QP_AUTH_LOCAL_OPENAI_PARTNER_API_KEY": "env-key",
                    "QP_MODEL_LOCAL_OPENAI_PARTNER": "env-model",
                },
                clear=True,
            ):
                cfg = load_partner_config("local-openai-partner", path)
        self.assertEqual(cfg.auth["api_key"], "env-key")
        self.assertEqual(cfg.model, "env-model")

    def test_config_path_uses_injected_environ(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom.json"
            self.assertEqual(config_path({"PARTNER_CONFIG": str(custom)}), custom)
            self.assertEqual(config_path({}), DEFAULT_CONFIG_PATH)

    def test_load_partner_config_reads_partner_config_from_environ(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(Path(tmp), {"local-openai-partner": _partner_section()})
            cfg = load_partner_config("local-openai-partner", environ={"PARTNER_CONFIG": str(path)})
        self.assertEqual(cfg.config_path, path)
        self.assertEqual(cfg.backend, "openai-compatible")


if __name__ == "__main__":
    unittest.main()

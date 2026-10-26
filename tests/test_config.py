#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from core.config import (  # noqa: E402
    compose_system_prompt,
    list_skills,
    load_partner_config,
    load_skill,
    strip_license_header,
)
from core.errors import PartnerError  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_copilot_example_config(self):
        cfg = load_partner_config("gh-copilot-partner", ROOT / "partners" / "config.example.json")
        self.assertEqual(cfg.backend, "copilot-direct")
        self.assertEqual(cfg.toolset, "code")
        self.assertEqual(cfg.default_skill, "software-architect")
        self.assertEqual(cfg.default_model(), "claude-sonnet-5")
        self.assertFalse(cfg.auth_configured())

    def test_gemini_example_config(self):
        cfg = load_partner_config("gemini-product", ROOT / "partners" / "config.example.json")
        self.assertEqual(cfg.backend, "gemini")
        self.assertEqual(cfg.default_model(), "gemini-4-argon")
        self.assertEqual(cfg.default_model("utility"), "gemini-3.5-flash")

    def test_skills_and_prompt_composition(self):
        cfg = load_partner_config("gh-copilot-partner", ROOT / "partners" / "config.example.json")
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
                load_partner_config("gh-copilot-partner", path)
        self.assertEqual(ctx.exception.code, "invalid_config")

    def test_non_object_skill_manifest_is_invalid_config(self):
        cfg = load_partner_config("gh-copilot-partner", ROOT / "partners" / "config.example.json")
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
            cfg = load_partner_config("local-openai-partner", path)
        self.assertEqual(cfg.skills_root, "skills/gh-copilot-partner")
        self.assertIn("software-architect", [skill.name for skill in list_skills(cfg)])

    def test_strip_license_keeps_prompt_heading(self):
        text = "# Copyright x\n# SPDX-License-Identifier: MIT\n\n# Skill: Test\nBody"
        self.assertEqual(strip_license_header(text), "# Skill: Test\nBody")


if __name__ == "__main__":
    unittest.main()

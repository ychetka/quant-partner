#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import sys
import unittest
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

    def test_strip_license_keeps_prompt_heading(self):
        text = "# Copyright x\n# SPDX-License-Identifier: MIT\n\n# Skill: Test\nBody"
        self.assertEqual(strip_license_header(text), "# Skill: Test\nBody")


if __name__ == "__main__":
    unittest.main()

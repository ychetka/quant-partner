#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from core.config import (  # noqa: E402
    compose_system_prompt,
    list_skills,
    load_partner_config,
    load_skill,
)
from core.server import PartnerMCPServer  # noqa: E402

SKILL_NAME = "release-manager"
SKILL_DIR = ROOT / "partners" / "skills" / "gemini-product" / SKILL_NAME


def _gemini_cfg():
    return load_partner_config(
        "gemini-product", ROOT / "partners" / "config.example.json", environ={}
    )


class ReleaseManagerSkillTest(unittest.TestCase):
    def test_manifest_is_discovered_and_loaded(self):
        cfg = _gemini_cfg()
        self.assertIn(SKILL_NAME, [skill.name for skill in list_skills(cfg)])
        skill = load_skill(cfg, SKILL_NAME)
        self.assertEqual(skill.name, SKILL_NAME)
        self.assertTrue(skill.description)
        self.assertTrue(skill.when_to_use)
        self.assertTrue(skill.prompt)

    def test_manifest_shape_and_generation(self):
        manifest = json.loads((SKILL_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], SKILL_NAME)
        self.assertEqual(manifest["version"], 1)
        self.assertTrue(manifest["_license"])
        description = manifest["description"].lower()
        self.assertIn("release", description)
        self.assertIn("changelog", description)
        self.assertIn("semver", description)
        temperature = manifest["generation"]["temperature"]
        self.assertEqual(temperature, 0.3)

    def test_list_partner_skills_surfaces_skill(self):
        env = {"PARTNER_CONFIG": str(ROOT / "partners" / "config.example.json")}
        with patch.dict(os.environ, env):
            server = PartnerMCPServer("gemini-product")
        self.assertIn(SKILL_NAME, server.call_tool("list_partner_skills", {}))

    def test_composed_prompt_includes_skill_and_strips_license(self):
        cfg = _gemini_cfg()
        skill = load_skill(cfg, SKILL_NAME)
        prompt = compose_system_prompt(cfg, skill)
        self.assertIn("Release Manager", prompt)
        self.assertIn(skill.prompt, prompt)
        self.assertNotIn("Copyright", prompt)
        self.assertNotIn("SPDX-License-Identifier", prompt)
        self.assertNotIn("Copyright", skill.prompt)
        self.assertNotIn("SPDX-License-Identifier", skill.prompt)

    def test_prompt_release_sections_and_semver_signals(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt.lower()
        for section in (
            "summary",
            "highlights",
            "changes",
            "risks",
            "suggested semver bump",
        ):
            self.assertIn(section, prompt)
        self.assertIn("release notes", prompt)
        self.assertIn("changelog", prompt)
        self.assertIn("patch", prompt)
        self.assertIn("minor", prompt)
        self.assertIn("major", prompt)

    def test_prompt_advisory_only_never_runs_release_actions(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt.lower()
        self.assertIn("advisory only", prompt)
        self.assertIn("never publish", prompt)
        self.assertIn("tags", prompt)
        self.assertIn("never merge", prompt)
        self.assertIn("draft", prompt)

    def test_prompt_safety_and_missing_material_clauses(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt.lower()
        self.assertIn("secret", prompt)
        self.assertIn("personal data", prompt)
        self.assertIn("never invent", prompt)
        self.assertIn("ask", prompt)
        self.assertIn("english", prompt)
        self.assertIn("untrusted", prompt)

    def test_prompt_is_prose_not_strict_json_artifact(self):
        # This skill runs on the conversational product toolset: unlike the
        # review artifact skills it must not demand a strict JSON object,
        # which would conflict with the prose gemini-product harness.
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt.lower()
        self.assertNotIn("strict json", prompt)
        self.assertNotIn("schema_version", prompt)
        self.assertNotIn("artifact", prompt)


if __name__ == "__main__":
    unittest.main()

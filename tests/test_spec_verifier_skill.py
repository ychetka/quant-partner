#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import json
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
)

SKILL_NAME = "spec-compliance-verifier"
SKILL_DIR = ROOT / "partners" / "skills" / "gemini-product" / SKILL_NAME


def _gemini_cfg():
    return load_partner_config(
        "gemini-product", ROOT / "partners" / "config.example.json", environ={}
    )


class SpecVerifierSkillTest(unittest.TestCase):
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
        temperature = manifest["generation"]["temperature"]
        self.assertGreaterEqual(temperature, 0.0)
        self.assertLessEqual(temperature, 0.2)

    def test_composed_prompt_includes_verifier_and_strips_license(self):
        cfg = _gemini_cfg()
        skill = load_skill(cfg, SKILL_NAME)
        prompt = compose_system_prompt(cfg, skill)
        self.assertIn("Spec Compliance Verifier", prompt)
        self.assertIn("consulting partner", prompt)
        self.assertNotIn("Copyright", prompt)
        self.assertNotIn("SPDX-License-Identifier", prompt)
        self.assertNotIn("Copyright", skill.prompt)
        self.assertNotIn("SPDX-License-Identifier", skill.prompt)

    def test_prompt_advisory_artifact_contract(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt
        self.assertIn('"advisory": true', prompt)
        self.assertIn('"schema_version"', prompt)
        self.assertIn("spec_drift", prompt)
        self.assertIn('"unreviewed"', prompt)
        self.assertIn("JSON", prompt)

    def test_prompt_safety_clauses(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt.lower()
        self.assertIn("advisory only", prompt)
        self.assertIn("merge approval", prompt)
        self.assertIn("assumption", prompt)
        self.assertIn("secret", prompt)
        self.assertIn("personal data", prompt)
        self.assertIn("untrusted", prompt)
        self.assertIn("publish", prompt)

    def test_prompt_describes_caller_materials_and_placeholders(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt.lower()
        self.assertIn("spec", prompt)
        self.assertIn("acceptance criteria", prompt)
        self.assertIn("diff summary", prompt)
        self.assertIn("code excerpts", prompt)
        self.assertIn("placeholder", prompt)
        self.assertIn("server", prompt)

    def test_prompt_sha_fallback_uses_valid_zero_hex(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt
        zero_sha = "0" * 40
        self.assertIn(f'"head_sha": "{zero_sha}"', prompt)
        self.assertIn(f'"base_sha": "{zero_sha}"', prompt)
        self.assertNotIn("server-assigned-or-caller-supplied", prompt)
        lowered = prompt.lower()
        self.assertIn("40-hex", lowered)
        self.assertIn("provenance gap", lowered)
        self.assertIn("warnings", lowered)
        self.assertIn("unreviewed", lowered)

    def test_prompt_max_findings_deterministic_fallback(self):
        prompt = load_skill(_gemini_cfg(), SKILL_NAME).prompt
        self.assertIn("max_findings", prompt)
        self.assertIn("defaults to 20", prompt)


if __name__ == "__main__":
    unittest.main()

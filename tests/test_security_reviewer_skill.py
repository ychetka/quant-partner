#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import json
import re
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
from review.schema import extract_json, validate  # noqa: E402

SKILL_NAME = "security-reviewer"
SKILL_DIR = ROOT / "partners" / "skills" / "code-reviewer" / SKILL_NAME


def _reviewer_cfg():
    return load_partner_config(
        "code-reviewer", ROOT / "partners" / "config.example.json", environ={}
    )


class SecurityReviewerSkillTest(unittest.TestCase):
    def test_manifest_is_discovered_and_loaded(self):
        cfg = _reviewer_cfg()
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

    def test_composed_prompt_includes_skill_and_strips_license(self):
        cfg = _reviewer_cfg()
        skill = load_skill(cfg, SKILL_NAME)
        prompt = compose_system_prompt(cfg, skill)
        self.assertIn("Security Reviewer", prompt)
        self.assertIn(skill.prompt, prompt)
        self.assertNotIn("Copyright", prompt)
        self.assertNotIn("SPDX-License-Identifier", prompt)
        self.assertNotIn("Copyright", skill.prompt)
        self.assertNotIn("SPDX-License-Identifier", skill.prompt)

    def test_prompt_advisory_artifact_contract(self):
        prompt = load_skill(_reviewer_cfg(), SKILL_NAME).prompt
        self.assertIn('"advisory": true', prompt)
        self.assertIn('"schema_version"', prompt)
        self.assertIn('"unreviewed"', prompt)
        self.assertIn("security", prompt)
        self.assertIn("JSON", prompt)
        for reason in (
            "schema",
            "path_not_in_diff",
            "line_not_in_diff",
            "secret",
            "duplicate",
            "over_limit",
        ):
            self.assertIn(f"`{reason}`", prompt)

    def test_embedded_json_template_is_schema_valid(self):
        prompt = load_skill(_reviewer_cfg(), SKILL_NAME).prompt
        match = re.search(r"```json[^\n]*\n.*?\n```", prompt, re.DOTALL)
        if match is None:
            self.fail("prompt.txt must embed a fenced ```json artifact template")
        artifact = extract_json(match.group(0))
        self.assertEqual(validate(artifact), [])

    def test_prompt_safety_clauses(self):
        prompt = load_skill(_reviewer_cfg(), SKILL_NAME).prompt.lower()
        self.assertIn("advisory only", prompt)
        self.assertIn("merge approval", prompt)
        self.assertIn("publish", prompt)
        self.assertIn("secret", prompt)
        self.assertIn("personal data", prompt)
        self.assertIn("untrusted", prompt)
        self.assertIn("defensive", prompt)

    def test_prompt_defensive_stance_bans_exploit_guidance(self):
        prompt = load_skill(_reviewer_cfg(), SKILL_NAME).prompt.lower()
        self.assertIn("exploit", prompt)
        self.assertIn("never provide", prompt)
        self.assertIn("weaponization", prompt)

    def test_prompt_lists_security_focus_areas(self):
        prompt = load_skill(_reviewer_cfg(), SKILL_NAME).prompt.lower()
        for term in (
            "injection",
            "authentication",
            "authorization",
            "deserialization",
            "path traversal",
            "ssrf",
            "redirect",
            "cryptographic",
            "randomness",
            "supply-chain",
        ):
            self.assertIn(term, prompt)

    def test_prompt_sha_fallback_uses_valid_zero_hex(self):
        prompt = load_skill(_reviewer_cfg(), SKILL_NAME).prompt
        zero_sha = "0" * 40
        self.assertIn(f'"head_sha": "{zero_sha}"', prompt)
        self.assertIn(f'"base_sha": "{zero_sha}"', prompt)
        lowered = prompt.lower()
        self.assertIn("40-hex", lowered)
        self.assertIn("warnings", lowered)

    def test_prompt_prefers_security_category_and_caps_findings(self):
        prompt = load_skill(_reviewer_cfg(), SKILL_NAME).prompt
        self.assertIn("`security`", prompt)
        self.assertIn('"RIGHT"', prompt)
        self.assertIn("max_findings", prompt)
        self.assertIn("defaults to 20", prompt)


if __name__ == "__main__":
    unittest.main()

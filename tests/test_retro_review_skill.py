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

SKILL_NAME = "retro-review"
SKILL_DIR = ROOT / "partners" / "skills" / "comms-partner" / SKILL_NAME


def _comms_cfg():
    return load_partner_config(
        "comms-partner", ROOT / "partners" / "config.example.json", environ={}
    )


class RetroReviewSkillTest(unittest.TestCase):
    def test_manifest_is_discovered_and_loaded(self):
        cfg = _comms_cfg()
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
        self.assertIn("retro", description)
        self.assertIn("ledger", description)
        self.assertIn("decision", description)
        temperature = manifest["generation"]["temperature"]
        self.assertEqual(temperature, 0.3)

    def test_list_partner_skills_surfaces_skill(self):
        env = {"PARTNER_CONFIG": str(ROOT / "partners" / "config.example.json")}
        with patch.dict(os.environ, env):
            server = PartnerMCPServer("comms-partner")
        self.assertIn(SKILL_NAME, server.call_tool("list_partner_skills", {}))

    def test_composed_prompt_includes_skill_and_strips_license(self):
        cfg = _comms_cfg()
        skill = load_skill(cfg, SKILL_NAME)
        prompt = compose_system_prompt(cfg, skill)
        self.assertIn("Retro Review", prompt)
        self.assertIn(skill.prompt, prompt)
        self.assertNotIn("Copyright", prompt)
        self.assertNotIn("SPDX-License-Identifier", prompt)
        self.assertNotIn("Copyright", skill.prompt)
        self.assertNotIn("SPDX-License-Identifier", skill.prompt)

    def test_prompt_covers_outcome_marks_and_patterns(self):
        prompt = load_skill(_comms_cfg(), SKILL_NAME).prompt.lower()
        for outcome in ("validated", "regretted", "pending"):
            self.assertIn(outcome, prompt)
        for field in (
            "decision_id",
            "orchestrator_action",
            "adopted",
            "modified",
            "rejected",
            "pattern",
        ):
            self.assertIn(field, prompt)

    def test_prompt_honesty_and_advisory_boundaries(self):
        prompt = load_skill(_comms_cfg(), SKILL_NAME).prompt.lower()
        self.assertIn("advisory only", prompt)
        self.assertIn("never invent", prompt)
        self.assertIn("untrusted", prompt)
        self.assertIn("stateless", prompt)
        self.assertIn("english", prompt)
        # The ledger contract is append-only: the skill must never claim
        # it edits, appends to or deletes from the file it reviews.
        self.assertIn("never edit", prompt)
        self.assertIn("never append", prompt)
        self.assertIn("never delete", prompt)


if __name__ == "__main__":
    unittest.main()

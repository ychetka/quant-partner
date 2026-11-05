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

CONFIG_EXAMPLE = ROOT / "partners" / "config.example.json"

ADVISORY_PARTNERS = {
    "pm-partner": {
        "harness": "harness/pm-partner.txt",
        "default_skill": "product-manager",
        "skills_root": "skills/pm-partner",
        "temperature": 0.4,
    },
    "market-partner": {
        "harness": "harness/market-partner.txt",
        "default_skill": "market-synthesis",
        "skills_root": "skills/market-partner",
        "temperature": 0.3,
    },
    "strategy-partner": {
        "harness": "harness/strategy-partner.txt",
        "default_skill": "strategy-consultant",
        "skills_root": "skills/strategy-partner",
        "temperature": 0.4,
    },
    "comms-partner": {
        "harness": "harness/comms-partner.txt",
        "default_skill": "communication",
        "skills_root": "skills/comms-partner",
        "temperature": 0.4,
    },
}

GENERIC_TOOLS = {
    "partner_info",
    "list_partner_models",
    "list_partner_skills",
    "ask_partner",
    "ask_partner_v2",
    "ask_partner_dialog",
}

FORBIDDEN_TOOLS = {
    "ask_code_partner",
    "ask_code_partner_v2",
    "ask_code_partner_dialog",
    "ask_product_partner",
    "ask_product_partner_v2",
    "ask_product_partner_dialog",
    "review_code",
    "list_reviewer_models",
}


def _cfg(partner_id):
    return load_partner_config(partner_id, CONFIG_EXAMPLE, environ={})


class AdvisoryConfigTest(unittest.TestCase):
    def test_example_config_is_valid_json(self):
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        for partner_id in ADVISORY_PARTNERS:
            self.assertIn(partner_id, data["partners"])

    def test_advisory_partners_load(self):
        for partner_id, spec in ADVISORY_PARTNERS.items():
            with self.subTest(partner=partner_id):
                cfg = _cfg(partner_id)
                self.assertTrue(cfg.enabled)
                self.assertEqual(cfg.toolset, "advisory")
                self.assertEqual(cfg.backend, "gemini")
                self.assertEqual(cfg.skills_root, spec["skills_root"])
                self.assertEqual(cfg.default_skill, spec["default_skill"])
                self.assertTrue(cfg.harness_path.is_file())
                self.assertEqual(cfg.harness_path, (ROOT / "partners" / spec["harness"]).resolve())
                self.assertEqual(cfg.default_model(), "gemini-4-argon")
                self.assertEqual(cfg.default_model("utility"), "gemini-3.5-flash")
                self.assertEqual(cfg.model_fallbacks(), ["gemini-3.8-flash", "gemini-3.5-flash"])
                self.assertEqual(cfg.generation["temperature"], spec["temperature"])
                self.assertEqual(cfg.timeout_seconds, 240)
                self.assertFalse(cfg.auth_configured())

    def test_default_skill_discovered_per_partner(self):
        for partner_id, spec in ADVISORY_PARTNERS.items():
            with self.subTest(partner=partner_id):
                cfg = _cfg(partner_id)
                names = [skill.name for skill in list_skills(cfg)]
                self.assertIn(spec["default_skill"], names)
                skill = load_skill(cfg)
                self.assertEqual(skill.name, spec["default_skill"])
                self.assertTrue(skill.description)
                self.assertTrue(skill.when_to_use)
                self.assertTrue(skill.prompt)


class AdvisoryToolSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PARTNER_CONFIG": str(CONFIG_EXAMPLE)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_generic_advisory_tools_exposed(self):
        for partner_id in ADVISORY_PARTNERS:
            with self.subTest(partner=partner_id):
                server = PartnerMCPServer(partner_id)
                names = {tool["name"] for tool in server.tools()}
                self.assertTrue(GENERIC_TOOLS <= names)
                for forbidden in FORBIDDEN_TOOLS:
                    self.assertNotIn(forbidden, names)

    def test_partner_info_reports_advisory_toolset(self):
        for partner_id, spec in ADVISORY_PARTNERS.items():
            with self.subTest(partner=partner_id):
                server = PartnerMCPServer(partner_id)
                info = json.loads(server.call_tool("partner_info", {}))
                self.assertEqual(info["toolset"], "advisory")
                self.assertEqual(info["backend"], "gemini")
                self.assertEqual(info["default_skill"], spec["default_skill"])
                self.assertEqual(info["active_skill"], spec["default_skill"])
                self.assertIn(spec["default_skill"], info["installed_skills"])
                self.assertFalse(info["auth_configured"])

    def test_list_partner_skills_surfaces_default_skill(self):
        for partner_id, spec in ADVISORY_PARTNERS.items():
            with self.subTest(partner=partner_id):
                server = PartnerMCPServer(partner_id)
                self.assertIn(
                    spec["default_skill"],
                    server.call_tool("list_partner_skills", {}),
                )


class AdvisoryHarnessTest(unittest.TestCase):
    def _harness(self, partner_id):
        return (ROOT / "partners" / "harness" / f"{partner_id}.txt").read_text(encoding="utf-8")

    def test_market_harness_honest_capability_clause(self):
        text = self._harness("market-partner").lower()
        self.assertIn("no live data", text)
        self.assertIn("web access", text)
        self.assertIn("monitoring", text)
        self.assertIn("caller", text)
        self.assertIn("tam", text)
        self.assertIn("estimation", text)

    def test_comms_harness_stateless_context_pack_clause(self):
        text = self._harness("comms-partner").lower()
        self.assertIn("stateless", text)
        self.assertIn("context-pack", text)
        self.assertIn("context", text)
        self.assertIn("memory", text)
        self.assertIn("socratic", text)

    def test_pm_harness_pm_clauses(self):
        text = self._harness("pm-partner").lower()
        for clause in (
            "value proposition canvas",
            "mvp",
            "rice",
            "ice",
            "moscow",
            "ltv",
            "cac",
            "churn",
            "roi",
            "ask",
        ):
            self.assertIn(clause, text)

    def test_strategy_harness_framework_clauses(self):
        text = self._harness("strategy-partner").lower()
        for clause in (
            "swot",
            "pestel",
            "porter",
            "bcg",
            "risk register",
            "confidence",
            "assumptions",
        ):
            self.assertIn(clause, text)

    def test_harnesses_strip_license_and_answer_format(self):
        for partner_id in ADVISORY_PARTNERS:
            with self.subTest(partner=partner_id):
                text = self._harness(partner_id)
                self.assertIn("SPDX-License-Identifier: MIT", text)
                self.assertIn("## Answer format", text)
                self.assertIn("unless the question requires another shape", text)
                cfg = _cfg(partner_id)
                prompt = compose_system_prompt(cfg, load_skill(cfg))
                self.assertNotIn("SPDX-License-Identifier", prompt)


if __name__ == "__main__":
    unittest.main()

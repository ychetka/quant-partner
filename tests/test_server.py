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

from core.config import load_skill  # noqa: E402
from core.errors import PartnerError, sanitize  # noqa: E402
from core.server import PartnerMCPServer  # noqa: E402


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"PARTNER_CONFIG": str(ROOT / "partners" / "config.example.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_code_tools(self):
        server = PartnerMCPServer("gh-copilot-partner")
        names = {tool["name"] for tool in server.tools()}
        self.assertIn("partner_info", names)
        self.assertIn("list_partner_models", names)
        self.assertIn("list_partner_skills", names)
        self.assertIn("ask_code_partner", names)
        self.assertIn("ask_code_partner_dialog", names)

    def test_product_tools(self):
        server = PartnerMCPServer("gemini-product")
        names = {tool["name"] for tool in server.tools()}
        self.assertIn("ask_product_partner", names)
        self.assertIn("ask_product_partner_dialog", names)

    def test_partner_info(self):
        server = PartnerMCPServer("gh-copilot-partner")
        info = json.loads(server.call_tool("partner_info", {}))
        self.assertEqual(info["backend"], "copilot-direct")
        self.assertEqual(info["active_skill"], "software-architect")
        self.assertFalse(info["auth_configured"])
        self.assertIn("polyglot-developer", info["installed_skills"])

    def test_list_skills(self):
        server = PartnerMCPServer("gemini-product")
        text = server.call_tool("list_partner_skills", {})
        self.assertIn("product-manager", text)
        self.assertIn("ux-architect", text)

    def test_missing_question_is_human_error(self):
        server = PartnerMCPServer("gh-copilot-partner")
        with self.assertRaises(PartnerError) as ctx:
            server.call_tool("ask_code_partner", {})
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_explicit_role_beats_skill_model(self):
        server = PartnerMCPServer("gemini-product")
        cfg = server._cfg()
        skill = load_skill(cfg, "prioritization")
        models = server._candidate_models(cfg, skill, {"role": "default"})
        self.assertEqual(models[0], "gemini-4-argon")

    def test_unknown_role_is_invalid_request(self):
        server = PartnerMCPServer("gemini-product")
        cfg = server._cfg()
        skill = load_skill(cfg, "product-manager")
        with self.assertRaises(PartnerError) as ctx:
            server._candidate_models(cfg, skill, {"role": "missing"})
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_secret_sanitizer(self):
        self.assertEqual(sanitize("token abc123 failed", ["abc123"]), "token [redacted] failed")


if __name__ == "__main__":
    unittest.main()

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
    _safe_relative_path,
    compose_system_prompt,
    list_skills,
    load_partner_config,
    load_skill,
)
from core.errors import PartnerError  # noqa: E402
from core.server import PartnerMCPServer  # noqa: E402

CONFIG_EXAMPLE = ROOT / "partners" / "config.example.json"
PARTNERS_DOC = ROOT / "docs" / "PARTNERS.md"

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
        # clear=True keeps stray QP_AUTH_*/QP_MODEL_* overrides from the
        # ambient environment out of auth_configured assertions.
        self.env = patch.dict(os.environ, {"PARTNER_CONFIG": str(CONFIG_EXAMPLE)}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_generic_advisory_tools_exposed(self):
        # The advisory toolset advertises exactly the canonical surface:
        # no code/product legacy aliases are added for these partners.
        for partner_id in ADVISORY_PARTNERS:
            with self.subTest(partner=partner_id):
                server = PartnerMCPServer(partner_id)
                names = {tool["name"] for tool in server.tools()}
                self.assertEqual(names, GENERIC_TOOLS)
                for forbidden in FORBIDDEN_TOOLS:
                    self.assertNotIn(forbidden, names)

    def test_advisory_rejects_legacy_alias_names(self):
        for partner_id in ADVISORY_PARTNERS:
            with self.subTest(partner=partner_id):
                server = PartnerMCPServer(partner_id)
                for name in (
                    "ask_code_partner",
                    "ask_code_partner_v2",
                    "ask_code_partner_dialog",
                    "ask_product_partner",
                    "ask_product_partner_v2",
                    "ask_product_partner_dialog",
                ):
                    with self.subTest(partner=partner_id, name=name):
                        with self.assertRaises(PartnerError) as ctx:
                            server.call_tool(name, {"question": "q?"})
                        self.assertEqual(ctx.exception.code, "invalid_request")

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


REVIEW_ONLY_TOOLS = ("review_code", "list_reviewer_models")

ASK_TOOL_NAMES = (
    "ask_partner",
    "ask_partner_v2",
    "ask_partner_dialog",
    "ask_code_partner",
    "ask_code_partner_v2",
    "ask_code_partner_dialog",
    "ask_product_partner",
    "ask_product_partner_v2",
    "ask_product_partner_dialog",
)


class PartnersDocTest(unittest.TestCase):
    """docs/PARTNERS.md must cover every configured partner and installed skill."""

    def _doc_text(self):
        return PARTNERS_DOC.read_text(encoding="utf-8")

    def _doc_sections(self):
        """Map each ``## <title>`` heading of PARTNERS.md to its body text."""
        sections = {}
        title = None
        for line in self._doc_text().splitlines():
            if line.startswith("## "):
                title = line[3:].strip()
                sections[title] = []
            elif title is not None:
                sections[title].append(line)
        return {name: "\n".join(lines) for name, lines in sections.items()}

    def _partner_section(self, sections, partner_id):
        """Body of the partner's own section; '' when the doc has none."""
        for title, body in sections.items():
            if title.split("(")[0].strip() == partner_id:
                return body
        return ""

    def _configured_partners(self):
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        return data["partners"]

    def _skill_names(self, partner_id, raw):
        """Installed skill dir names, resolved like core.config._skills_root.

        Returns None when the resolved skills root does not exist at all.
        """
        skills_root = raw.get("skills_root") or f"skills/{partner_id}"
        # Same guard core.config applies on load: skills_root must stay a
        # relative path inside partners/, so absolute paths or ".."
        # escapes raise here and fail the test loudly instead of silently
        # scanning a directory outside the package.
        skills_root = _safe_relative_path(skills_root, "skills_root")
        root = ROOT / "partners" / skills_root
        if not root.is_dir():
            return None
        return [
            directory.name
            for directory in sorted(p for p in root.iterdir() if p.is_dir())
            if (directory / "manifest.json").exists()
        ]

    def _assert_not_advertised(self, section, tool_name):
        """Every mention of tool_name must be a negation, never an offer.

        Line-scoped heuristic: a mention counts as negated only when the
        line it appears on carries a negation marker (`no `, `not `,
        `lacks`, `does not`, `never`, `without`). Deliberately simple and
        deterministic - docs should spell the negation out on the same
        line as the tool name.
        """
        markers = ("no ", "not ", "lacks", "does not", "never", "without")
        for line in section.splitlines():
            if tool_name not in line:
                continue
            lower = line.lower()
            self.assertTrue(
                any(marker in lower for marker in markers),
                f"{tool_name} appears advertised, not negated, in section:\n{section}",
            )

    def test_every_configured_partner_id_is_documented(self):
        text = self._doc_text()
        for partner_id in self._configured_partners():
            with self.subTest(partner=partner_id):
                self.assertIn(partner_id, text)

    def test_every_installed_skill_name_is_documented(self):
        sections = self._doc_sections()
        for partner_id, raw in self._configured_partners().items():
            section = self._partner_section(sections, partner_id)
            with self.subTest(partner=partner_id):
                self.assertTrue(
                    section,
                    f"missing PARTNERS.md '## {partner_id}' section",
                )
            names = self._skill_names(partner_id, raw)
            if not names:
                # No resolved skill dirs: the doc must still name the
                # partner and its configured default_skill explicitly.
                with self.subTest(partner=partner_id, skill="<none>"):
                    default_skill = str(raw.get("default_skill") or "")
                    self.assertTrue(default_skill)
                    self.assertIn(f"`{default_skill}`", section)
                continue
            for name in names:
                with self.subTest(partner=partner_id, skill=name):
                    # Backticked inside the partner's own section so the
                    # code-reviewer *skill* cannot be satisfied by the
                    # code-reviewer *partner id* elsewhere in the doc.
                    self.assertIn(f"`{name}`", section)

    def test_documented_tools_match_configured_toolset(self):
        sections = self._doc_sections()
        # Common tools are documented once under "How to call a partner",
        # not repeated inside every partner section.
        shared = sections.get("How to call a partner", "")
        env = patch.dict(
            os.environ,
            {"PARTNER_CONFIG": str(CONFIG_EXAMPLE)},
            clear=True,
        )
        with env:
            for partner_id, raw in self._configured_partners().items():
                if not raw.get("enabled", True):
                    continue
                section = self._partner_section(sections, partner_id)
                documented = section + "\n" + shared
                names = {tool["name"] for tool in PartnerMCPServer(partner_id).tools()}
                for name in sorted(names):
                    with self.subTest(partner=partner_id, tool=name):
                        self.assertIn(name, documented)
                # Cross-toolset leakage: question-tool names from the
                # other toolset branches must not appear as offers
                # either. Same per-line negation heuristic, so an
                # explicit "no ask_code_partner"-style note still passes.
                for name in sorted(set(ASK_TOOL_NAMES) - names):
                    with self.subTest(partner=partner_id, foreign=name):
                        self._assert_not_advertised(section, name)
                if raw.get("toolset") == "review":
                    for name in ASK_TOOL_NAMES:
                        with self.subTest(partner=partner_id, forbidden=name):
                            self.assertNotIn(name, section)
                    with self.subTest(partner=partner_id, forbidden="list_partner_models"):
                        self._assert_not_advertised(section, "list_partner_models")
                else:
                    for name in REVIEW_ONLY_TOOLS:
                        with self.subTest(partner=partner_id, forbidden=name):
                            self.assertNotIn(name, section)


if __name__ == "__main__":
    unittest.main()

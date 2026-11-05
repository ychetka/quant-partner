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
        "skills": {"product-manager"},
        "temperature": 0.4,
    },
    "market-partner": {
        "harness": "harness/market-partner.txt",
        "default_skill": "market-synthesis",
        "skills_root": "skills/market-partner",
        "skills": {"market-synthesis"},
        "temperature": 0.3,
    },
    "strategy-partner": {
        "harness": "harness/strategy-partner.txt",
        "default_skill": "strategy-consultant",
        "skills_root": "skills/strategy-partner",
        "skills": {"strategy-consultant"},
        "temperature": 0.4,
    },
    "comms-partner": {
        "harness": "harness/comms-partner.txt",
        "default_skill": "communication",
        "skills_root": "skills/comms-partner",
        "skills": {"communication"},
        "temperature": 0.4,
    },
    "org-partner": {
        "harness": "harness/org-partner.txt",
        "default_skill": "org-design",
        "skills_root": "skills/org-partner",
        "skills": {"org-design", "operating-model", "role-architecture"},
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
                names = {skill.name for skill in list_skills(cfg)}
                self.assertEqual(names, spec["skills"])
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

    def test_org_harness_org_design_clauses(self):
        text = self._harness("org-partner").lower()
        for clause in (
            "digital employee",
            "span of control",
            "raci",
            "quorum",
            "escalation",
            "hand-off",
            "caller-supplied",
            "advisory",
            "human",
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


GROUNDING_FIELDS = ("decision_to_make", "options_considered", "constraints", "context_pack")


class AdvisoryGroundingContractTest(unittest.TestCase):
    """Issue #48: grounding contract fields on the advisory toolset only."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"PARTNER_CONFIG": str(CONFIG_EXAMPLE)}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_advisory_schemas_expose_grounding_fields(self):
        for partner_id in ADVISORY_PARTNERS:
            with self.subTest(partner=partner_id):
                tools = {tool["name"]: tool for tool in PartnerMCPServer(partner_id).tools()}
                for name in ("ask_partner", "ask_partner_v2", "ask_partner_dialog"):
                    with self.subTest(partner=partner_id, tool=name):
                        properties = tools[name]["inputSchema"]["properties"]
                        for field in GROUNDING_FIELDS:
                            self.assertIn(field, properties)
                        self.assertEqual(properties["decision_to_make"]["type"], "string")
                        self.assertEqual(properties["context_pack"]["type"], "string")
                        for field in ("options_considered", "constraints"):
                            self.assertEqual(properties[field]["type"], "array")
                            self.assertEqual(properties[field]["items"], {"type": "string"})

    def test_non_advisory_toolsets_do_not_expose_grounding_fields(self):
        partners = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))["partners"]
        non_advisory = {
            partner_id
            for partner_id, raw in partners.items()
            if raw.get("enabled", True) and raw.get("toolset") != "advisory"
        }
        # The sweep must really cover every enabled non-advisory
        # partner - one per code, product and review toolset. Disabled
        # partners cannot be loaded, so they cannot be checked here.
        self.assertEqual(
            {"gh-copilot-partner", "gemini-product", "code-reviewer"},
            non_advisory,
        )
        for partner_id in sorted(non_advisory):
            for tool in PartnerMCPServer(partner_id).tools():
                properties = tool["inputSchema"]["properties"]
                for field in GROUNDING_FIELDS:
                    with self.subTest(partner=partner_id, tool=tool["name"], field=field):
                        self.assertNotIn(field, properties)

    def test_grounded_helper(self):
        grounded = PartnerMCPServer._grounded
        self.assertFalse(grounded({"question": "q?"}))
        self.assertFalse(grounded({"question": "q?", "context": "   "}))
        self.assertFalse(grounded({"question": "q?", "options_considered": []}))
        for args in (
            {"decision_to_make": "pick one"},
            {"options_considered": ["a"]},
            {"constraints": ["c"]},
            {"context": "ctx"},
            {"context_pack": "brief"},
            {"files": [{"name": "f", "content": "x"}]},
        ):
            with self.subTest(args=args):
                self.assertTrue(grounded(args))

    def test_grounded_helper_blank_renderings_do_not_count(self):
        # A field counts as grounding only when it would render
        # grounding content: blank-only items, whitespace scalars and
        # files entries without content render nothing, so they must
        # not let a bare question escape the fail-soft instruction.
        grounded = PartnerMCPServer._grounded
        for args in (
            {"options_considered": [" ", ""]},
            {"options_considered": "  "},
            {"options_considered": 5},
            {"constraints": [""]},
            {"files": []},
            {"files": [{"name": "f"}]},
            {"files": [{"name": "f", "content": ""}]},
            {"files": [{"name": "f", "content": "  "}]},
            {"files": ["f.txt"]},
            {"context": "\t \n"},
            {"context_pack": "  "},
            {"decision_to_make": 0},
        ):
            with self.subTest(args=args):
                self.assertFalse(grounded(args))

    def test_grounded_helper_non_string_values(self):
        # The renderer coerces values with str(), so non-string scalars
        # and items that render a section count as grounded too.
        grounded = PartnerMCPServer._grounded
        for args in (
            {"decision_to_make": 5},
            {"context_pack": 5},
            {"context": 5},
            {"options_considered": [5]},
            {"files": [{"name": "f", "content": 5}]},
        ):
            with self.subTest(args=args):
                self.assertTrue(grounded(args))

    def test_composed_message_labeled_sections_in_stable_order(self):
        server = PartnerMCPServer("pm-partner")
        content = server._ask_messages(
            {
                "question": "Which option?",
                "context": "ad-hoc context",
                "decision_to_make": "pick A or B",
                "options_considered": ["A", "B"],
                "constraints": ["budget cap", "no new hires"],
                "context_pack": "project brief",
                "files": [{"name": "f.txt", "content": "data"}],
            }
        )[0]["content"]
        expected_order = [
            "Which option?",
            "## Context\nad-hoc context",
            "## Decision to make\npick A or B",
            "## Options considered\n- A\n- B",
            "## Constraints\n- budget cap\n- no new hires",
            "## Context pack\nproject brief",
            "## File: f.txt",
        ]
        position = 0
        for marker in expected_order:
            with self.subTest(marker=marker):
                next_position = content.find(marker)
                self.assertGreaterEqual(next_position, position)
                position = next_position
        # Grounded call: no clarifying-questions instruction.
        self.assertNotIn("clarifying questions", content)

    def test_composed_message_omits_unsupplied_sections(self):
        server = PartnerMCPServer("pm-partner")
        content = server._ask_messages(
            {"question": "q?", "context": "ctx", "constraints": ["only one"]}
        )[0]["content"]
        self.assertIn("## Constraints\n- only one", content)
        self.assertNotIn("## Decision to make", content)
        self.assertNotIn("## Options considered", content)
        self.assertNotIn("## Context pack", content)
        # Blank values produce no section either.
        blank = server._ask_messages(
            {"question": "q?", "context": "ctx", "decision_to_make": "  "}
        )[0]["content"]
        self.assertNotIn("## Decision to make", blank)

    def test_ungrounded_ask_injects_clarifying_questions_instruction(self):
        server = PartnerMCPServer("pm-partner")
        content = server._ask_messages({"question": "q?"})[0]["content"]
        self.assertIn("## Missing grounding", content)
        self.assertIn("exactly 2-3 numbered clarifying questions", content)
        self.assertTrue(content.endswith("do not attempt to answer yet."))

    def test_grounded_ask_has_no_instruction(self):
        server = PartnerMCPServer("pm-partner")
        for field_args in (
            {"context": "ctx"},
            {"context_pack": "brief"},
            {"decision_to_make": "d"},
            {"options_considered": ["o"]},
            {"constraints": ["c"]},
            {"files": [{"name": "f", "content": "x"}]},
        ):
            with self.subTest(field_args=field_args):
                content = server._ask_messages({"question": "q?", **field_args})[0]["content"]
                self.assertNotIn("## Missing grounding", content)

    def test_blank_grounding_fields_keep_ungrounded_instruction(self):
        # Whitespace-only grounding values render no section, so the
        # call stays ungrounded and keeps the clarifying-questions
        # instruction appended.
        server = PartnerMCPServer("pm-partner")
        for field_args in (
            {"context": "  "},
            {"options_considered": [" ", ""]},
            {"constraints": [""]},
            {"files": [{"name": "f"}]},
            {"files": [{"name": "f", "content": "  "}]},
        ):
            with self.subTest(field_args=field_args):
                content = server._ask_messages({"question": "q?", **field_args})[0]["content"]
                self.assertIn("## Missing grounding", content)
                for marker in (
                    "## Context\n",
                    "## Options considered",
                    "## Constraints",
                    "## File:",
                ):
                    self.assertNotIn(marker, content)

    def test_non_string_grounding_scalar_counts_as_grounded(self):
        # decision_to_make=5 renders a real section, so the call is
        # grounded: no clarifying-questions instruction is appended.
        server = PartnerMCPServer("pm-partner")
        content = server._ask_messages({"question": "q?", "decision_to_make": 5})[0]["content"]
        self.assertIn("## Decision to make\n5", content)
        self.assertNotIn("## Missing grounding", content)

    def test_non_advisory_ask_ignores_grounding_fields(self):
        server = PartnerMCPServer("gh-copilot-partner")
        content = server._ask_messages(
            {
                "question": "q?",
                "decision_to_make": "d",
                "options_considered": ["o"],
                "constraints": ["c"],
                "context_pack": "brief",
            }
        )[0]["content"]
        self.assertEqual(content, "q?")

    def test_dialog_context_pack_joins_leading_context_turn(self):
        server = PartnerMCPServer("pm-partner")
        sent = server._dialog_messages(
            {
                "messages": [{"role": "user", "text": "hi"}],
                "context": "ad-hoc context",
                "context_pack": "project brief",
            }
        )
        self.assertEqual(
            sent[0],
            {
                "role": "user",
                "content": "## Context\nad-hoc context\n\n## Context pack\nproject brief",
            },
        )
        self.assertEqual(sent[1], {"role": "user", "content": "hi"})

    def test_dialog_context_pack_alone_forms_leading_turn(self):
        server = PartnerMCPServer("pm-partner")
        sent = server._dialog_messages(
            {
                "messages": [{"role": "user", "text": "hi"}],
                "context_pack": "project brief",
            }
        )
        self.assertEqual(sent[0], {"role": "user", "content": "## Context pack\nproject brief"})
        self.assertEqual(len(sent), 2)

    def test_dialog_grounding_fields_join_leading_turn(self):
        server = PartnerMCPServer("pm-partner")
        sent = server._dialog_messages(
            {
                "messages": [{"role": "user", "text": "hi"}],
                "decision_to_make": "pick A or B",
                "options_considered": ["A"],
            }
        )
        self.assertEqual(
            sent[0],
            {
                "role": "user",
                "content": "## Decision to make\npick A or B\n\n## Options considered\n- A",
            },
        )

    def test_dialog_never_gets_ungrounded_instruction(self):
        server = PartnerMCPServer("pm-partner")
        sent = server._dialog_messages({"messages": [{"role": "user", "text": "hi"}]})
        self.assertEqual(sent, [{"role": "user", "content": "hi"}])

    def test_non_advisory_dialog_ignores_grounding_fields(self):
        server = PartnerMCPServer("gemini-product")
        sent = server._dialog_messages(
            {
                "messages": [{"role": "user", "text": "hi"}],
                "context": "ctx",
                "context_pack": "brief",
                "decision_to_make": "d",
            }
        )
        self.assertEqual(
            sent,
            [
                {"role": "user", "content": "## Context\nctx"},
                {"role": "user", "content": "hi"},
            ],
        )


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

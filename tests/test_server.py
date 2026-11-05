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

from backends.base import CompletionResult  # noqa: E402
from core.config import load_skill  # noqa: E402
from core.errors import PartnerError, sanitize  # noqa: E402
from core.server import PartnerMCPServer  # noqa: E402


class ExBackend:
    """Fake backend exposing complete_ex with scripted model_used."""

    def __init__(self, text="answer text", model_used=None):
        self.text = text
        self.model_used = model_used
        self.calls = []
        self.sent_messages = []

    def list_models(self, refresh=False):
        return []

    def complete_ex(self, messages, *, system_prompt, model, generation):
        self.calls.append(model)
        self.sent_messages.append(messages)
        return CompletionResult(text=self.text, model_used=self.model_used)

    def complete(self, messages, *, system_prompt, model, generation):
        return self.complete_ex(
            messages, system_prompt=system_prompt, model=model, generation=generation
        ).text


class PlainBackend:
    """Minimal fake without complete_ex; only the legacy complete call."""

    def __init__(self, text="plain answer"):
        self.text = text
        self.calls = []

    def complete(self, messages, *, system_prompt, model, generation):
        self.calls.append(model)
        return self.text


class ServerTest(unittest.TestCase):
    def setUp(self):
        # clear=True keeps stray QP_AUTH_*/QP_MODEL_* overrides from the
        # ambient environment out of auth_configured assertions.
        self.env = patch.dict(
            os.environ,
            {"PARTNER_CONFIG": str(ROOT / "partners" / "config.example.json")},
            clear=True,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_code_tools(self):
        server = PartnerMCPServer("gh-copilot-partner")
        tools = server.tools()
        names = {tool["name"] for tool in tools}
        self.assertIn("partner_info", names)
        self.assertIn("list_partner_models", names)
        self.assertIn("list_partner_skills", names)
        # Canonical names first, then the code toolset's legacy aliases.
        ask_names = [tool["name"] for tool in tools if tool["name"].startswith("ask_")]
        self.assertEqual(
            ask_names,
            [
                "ask_partner",
                "ask_partner_v2",
                "ask_partner_dialog",
                "ask_code_partner",
                "ask_code_partner_v2",
                "ask_code_partner_dialog",
            ],
        )

    def test_product_tools(self):
        server = PartnerMCPServer("gemini-product")
        tools = server.tools()
        ask_names = [tool["name"] for tool in tools if tool["name"].startswith("ask_")]
        self.assertEqual(
            ask_names,
            [
                "ask_partner",
                "ask_partner_v2",
                "ask_partner_dialog",
                "ask_product_partner",
                "ask_product_partner_v2",
                "ask_product_partner_dialog",
            ],
        )

    def test_legacy_alias_descriptions_are_marked(self):
        server = PartnerMCPServer("gh-copilot-partner")
        tools = {tool["name"]: tool for tool in server.tools()}
        for alias, canonical in (
            ("ask_code_partner", "ask_partner"),
            ("ask_code_partner_v2", "ask_partner_v2"),
            ("ask_code_partner_dialog", "ask_partner_dialog"),
        ):
            with self.subTest(alias=alias):
                self.assertTrue(
                    tools[alias]["description"].endswith(f"Legacy alias of {canonical}."),
                    tools[alias]["description"],
                )
                self.assertEqual(tools[alias]["inputSchema"], tools[canonical]["inputSchema"])

    def test_legacy_alias_dispatches_like_canonical(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(text="answer text")
        args = {"question": "q?"}
        self.assertEqual(
            server.call_tool("ask_code_partner", args),
            server.call_tool("ask_partner", args),
        )
        self.assertEqual(
            json.loads(server.call_tool("ask_code_partner_v2", args)),
            json.loads(server.call_tool("ask_partner_v2", args)),
        )
        dialog_args = {"messages": [{"role": "user", "text": "hi"}]}
        self.assertEqual(
            server.call_tool("ask_code_partner_dialog", dialog_args),
            server.call_tool("ask_partner_dialog", dialog_args),
        )

    def test_product_legacy_alias_dispatches_like_canonical(self):
        server = PartnerMCPServer("gemini-product")
        server._backend = ExBackend(text="answer text")
        args = {"question": "q?"}
        self.assertEqual(
            server.call_tool("ask_product_partner", args),
            server.call_tool("ask_partner", args),
        )
        self.assertEqual(
            json.loads(server.call_tool("ask_product_partner_v2", args)),
            json.loads(server.call_tool("ask_partner_v2", args)),
        )
        dialog_args = {"messages": [{"role": "user", "text": "hi"}]}
        self.assertEqual(
            server.call_tool("ask_product_partner_dialog", dialog_args),
            server.call_tool("ask_partner_dialog", dialog_args),
        )

    def test_foreign_toolset_alias_rejected(self):
        for partner_id, foreign in (
            ("gh-copilot-partner", "ask_product_partner"),
            ("gemini-product", "ask_code_partner"),
        ):
            with self.subTest(partner=partner_id, foreign=foreign):
                server = PartnerMCPServer(partner_id)
                server._backend = ExBackend()
                with self.assertRaises(PartnerError) as ctx:
                    server.call_tool(foreign, {"question": "q?"})
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(server._backend.calls, [])

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

    # -- ask_v2 ----------------------------------------------------------------

    def test_ask_v2_tool_registered_with_same_schema(self):
        server = PartnerMCPServer("gh-copilot-partner")
        tools = {tool["name"]: tool for tool in server.tools()}
        self.assertIn("ask_partner_v2", tools)
        self.assertIn("ask_code_partner_v2", tools)
        self.assertEqual(
            tools["ask_partner_v2"]["inputSchema"],
            tools["ask_partner"]["inputSchema"],
        )
        self.assertEqual(
            tools["ask_code_partner_v2"]["inputSchema"],
            tools["ask_partner_v2"]["inputSchema"],
        )

    def test_product_ask_v2_registered(self):
        server = PartnerMCPServer("gemini-product")
        names = {tool["name"] for tool in server.tools()}
        self.assertIn("ask_partner_v2", names)
        self.assertIn("ask_product_partner_v2", names)
        self.assertNotIn("ask_code_partner_v2", names)

    def test_ask_v2_reports_model_provenance(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(text="answer text", model_used="claude-sonnet-5")
        payload = json.loads(server.call_tool("ask_code_partner_v2", {"question": "q?"}))
        self.assertEqual(payload["partner_id"], "gh-copilot-partner")
        self.assertEqual(payload["skill"], "software-architect")
        self.assertEqual(payload["model_requested"], "claude-sonnet-5")
        self.assertEqual(payload["model_used"], "claude-sonnet-5")
        self.assertIs(payload["model_match"], True)
        self.assertEqual(payload["text"], "answer text")

    def test_ask_v2_model_override_is_model_requested(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(model_used="custom-model")
        payload = json.loads(
            server.call_tool("ask_code_partner_v2", {"question": "q?", "model": "custom-model"})
        )
        self.assertEqual(payload["model_requested"], "custom-model")
        self.assertEqual(payload["model_used"], "custom-model")
        self.assertIs(payload["model_match"], True)

    def test_ask_v2_model_used_mismatch_reports_false(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(model_used="other-model")
        payload = json.loads(server.call_tool("ask_code_partner_v2", {"question": "q?"}))
        self.assertEqual(payload["model_requested"], "claude-sonnet-5")
        self.assertEqual(payload["model_used"], "other-model")
        self.assertIs(payload["model_match"], False)

    def test_ask_v2_missing_model_used_is_null(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(model_used=None)
        payload = json.loads(server.call_tool("ask_code_partner_v2", {"question": "q?"}))
        self.assertIsNone(payload["model_used"])
        self.assertIsNone(payload["model_match"])
        self.assertEqual(payload["text"], "answer text")

    def test_ask_v2_plain_backend_without_complete_ex(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = PlainBackend()
        payload = json.loads(server.call_tool("ask_code_partner_v2", {"question": "q?"}))
        self.assertEqual(payload["text"], "plain answer")
        self.assertEqual(payload["model_requested"], "claude-sonnet-5")
        self.assertIsNone(payload["model_used"])
        self.assertIsNone(payload["model_match"])

    def test_generic_ask_v2_alias_dispatches(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend()
        payload = json.loads(server.call_tool("ask_partner_v2", {"question": "q?"}))
        self.assertEqual(payload["partner_id"], "gh-copilot-partner")
        self.assertEqual(payload["text"], "answer text")

    def test_ask_v2_requires_question(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend()
        with self.assertRaises(PartnerError) as ctx:
            server.call_tool("ask_code_partner_v2", {})
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(server._backend.calls, [])

    def test_legacy_ask_and_dialog_output_unchanged(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(text="plain text answer")
        self.assertEqual(
            server.call_tool("ask_code_partner", {"question": "q?"}), "plain text answer"
        )
        self.assertEqual(
            server.call_tool(
                "ask_code_partner_dialog",
                {"messages": [{"role": "user", "text": "hi"}]},
            ),
            "plain text answer",
        )
        self.assertEqual(
            server._backend.sent_messages[1],
            [{"role": "user", "content": "hi"}],
        )

    # -- dialog context ----------------------------------------------------------

    def test_dialog_tool_schema_exposes_context(self):
        server = PartnerMCPServer("gh-copilot-partner")
        tools = {tool["name"]: tool for tool in server.tools()}
        properties = tools["ask_partner_dialog"]["inputSchema"]["properties"]
        self.assertIn("context", properties)
        self.assertEqual(properties["context"]["type"], "string")
        self.assertEqual(tools["ask_partner_dialog"]["inputSchema"]["required"], ["messages"])

    def test_dialog_context_added_as_leading_user_turn(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(text="answer")
        server.call_tool(
            "ask_code_partner_dialog",
            {
                "messages": [
                    {"role": "user", "text": "first"},
                    {"role": "assistant", "text": "earlier answer"},
                    {"role": "user", "text": "follow-up"},
                ],
                "context": "venture brief",
            },
        )
        sent = server._backend.sent_messages[0]
        self.assertEqual(
            sent[0],
            {"role": "user", "content": "## Context\nventure brief"},
        )
        self.assertEqual(
            sent[1:],
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "follow-up"},
            ],
        )

    def test_dialog_context_keeps_assistant_first_history_intact(self):
        """Regression: an assistant-first history must not absorb the brief.

        The context lands in its own leading user turn; the original
        assistant message keeps its role and verbatim content.
        """
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(text="answer")
        server.call_tool(
            "ask_code_partner_dialog",
            {
                "messages": [
                    {"role": "assistant", "text": "earlier answer"},
                    {"role": "user", "text": "follow-up"},
                ],
                "context": "venture brief",
            },
        )
        sent = server._backend.sent_messages[0]
        self.assertEqual(
            sent[0],
            {"role": "user", "content": "## Context\nventure brief"},
        )
        self.assertEqual(
            sent[1:],
            [
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "follow-up"},
            ],
        )

    def test_dialog_without_context_leaves_messages_unchanged(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(text="answer")
        for args in (
            {"messages": [{"role": "user", "text": "hi"}]},
            {"messages": [{"role": "user", "text": "hi"}], "context": ""},
        ):
            server.call_tool("ask_code_partner_dialog", args)
        for sent in server._backend.sent_messages:
            self.assertEqual(sent, [{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()

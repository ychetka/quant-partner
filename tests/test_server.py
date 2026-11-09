#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from backends.base import CompletionResult  # noqa: E402
from core.config import load_skill  # noqa: E402
from core.errors import PartnerError, sanitize  # noqa: E402
from core.mcp import handle_message  # noqa: E402
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


class CrashOnceBackend:
    """Fake backend that raises an unexpected error once, then recovers."""

    def __init__(self, text="recovered"):
        self.text = text
        self.calls = []

    def complete(self, messages, *, system_prompt, model, generation):
        self.calls.append(model)
        if len(self.calls) == 1:
            raise RuntimeError("backend exploded")
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
        foreign_aliases = {
            "gh-copilot-partner": (
                "ask_product_partner",
                "ask_product_partner_v2",
                "ask_product_partner_dialog",
            ),
            "gemini-product": (
                "ask_code_partner",
                "ask_code_partner_v2",
                "ask_code_partner_dialog",
            ),
        }
        for partner_id, aliases in foreign_aliases.items():
            for foreign in aliases:
                args = (
                    {"messages": [{"role": "user", "text": "hi"}]}
                    if foreign.endswith("_dialog")
                    else {"question": "q?"}
                )
                with self.subTest(partner=partner_id, foreign=foreign):
                    server = PartnerMCPServer(partner_id)
                    server._backend = ExBackend()
                    with self.assertRaises(PartnerError) as ctx:
                        server.call_tool(foreign, args)
                    self.assertEqual(ctx.exception.code, "invalid_request")
                    self.assertIn("unknown tool", ctx.exception.message)
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


def _temp_config(path: Path, partner_id: str = "gh-copilot-partner", **overrides) -> None:
    """Write a minimal valid one-partner config file for reload tests."""
    section = {
        "enabled": True,
        "toolset": "code",
        "backend": "openai-compatible",
        "harness": "harness/gh-copilot-partner.txt",
        "default_skill": "software-architect",
        "auth": {},
        "model": "model-a",
    }
    section.update(overrides)
    path.write_text(json.dumps({"partners": {partner_id: section}}), encoding="utf-8")


def _bump_mtime(path: Path, ns: int = 1_000_000_000) -> None:
    """Push the file mtime forward so the reload check sees a change."""
    new = path.stat().st_mtime_ns + ns
    os.utime(path, ns=(new, new))


def _json_records(stream: io.StringIO) -> list[dict]:
    records = []
    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


class ConfigReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        _temp_config(self.path, model="model-a")
        self.env = patch.dict(os.environ, {"PARTNER_CONFIG": str(self.path)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_reload_picks_up_changed_model_and_clears_backend(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend()
        _temp_config(self.path, model="model-b")
        _bump_mtime(self.path)
        info = json.loads(server.call_tool("partner_info", {}))
        self.assertEqual(info["model"], "model-b")
        self.assertIsNone(server._backend)
        self.assertIsNone(info["config_reload_error"])

    def test_reload_reflects_in_tools_dispatch(self):
        server = PartnerMCPServer("gh-copilot-partner")
        _temp_config(self.path, toolset="product")
        _bump_mtime(self.path)
        names = {tool["name"] for tool in server.tools()}
        self.assertIn("ask_product_partner", names)
        self.assertNotIn("ask_code_partner", names)

    def test_invalid_reload_keeps_previous_config_and_warns(self):
        server = PartnerMCPServer("gh-copilot-partner")
        self.path.write_text("{not json", encoding="utf-8")
        _bump_mtime(self.path)
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            info = json.loads(server.call_tool("partner_info", {}))
        self.assertEqual(info["model"], "model-a")
        self.assertEqual(info["config_reload_error"], "invalid_config")
        warnings = [r for r in _json_records(stderr) if r.get("event") == "config_reload"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["outcome"], "error")
        self.assertEqual(warnings[0]["reason"], "invalid_config")
        self.assertEqual(warnings[0]["error_class"], "PartnerError")

    def test_non_utf8_reload_keeps_previous_config_and_warns(self):
        """Regression: a decode failure must not escape ``_maybe_reload``.

        ``load_partner_config`` raises ``UnicodeDecodeError`` for
        non-UTF-8 bytes - outside the PartnerError/OSError family - so
        the broad catch has to keep serving the previous valid config
        and emit the warn-once ``config_reload`` record.
        """
        server = PartnerMCPServer("gh-copilot-partner")
        self.path.write_bytes(b'{"partners": \xff\xfe}')
        _bump_mtime(self.path)
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            info = json.loads(server.call_tool("partner_info", {}))
        self.assertEqual(info["model"], "model-a")
        self.assertEqual(info["config_reload_error"], "decode_error")
        warnings = [r for r in _json_records(stderr) if r.get("event") == "config_reload"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["outcome"], "error")
        self.assertEqual(warnings[0]["reason"], "decode_error")
        self.assertEqual(warnings[0]["error_class"], "UnicodeDecodeError")

    def test_same_mtime_different_size_still_reloads(self):
        """Timestamp-preserving writes: the (mtime_ns, size) key catches them."""
        server = PartnerMCPServer("gh-copilot-partner")
        stat = self.path.stat()
        _temp_config(self.path, model="model-longer-name")
        # Restore the original timestamps so only the size differs.
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        info = json.loads(server.call_tool("partner_info", {}))
        self.assertEqual(info["model"], "model-longer-name")
        self.assertIsNone(info["config_reload_error"])

    def test_failed_reload_warns_once_until_stamp_changes(self):
        server = PartnerMCPServer("gh-copilot-partner")
        self.path.write_text("{not json", encoding="utf-8")
        _bump_mtime(self.path)
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            for _ in range(2):
                server.call_tool("partner_info", {})
        warnings = [r for r in _json_records(stderr) if r.get("event") == "config_reload"]
        self.assertEqual(len(warnings), 1)

    def test_missing_config_file_keeps_previous_config_and_warns_once(self):
        server = PartnerMCPServer("gh-copilot-partner")
        self.path.unlink()
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            info = json.loads(server.call_tool("partner_info", {}))
            server.call_tool("partner_info", {})
        self.assertEqual(info["model"], "model-a")
        warnings = [r for r in _json_records(stderr) if r.get("event") == "config_reload"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["outcome"], "error")

    def test_config_reappearing_after_delete_recovers(self):
        server = PartnerMCPServer("gh-copilot-partner")
        self.path.unlink()
        with patch.object(sys, "stderr", io.StringIO()):
            server.call_tool("partner_info", {})
        _temp_config(self.path, model="model-c")
        _bump_mtime(self.path)
        info = json.loads(server.call_tool("partner_info", {}))
        self.assertEqual(info["model"], "model-c")
        self.assertIsNone(info["config_reload_error"])

    def test_initially_broken_config_recovers_on_reload(self):
        self.path.write_text("{not json", encoding="utf-8")
        server = PartnerMCPServer("gh-copilot-partner")
        self.assertIsNotNone(server._config_error)
        _temp_config(self.path, model="model-d")
        _bump_mtime(self.path)
        info = json.loads(server.call_tool("partner_info", {}))
        self.assertEqual(info["model"], "model-d")
        self.assertIsNone(server._config_error)


class RequestLogTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"PARTNER_CONFIG": str(ROOT / "partners" / "config.example.json")},
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _call_logged(self, server, name, arguments):
        stderr = io.StringIO()
        result = None
        error = None
        with patch.object(sys, "stderr", stderr):
            try:
                result = server.call_tool(name, arguments)
            except Exception as e:
                error = e
        records = [r for r in _json_records(stderr) if r.get("event") == "partner_call"]
        return result, error, records, stderr.getvalue()

    def test_partner_call_log_fields_for_ok_outcome(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend(text="answer text", model_used="served-model")
        result, error, records, raw = self._call_logged(
            server, "ask_partner", {"question": "top secret question body"}
        )
        self.assertIsNone(error)
        self.assertEqual(result, "answer text")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["partner_id"], "gh-copilot-partner")
        self.assertEqual(record["tool"], "ask_partner")
        self.assertEqual(record["skill"], "software-architect")
        self.assertEqual(record["model_requested"], "claude-sonnet-5")
        self.assertEqual(record["model_used"], "served-model")
        self.assertIsInstance(record["latency_ms"], int)
        self.assertEqual(record["outcome"], "ok")
        self.assertNotIn("top secret question body", raw)

    def test_partner_call_log_records_partner_error_code(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = ExBackend()
        _, error, records, _ = self._call_logged(server, "ask_partner", {})
        self.assertIsInstance(error, PartnerError)
        assert isinstance(error, PartnerError)
        self.assertEqual(error.code, "invalid_request")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "invalid_request")
        self.assertIsNone(records[0]["skill"])
        self.assertIsNone(records[0]["model_requested"])

    def test_partner_call_log_records_internal_error_outcome(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = CrashOnceBackend()
        _, error, records, _ = self._call_logged(server, "ask_partner", {"question": "q?"})
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "internal_error")
        self.assertEqual(records[0]["skill"], "software-architect")

    def test_log_line_is_sanitized_against_config_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _temp_config(
                path,
                auth={"api_key": "tok-live-secret-999"},
                model="model-tok-live-secret-999-x",
            )
            with patch.dict(os.environ, {"PARTNER_CONFIG": str(path)}, clear=True):
                server = PartnerMCPServer("gh-copilot-partner")
                server._backend = ExBackend()
                _, error, records, raw = self._call_logged(
                    server, "ask_partner", {"question": "q?"}
                )
        self.assertIsNone(error)
        self.assertEqual(len(records), 1)
        self.assertNotIn("tok-live-secret-999", raw)
        self.assertIn("[redacted]", raw)

    def test_crashing_backend_returns_is_error_and_recovers_next_call(self):
        server = PartnerMCPServer("gh-copilot-partner")
        server._backend = CrashOnceBackend()
        stdout = io.StringIO()
        stderr = io.StringIO()
        message = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "ask_partner", "arguments": {"question": "q?"}},
        }
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            handle_message(server, dict(message, id="first"))
            handle_message(server, dict(message, id="second"))
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("internal_error", responses[0]["result"]["content"][0]["text"])
        self.assertNotIn("backend exploded", stdout.getvalue())
        self.assertNotIn("isError", responses[1]["result"])
        self.assertEqual(responses[1]["result"]["content"][0]["text"], "recovered")
        records = [r for r in _json_records(stderr) if r.get("event") == "partner_call"]
        self.assertEqual([r["outcome"] for r in records], ["internal_error", "ok"])
        # The transport also reports the crash as a structured JSON
        # event on stderr, keeping the whole stream JSONL.
        internal = [r for r in _json_records(stderr) if r.get("event") == "internal_error"]
        self.assertEqual(len(internal), 1)
        self.assertEqual(internal[0]["tool"], "ask_partner")
        self.assertEqual(internal[0]["error_class"], "RuntimeError")
        # No non-JSON prose line may leak into the stderr stream.
        for line in stderr.getvalue().splitlines():
            if line.strip():
                self.assertIsInstance(json.loads(line), dict)

    # -- review_code outcome mapping ------------------------------------------

    def test_review_code_error_artifact_logs_error_outcome(self):
        server = PartnerMCPServer("code-reviewer")
        result, error, records, _ = self._call_logged(server, "review_code", {})
        self.assertIsNone(error)
        assert result is not None
        self.assertEqual(json.loads(result)["status"], "error")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "error")

    def test_review_code_rejected_artifact_logs_rejected_outcome(self):
        server = PartnerMCPServer("code-reviewer")
        args = {
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "diff": "x" * 200001,
            "model": "claude",
        }
        result, error, records, _ = self._call_logged(server, "review_code", args)
        self.assertIsNone(error)
        assert result is not None
        self.assertEqual(json.loads(result)["status"], "rejected")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "rejected")

    def test_review_code_ok_artifact_logs_ok_outcome(self):
        server = PartnerMCPServer("code-reviewer")
        ok_text = json.dumps({"status": "ok", "findings": []})
        with patch.object(PartnerMCPServer, "_review_code", return_value=ok_text):
            _, error, records, _ = self._call_logged(server, "review_code", {})
        self.assertIsNone(error)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "ok")

    def test_review_code_unparseable_result_logs_unknown_outcome(self):
        server = PartnerMCPServer("code-reviewer")
        with patch.object(PartnerMCPServer, "_review_code", return_value="not json"):
            result, error, records, _ = self._call_logged(server, "review_code", {})
        self.assertIsNone(error)
        self.assertEqual(result, "not json")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "unknown")


if __name__ == "__main__":
    unittest.main()

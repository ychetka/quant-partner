#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from backends.base import CompletionResult  # noqa: E402
from backends.copilot_direct import CopilotDirectBackend  # noqa: E402
from backends.gemini import GeminiBackend  # noqa: E402
from backends.openai_compatible import OpenAICompatibleBackend  # noqa: E402

MESSAGES = [{"role": "user", "content": "hi"}]
KW = {"system_prompt": "sys", "model": "claude-sonnet-5.5", "generation": {}}


class CompletionResultTest(unittest.TestCase):
    def test_default_model_used_is_none(self):
        result = CompletionResult(text="x")
        self.assertEqual(result.text, "x")
        self.assertIsNone(result.model_used)

    def test_default_usage_is_none(self):
        self.assertIsNone(CompletionResult(text="x").usage)


class CopilotProvenanceTest(unittest.TestCase):
    def _backend(self, options=None):
        backend = CopilotDirectBackend({"github_oauth_token": "oauth"}, options or {}, 10)
        backend._models_cache = [
            {"id": "claude-sonnet-5.5", "supported_endpoints": ["chat/completions"]}
        ]
        return backend

    def _run(self, payload, options=None):
        backend = self._backend(options)
        with (
            patch.object(
                backend, "_session", return_value=("https://api.githubcopilot.com", "sess")
            ),
            patch("backends.copilot_direct.post_json", return_value=payload),
        ):
            result = backend.complete_ex(MESSAGES, **KW)
        return backend, result

    def test_chat_completions_model_extracted(self):
        payload = {"choices": [{"message": {"content": "answer"}}], "model": "claude-sonnet-5.5"}
        _, result = self._run(payload)
        self.assertEqual(result.text, "answer")
        self.assertEqual(result.model_used, "claude-sonnet-5.5")
        self.assertIsInstance(result, CompletionResult)

    def test_complete_still_returns_str(self):
        payload = {"choices": [{"message": {"content": "answer"}}], "model": "claude-sonnet-5.5"}
        backend = self._backend()
        with (
            patch.object(
                backend, "_session", return_value=("https://api.githubcopilot.com", "sess")
            ),
            patch("backends.copilot_direct.post_json", return_value=payload),
        ):
            text = backend.complete(MESSAGES, **KW)
        self.assertIsInstance(text, str)
        self.assertEqual(text, "answer")

    def test_missing_model_is_none_not_inferred(self):
        payload = {"choices": [{"message": {"content": "answer"}}]}
        _, result = self._run(payload)
        self.assertIsNone(result.model_used)

    def test_mismatched_model_is_reported_honestly(self):
        payload = {"choices": [{"message": {"content": "answer"}}], "model": "gpt-5.3-codex"}
        _, result = self._run(payload)
        self.assertEqual(result.model_used, "gpt-5.3-codex")

    def test_responses_endpoint_model_extracted(self):
        payload = {"output_text": "answer", "model": "claude-sonnet-5.5"}
        _, result = self._run(payload, options={"copilot": {"api": "responses"}})
        self.assertEqual(result.text, "answer")
        self.assertEqual(result.model_used, "claude-sonnet-5.5")

    def test_responses_endpoint_missing_model_is_none(self):
        payload = {"output_text": "answer"}
        _, result = self._run(payload, options={"copilot": {"api": "responses"}})
        self.assertIsNone(result.model_used)


class GeminiProvenanceTest(unittest.TestCase):
    def _run(self, payload):
        backend = GeminiBackend({"gemini_api_key": "key"}, {"ipv4_only": False}, 10)
        with patch("backends.gemini.post_json", return_value=payload):
            return backend.complete_ex(
                MESSAGES, system_prompt="sys", model="gemini-4-argon", generation={}
            )

    def test_model_version_extracted(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "modelVersion": "models/gemini-4-argon",
        }
        result = self._run(payload)
        self.assertEqual(result.model_used, "gemini-4-argon")

    def test_model_version_without_prefix(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "modelVersion": "gemini-4-argon",
        }
        self.assertEqual(self._run(payload).model_used, "gemini-4-argon")

    def test_missing_model_version_is_none(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}
        self.assertIsNone(self._run(payload).model_used)

    def test_usage_metadata_extracted(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 4,
                "totalTokenCount": 16,
            },
        }
        self.assertEqual(
            self._run(payload).usage,
            {"prompt_tokens": 12, "candidates_tokens": 4, "total_tokens": 16},
        )

    def test_usage_metadata_thinking_counters_extracted(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 4,
                "totalTokenCount": 16,
                "thoughtsTokenCount": 7,
                "cachedContentTokenCount": 3,
            },
        }
        self.assertEqual(
            self._run(payload).usage,
            {
                "prompt_tokens": 12,
                "candidates_tokens": 4,
                "total_tokens": 16,
                "thoughts_tokens": 7,
                "cached_tokens": 3,
            },
        )

    def test_usage_metadata_without_thinking_counters_omits_them(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "usageMetadata": {"promptTokenCount": 12},
        }
        usage = self._run(payload).usage or {}
        self.assertEqual(usage, {"prompt_tokens": 12})
        self.assertNotIn("thoughts_tokens", usage)
        self.assertNotIn("cached_tokens", usage)

    def test_missing_usage_metadata_is_none(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}
        self.assertIsNone(self._run(payload).usage)

    def test_usage_metadata_keeps_int_counters_only(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": "many",
                "totalTokenCount": None,
                "thoughtsTokenCount": "many",
            },
        }
        self.assertEqual(self._run(payload).usage, {"prompt_tokens": 12})

    def test_unusable_usage_metadata_is_none(self):
        for usage_metadata in ("tokens", {"promptTokenCount": "x"}, {}):
            payload = {
                "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
                "usageMetadata": usage_metadata,
            }
            with self.subTest(usage=usage_metadata):
                self.assertIsNone(self._run(payload).usage)

    def test_complete_still_returns_str(self):
        backend = GeminiBackend({"gemini_api_key": "key"}, {"ipv4_only": False}, 10)
        payload = {
            "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
            "modelVersion": "models/gemini-4-argon",
        }
        with patch("backends.gemini.post_json", return_value=payload):
            text = backend.complete(
                MESSAGES, system_prompt="sys", model="gemini-4-argon", generation={}
            )
        self.assertIsInstance(text, str)
        self.assertEqual(text, "answer")


class OpenAICompatibleProvenanceTest(unittest.TestCase):
    def _backend(self):
        return OpenAICompatibleBackend(
            {"api_key": "k"}, {"openai": {"base_url": "http://127.0.0.1:1/v1"}}, 10
        )

    def _run(self, payload):
        backend = self._backend()
        with patch("backends.openai_compatible.post_json", return_value=payload):
            return backend.complete_ex(
                MESSAGES, system_prompt="sys", model="model-a", generation={}
            )

    def test_model_extracted(self):
        payload = {"choices": [{"message": {"content": "answer"}}], "model": "model-a"}
        self.assertEqual(self._run(payload).model_used, "model-a")

    def test_missing_model_is_none(self):
        payload = {"choices": [{"message": {"content": "answer"}}]}
        self.assertIsNone(self._run(payload).model_used)

    def test_mismatched_model_is_reported_honestly(self):
        payload = {"choices": [{"message": {"content": "answer"}}], "model": "other-model"}
        self.assertEqual(self._run(payload).model_used, "other-model")

    def test_complete_still_returns_str(self):
        backend = self._backend()
        payload = {"choices": [{"message": {"content": "answer"}}], "model": "model-a"}
        with patch("backends.openai_compatible.post_json", return_value=payload):
            text = backend.complete(MESSAGES, system_prompt="sys", model="model-a", generation={})
        self.assertIsInstance(text, str)
        self.assertEqual(text, "answer")


if __name__ == "__main__":
    unittest.main()

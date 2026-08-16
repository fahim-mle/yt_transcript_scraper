"""
Gateway tests — offline. Every provider call is faked at the urlopen boundary,
so nothing here reaches Ollama or a hosted endpoint.
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import config
from scraper import llm_client


def _response(payload: dict):
    """A urlopen context manager returning `payload` as a JSON body."""
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Resp(json.dumps(payload).encode("utf-8"))


class OllamaBackendTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(config, "LLM_PROVIDER", "ollama")
        p.start()
        self.addCleanup(p.stop)

    def test_returns_text_and_usage(self):
        body = {
            "message": {"content": "hello"},
            "prompt_eval_count": 12,
            "eval_count": 34,
        }
        with patch.object(llm_client.urllib.request, "urlopen",
                          lambda req, timeout: _response(body)):
            result = llm_client.chat(model="m", system="s", user="u")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.usage.prompt_tokens, 12)
        self.assertEqual(result.usage.completion_tokens, 34)
        self.assertEqual(result.usage.total_tokens, 46)

    def test_transport_failure_is_an_error_not_an_exception(self):
        def boom(req, timeout):
            raise urllib.error.URLError("connection refused")

        with patch.object(llm_client.urllib.request, "urlopen", boom):
            result = llm_client.chat(model="m", system="s", user="u")

        self.assertFalse(result.ok)
        self.assertIn("unreachable", result.error)


class OpenAIBackendTests(unittest.TestCase):
    def setUp(self):
        for attr, value in (("LLM_PROVIDER", "openai"),
                            ("LLM_BASE_URL", "https://example.test/v4"),
                            ("LLM_API_KEY", "test-key"),
                            ("LLM_EXTRA_BODY", "")):
            p = patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_sends_bearer_key_to_chat_completions(self):
        captured = {}

        def fake(req, timeout):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            captured["body"] = json.loads(req.data.decode())
            return _response({"choices": [{"message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": 5, "completion_tokens": 7}})

        with patch.object(llm_client.urllib.request, "urlopen", fake):
            result = llm_client.chat(model="glm-4.6", system="s", user="u", temperature=0.3)

        self.assertEqual(captured["url"], "https://example.test/v4/chat/completions")
        self.assertEqual(captured["auth"], "Bearer test-key")
        self.assertEqual(captured["body"]["model"], "glm-4.6")
        self.assertTrue(result.ok)
        self.assertEqual(result.usage.completion_tokens, 7)

    def test_missing_key_fails_before_any_request(self):
        with patch.object(config, "LLM_API_KEY", ""):
            with patch.object(llm_client.urllib.request, "urlopen",
                              lambda *a, **k: self.fail("must not send a request")):
                result = llm_client.chat(model="m", system="s", user="u")
        self.assertIn("LLM_API_KEY", result.error)

    def test_json_schema_rejection_falls_back_to_json_object(self):
        """
        Structured-output support is uneven across OpenAI-compatible providers.
        A 400 on the schema form must degrade to json_object rather than fail
        the whole enrichment.
        """
        modes = []

        def fake(req, timeout):
            body = json.loads(req.data.decode())
            modes.append(body["response_format"]["type"])
            if body["response_format"]["type"] == "json_schema":
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", {},
                    io.BytesIO(b'{"error":"response_format not supported"}'),
                )
            return _response({"choices": [{"message": {"content": '{"a": 1}'}}],
                              "usage": {}})

        with patch.object(llm_client.urllib.request, "urlopen", fake):
            result = llm_client.chat(model="m", system="s", user="u",
                                     schema={"type": "object"})

        self.assertEqual(modes, ["json_schema", "json_object"])
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"a": 1})

    def test_fenced_json_is_unwrapped(self):
        payload = {"choices": [{"message": {"content": '```json\n{"a": 2}\n```'}}],
                   "usage": {}}
        with patch.object(llm_client.urllib.request, "urlopen",
                          lambda req, timeout: _response(payload)):
            result = llm_client.chat(model="m", system="s", user="u",
                                     schema={"type": "object"})
        self.assertEqual(result.data, {"a": 2})

    def test_extra_body_is_merged_into_the_request(self):
        captured = {}

        def fake(req, timeout):
            captured.update(json.loads(req.data.decode()))
            return _response({"choices": [{"message": {"content": "x"}}], "usage": {}})

        with patch.object(config, "LLM_EXTRA_BODY", '{"thinking": {"type": "disabled"}}'), \
                patch.object(llm_client.urllib.request, "urlopen", fake):
            llm_client.chat(model="m", system="s", user="u")

        self.assertEqual(captured["thinking"], {"type": "disabled"})

    def test_malformed_extra_body_is_ignored_not_fatal(self):
        with patch.object(config, "LLM_EXTRA_BODY", "not json"), \
                patch.object(llm_client.urllib.request, "urlopen",
                             lambda req, timeout: _response(
                                 {"choices": [{"message": {"content": "x"}}], "usage": {}})):
            result = llm_client.chat(model="m", system="s", user="u")
        self.assertTrue(result.ok)


class UsageTests(unittest.TestCase):
    def test_add_accumulates_across_calls(self):
        total = llm_client.Usage(calls=0)
        total.add(llm_client.Usage(prompt_tokens=10, completion_tokens=5))
        total.add(llm_client.Usage(prompt_tokens=3, completion_tokens=2))
        self.assertEqual(total.prompt_tokens, 13)
        self.assertEqual(total.completion_tokens, 7)
        self.assertEqual(total.calls, 2)


if __name__ == "__main__":
    unittest.main()

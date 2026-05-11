"""
End-to-end tests for LLM warmup and ChatOpenAI client caching.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from types import SimpleNamespace

import server
from config import LLMConfig, MeetingConfig
from session import MeetingSession


# ── Warmup Tests ────────────────────────────────────────────────────────────


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    async def test_warmup_sends_minimal_request_to_llamacpp_server(self):
        """warmup_llm() should POST a minimal chat completion to pre-allocate KV cache."""
        captured = {}

        mock_response = MagicMock()
        mock_response.status_code = 200

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json=None, **kwargs):
                captured["url"] = url
                captured["json"] = json
                return mock_response

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await server.warmup_llm()

        self.assertIn("/v1/chat/completions", captured["url"])
        self.assertEqual(captured["json"]["model"], server.llm_config.model_name)
        self.assertEqual(captured["json"]["max_tokens"], 1)
        self.assertEqual(captured["json"]["messages"], [{"role": "user", "content": "hi"}])

    async def test_warmup_targets_configured_llamacpp_url(self):
        """warmup_llm() should use llamacpp_base_url from config, not a hardcoded URL."""
        captured = {}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json=None, **kwargs):
                captured["url"] = url
                return MagicMock(status_code=200)

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await server.warmup_llm()

        self.assertTrue(captured["url"].startswith(server.llm_config.base_url))

    async def test_warmup_failure_does_not_raise(self):
        """warmup_llm() should swallow errors so startup is not blocked."""
        class FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                raise ConnectionError("llama.cpp server not running")

        with patch("httpx.AsyncClient", return_value=FailingClient()):
            # Should not raise
            await server.warmup_llm()

    async def test_warmup_called_on_startup_when_llm_is_ok(self):
        """on_startup() should invoke warmup_llm() after a successful LLM health check."""
        with patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "warmup_llm", new_callable=AsyncMock) as mock_warmup, \
             patch.object(server, "refresh_runtime_snapshot"), \
             patch.object(server.meeting_store, "init_preset_templates"), \
             patch.object(server.asr_service, "initialize", new_callable=AsyncMock):
            await server.on_startup()

        mock_warmup.assert_awaited_once()

    async def test_warmup_skipped_when_llm_is_unreachable(self):
        """on_startup() should NOT call warmup_llm() when LLM health check fails."""
        with patch.object(server, "get_llm_status", return_value="unreachable: connection refused"), \
             patch.object(server, "warmup_llm", new_callable=AsyncMock) as mock_warmup, \
             patch.object(server, "refresh_runtime_snapshot"), \
             patch.object(server.meeting_store, "init_preset_templates"), \
             patch.object(server.asr_service, "initialize", new_callable=AsyncMock):
            await server.on_startup()

        mock_warmup.assert_not_awaited()


# ── ChatOpenAI Caching Tests ────────────────────────────────────────────────


class ChatModelCachingTests(unittest.TestCase):
    def setUp(self):
        self.llm_config = LLMConfig(provider="llamacpp", local_only=True)
        self.meeting_config = MeetingConfig()
        self.session = MeetingSession("test-cache", self.llm_config, self.meeting_config)

    def test_same_temperature_returns_same_instance(self):
        """_create_chat_model() should return the same object for the same temperature."""
        client_a = self.session._create_chat_model(temperature=0.7)
        client_b = self.session._create_chat_model(temperature=0.7)

        self.assertIs(client_a, client_b)

    def test_different_temperatures_return_different_instances(self):
        """_create_chat_model() should return distinct objects for different temperatures."""
        client_hot = self.session._create_chat_model(temperature=0.7)
        client_cold = self.session._create_chat_model(temperature=0.2)

        self.assertIsNot(client_hot, client_cold)

    def test_cache_holds_expected_number_of_entries(self):
        """Only one entry per unique temperature should be stored."""
        self.session._create_chat_model(0.7)
        self.session._create_chat_model(0.2)
        self.session._create_chat_model(0.7)  # duplicate, should reuse

        self.assertEqual(len(self.session._chat_model_cache), 2)
        self.assertIn(0.7, self.session._chat_model_cache)
        self.assertIn(0.2, self.session._chat_model_cache)

    def test_cached_instance_uses_configured_base_url(self):
        """Cached ChatOpenAI should point to llamacpp_base_url, not a default."""
        client = self.session._create_chat_model(temperature=0.7)

        self.assertEqual(client.openai_api_base, self.llm_config.base_url)
        self.assertEqual(client.model_name, self.llm_config.model_name)

    def test_cache_is_per_session_not_shared(self):
        """Each MeetingSession should have its own independent cache."""
        session_a = MeetingSession("a", self.llm_config, self.meeting_config)
        session_b = MeetingSession("b", self.llm_config, self.meeting_config)

        client_a = session_a._create_chat_model(0.7)
        client_b = session_b._create_chat_model(0.7)

        self.assertIsNot(client_a, client_b)
        self.assertIs(session_a._chat_model_cache[0.7], client_a)
        self.assertIs(session_b._chat_model_cache[0.7], client_b)

    def test_cached_client_is_reused_in_real_llm_flow(self):
        """ask_llm() and stream_llm_answer() should use the cached client."""
        call_count = {"n": 0}
        original_client = None

        class SpyLLM:
            async def ainvoke(self, _messages):
                call_count["n"] += 1
                return SimpleNamespace(content="ok")

            async def astream(self, _messages):
                call_count["n"] += 1
                yield SimpleNamespace(content="ok")

        original_create = self.session._create_chat_model

        def spy_create(temperature):
            nonlocal original_client
            client = original_create(temperature)
            if original_client is None:
                original_client = client
            else:
                self.assertIs(client, original_client, "Expected cached instance to be reused")
            return SpyLLM()

        self.session._create_chat_model = spy_create

        # Run two sequential async calls
        async def run():
            await self.session.ask_llm("question 1")
            async for _ in self.session.stream_llm_answer("question 2"):
                pass

        asyncio.run(run())
        self.assertEqual(call_count["n"], 2)


# ── Integration: First-request TTFT simulation ──────────────────────────────


class WarmupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_warmup_then_real_call_uses_same_server_endpoint(self):
        """Verify warmup target and real LLM call target are identical."""
        warmup_url = {}

        class TrackClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, json=None, **kwargs):
                warmup_url["val"] = url
                return MagicMock(status_code=200)

        with patch("httpx.AsyncClient", return_value=TrackClient()):
            await server.warmup_llm()

        session = MeetingSession("integration", server.llm_config, server.meeting_config)
        client = session._create_chat_model(0.7)

        # ChatOpenAI builds the URL as base_url + /chat/completions
        self.assertIn(str(client.openai_api_base), warmup_url["val"])


if __name__ == "__main__":
    unittest.main()

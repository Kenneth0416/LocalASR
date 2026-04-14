"""
Tests for SessionManager recovery registration.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient

from config import LLMConfig, MeetingConfig
from persistence import MeetingStore
from session import MeetingSession, SessionManager
import server


class SessionManagerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.llm_config = LLMConfig(provider="ollama", local_only=True)
        self.meeting_config = MeetingConfig()
        self.manager = SessionManager(self.llm_config, self.meeting_config)

    def test_register_recovered_session_rewrites_identifier_and_clears_stale_keys(self):
        session = MeetingSession("temporary-session", self.llm_config, self.meeting_config)
        other_session = MeetingSession("other-session", self.llm_config, self.meeting_config)

        self.manager.sessions["temporary-session"] = session
        self.manager.sessions["legacy-session"] = session
        self.manager.sessions["other-session"] = other_session

        recovered = self.manager.register_recovered_session("persisted-session", session)

        self.assertIs(recovered, session)
        self.assertEqual(session.session_id, "persisted-session")
        self.assertNotIn("temporary-session", self.manager.sessions)
        self.assertNotIn("legacy-session", self.manager.sessions)
        self.assertIn("persisted-session", self.manager.sessions)
        self.assertIs(self.manager.sessions["persisted-session"], session)
        self.assertIn("other-session", self.manager.sessions)
        self.assertIs(self.manager.sessions["other-session"], other_session)

    def test_create_chat_model_blocks_remote_provider_in_local_only_mode(self):
        llm_config = LLMConfig(
            provider="openai",
            local_only=True,
            openai_api_key="sk-test",
            openai_base_url="https://api.openai.com/v1",
        )
        session = MeetingSession("temporary-session", llm_config, self.meeting_config)

        with self.assertRaises(RuntimeError) as ctx:
            session._create_chat_model(temperature=0.2)

        self.assertIn("local-only", str(ctx.exception))
        self.assertIn("offline", str(ctx.exception))

    def test_get_llm_status_blocks_remote_probe_when_local_only(self):
        blocked_config = LLMConfig(
            provider="openai",
            local_only=True,
            openai_api_key="sk-test",
            openai_base_url="https://api.openai.com/v1",
        )

        with patch.object(server, "llm_config", blocked_config), \
             patch.object(server.requests, "get") as mock_get:
            status = server.get_llm_status()

        self.assertTrue(status.startswith("blocked:"))
        self.assertIn("local-only", status)
        mock_get.assert_not_called()

    def test_chat_rehydration_restores_persisted_summary_into_session_context(self):
        with TemporaryDirectory() as tmpdir:
            store = MeetingStore(Path(tmpdir) / "meetings.sqlite3")
            session = MeetingSession("persisted-1234", self.llm_config, self.meeting_config, store=store)
            session.add_transcript_segment("Speaker 1", "First transcript line", 0.0, 1.0)
            session.add_chat_message("user", "What happened?")
            store.upsert_summary(
                session.session_id,
                session.created_at,
                "Stored summary",
                "2026-04-08T10:00:00",
                1,
            )
            store.complete_session(
                session.session_id,
                session.created_at,
                "2026-04-08T10:01:00",
                recording_path=None,
                recording_bytes=0,
                recording_error=None,
            )

            captured = {}

            async def fake_ask_llm(self, question):
                captured["summary"] = self.summary.text if self.summary else None
                captured["context"] = self.get_context_for_llm()
                captured["question"] = question
                return "ok"

            with patch.object(server, "meeting_store", store), \
                 patch.object(server.session_manager, "store", store), \
                 patch.object(MeetingSession, "ask_llm", new=fake_ask_llm):
                with TestClient(server.app) as client:
                    response = client.post(
                        f"/api/meetings/{session.session_id}/chat",
                        json={"question": "What is the summary?"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(captured["summary"], "Stored summary")
            self.assertIn("Stored summary", captured["context"])
            self.assertIn("【会议摘要】", captured["context"])
            self.assertEqual(captured["question"], "What is the summary?")


if __name__ == "__main__":
    unittest.main()

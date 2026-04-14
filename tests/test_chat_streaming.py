import unittest
from types import SimpleNamespace

import server
from config import LLMConfig, MeetingConfig
from session import MeetingSession


class _FakeLLM:
    async def astream(self, _messages):
        for part in ["流", "式", "回", "答"]:
            yield SimpleNamespace(content=part)

    async def ainvoke(self, _messages):
        return SimpleNamespace(content="完整回答")


class _FakeEmptyStreamingLLM:
    async def astream(self, _messages):
        yield SimpleNamespace(content="")

    async def ainvoke(self, _messages):
        return SimpleNamespace(content="回退回答")


class _FakeStreamingSession:
    def __init__(self):
        self.chat_history = []
        self.added_messages = []

    async def stream_llm_answer(self, _question):
        for part in ["流", "式", "回", "答"]:
            yield part

    def add_chat_message(self, role, content, message_id=None):
        message = SimpleNamespace(id=message_id or f"{role}_id", role=role, content=content)
        self.chat_history.append(message)
        self.added_messages.append(message)
        return message


class _FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class MeetingSessionStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_llm_answer_uses_langchain_astream(self):
        session = MeetingSession("session", LLMConfig(), MeetingConfig())
        session._create_chat_model = lambda temperature: _FakeLLM()

        chunks = [chunk async for chunk in session.stream_llm_answer("现在会议讲了什么")]

        self.assertEqual(chunks, ["流", "式", "回", "答"])

    async def test_ask_llm_uses_langchain_ainvoke(self):
        session = MeetingSession("session", LLMConfig(), MeetingConfig())
        session._create_chat_model = lambda temperature: _FakeLLM()

        answer = await session.ask_llm("请总结一下")

        self.assertEqual(answer, "完整回答")

    async def test_stream_llm_answer_falls_back_when_stream_is_empty(self):
        session = MeetingSession("session", LLMConfig(), MeetingConfig())
        session._create_chat_model = lambda temperature: _FakeEmptyStreamingLLM()

        chunks = [chunk async for chunk in session.stream_llm_answer("现在会议讲了什么")]

        self.assertEqual(chunks, ["回退回答"])


class ServerChatStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_chat_emits_stream_events_and_persists_history(self):
        session = _FakeStreamingSession()
        websocket = _FakeWebSocket()

        await server.handle_chat(session, "现在到哪一步了？", websocket)

        event_types = [message["type"] for message in websocket.messages]
        self.assertEqual(
            event_types,
            [
                "chat_status",
                "chat_stream_start",
                "chat_stream_delta",
                "chat_stream_delta",
                "chat_stream_delta",
                "chat_stream_delta",
                "chat_response",
            ],
        )
        self.assertEqual(session.added_messages[0].role, "user")
        self.assertEqual(session.added_messages[0].content, "现在到哪一步了？")
        self.assertEqual(session.added_messages[1].role, "assistant")
        self.assertEqual(session.added_messages[1].content, "流式回答")

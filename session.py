"""
Meeting Session Manager - Handles meeting context and LLM interactions.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional
from uuid import uuid4

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from config import LLMConfig, MeetingConfig

if TYPE_CHECKING:
    from persistence import MeetingStore

logger = logging.getLogger("meeting.session")

CHAT_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "你是一个会议助手，正在参与或旁听一场会议。"
        "你只能基于给出的会议上下文回答问题；如果上下文里没有答案，就明确说明。",
    ),
    (
        "human",
        "会议上下文：\n{context}\n\n用户问题：{question}\n\n"
        "请简洁、准确地回答。如果会议内容中没有相关信息，请如实说明。",
    ),
])

SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "你是一个会议记录助手。请根据会议转录生成简明摘要，优先保留决策、结论和待办事项。",
    ),
    (
        "human",
        "请为以下会议内容生成简短摘要，包括：\n"
        "1. 会议主题\n"
        "2. 主要讨论内容\n"
        "3. 已达成的一致意见\n"
        "4. 待解决或待跟进的事项\n\n"
        "会议内容：\n{transcript_text}\n\n"
        "请用简洁的要点格式输出摘要。",
    ),
])


@dataclass
class TranscriptSegment:
    """A segment of transcript"""

    id: str
    speaker: str
    text: str
    start_time: float
    end_time: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ChatMessage:
    """A chat message"""

    id: str
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class MeetingSummary:
    """Meeting summary"""

    text: str
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    turn_count: int = 0


class MeetingSession:
    """
    Manages a meeting session with transcript history and chat context.
    Non-blocking LLM calls to ensure transcription continues.
    """

    def __init__(
        self,
        session_id: str,
        llm_config: LLMConfig,
        meeting_config: MeetingConfig,
        store: Optional["MeetingStore"] = None,
    ):
        self.session_id = session_id
        self.llm_config = llm_config
        self.meeting_config = meeting_config
        self.store = store

        self.transcript: list[TranscriptSegment] = []
        self.chat_history: list[ChatMessage] = []
        self.summary: Optional[MeetingSummary] = None
        self.created_at = datetime.now().isoformat()

        self._transcript_lock = asyncio.Lock()

    def add_transcript_segment(self, speaker: str, text: str, start_time: float, end_time: float) -> TranscriptSegment:
        """Add a transcript segment (thread-safe)"""
        segment = TranscriptSegment(
            id=str(uuid4())[:8],
            speaker=speaker,
            text=text,
            start_time=start_time,
            end_time=end_time,
        )
        self.transcript.append(segment)
        if self.store is not None:
            self.store.insert_transcript_segment(
                self.session_id,
                self.created_at,
                segment,
                ordinal=len(self.transcript),
            )
        return segment

    def add_chat_message(self, role: str, content: str, message_id: Optional[str] = None) -> ChatMessage:
        """Append a chat message to session history."""
        message = ChatMessage(
            id=message_id or str(uuid4())[:8],
            role=role,
            content=content,
        )
        self.chat_history.append(message)
        if self.store is not None:
            self.store.insert_chat_message(
                self.session_id,
                self.created_at,
                message,
                ordinal=len(self.chat_history),
            )
        return message

    def get_transcript_text(self, max_chars: Optional[int] = None) -> str:
        """Get formatted transcript text for LLM context"""
        lines = []
        for seg in self.transcript:
            time_str = f"[{seg.start_time:.1f}s-{seg.end_time:.1f}s]"
            lines.append(f"{seg.speaker} {time_str}: {seg.text}")

        full_text = "\n".join(lines)

        if max_chars and len(full_text) > max_chars:
            # Truncate from the beginning, keep most recent
            full_text = "..." + full_text[-(max_chars - 3):]

        return full_text

    def get_context_for_llm(self) -> str:
        """Build context string for LLM with transcript + chat history"""
        parts = []

        # Meeting transcript
        transcript_text = self.get_transcript_text(max_chars=self.meeting_config.max_context_chars)
        if transcript_text:
            parts.append(f"【会议转录】\n{transcript_text}")

        # Recent chat history
        if self.chat_history:
            chat_lines = []
            for msg in self.chat_history[-10:]:  # Last 10 messages
                role_name = "用户" if msg.role == "user" else "AI"
                chat_lines.append(f"{role_name}: {msg.content}")
            parts.append(f"【问答历史】\n" + "\n".join(chat_lines))

        # Current summary if available
        if self.summary:
            parts.append(f"【会议摘要】\n{self.summary.text}")

        return "\n\n".join(parts) if parts else "（暂无会议内容）"

    async def ask_llm(self, question: str) -> str:
        """
        Ask LLM a question based on meeting context.
        Non-blocking - uses LangChain async invocation.
        """
        try:
            prompt = CHAT_PROMPT.format_messages(
                context=self.get_context_for_llm(),
                question=question,
            )
            response = await self._create_chat_model(temperature=0.7).ainvoke(prompt)
            return self._extract_text(response.content).strip()
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return f"抱歉，AI 服务暂时不可用: {str(e)}"

    async def stream_llm_answer(self, question: str) -> AsyncIterator[str]:
        """Stream LLM answer tokens via LangChain."""
        prompt = CHAT_PROMPT.format_messages(
            context=self.get_context_for_llm(),
            question=question,
        )
        llm = self._create_chat_model(temperature=0.7)
        yielded_parts: list[str] = []

        try:
            async for chunk in llm.astream(prompt):
                text = self._extract_text(getattr(chunk, "content", ""))
                if text:
                    yielded_parts.append(text)
                    yield text

            if yielded_parts:
                return

            logger.warning("Streaming LLM returned no visible content; falling back to ainvoke")
            fallback = await llm.ainvoke(prompt)
            fallback_text = self._extract_text(getattr(fallback, "content", fallback)).strip()
            if fallback_text:
                yield fallback_text
                return

            raise RuntimeError("AI 返回了空内容")
        except Exception as e:
            logger.error(f"Streaming LLM call failed: {e}")
            raise RuntimeError(f"AI 服务暂时不可用: {e}") from e

    async def update_summary(self) -> Optional[str]:
        """
        Generate/update meeting summary.
        Non-blocking.
        """
        if len(self.transcript) < 3:
            return None

        try:
            transcript_text = self.get_transcript_text(max_chars=20000)
            prompt = SUMMARY_PROMPT.format_messages(transcript_text=transcript_text)
            response = await self._create_chat_model(temperature=0.2).ainvoke(prompt)
            summary_text = self._extract_text(response.content)

            self.summary = MeetingSummary(
                text=summary_text.strip(),
                turn_count=len(self.transcript),
            )
            if self.store is not None:
                self.store.upsert_summary(
                    self.session_id,
                    self.created_at,
                    self.summary.text,
                    self.summary.updated_at,
                    self.summary.turn_count,
                )
            return self.summary.text

        except Exception as e:
            logger.error(f"Summary generation failed: {e}")
            return None

    def _create_chat_model(self, temperature: float):
        """Create a LangChain chat model for the configured provider."""
        if self.llm_config.local_only and not self.llm_config.is_local_provider():
            raise RuntimeError(
                f"local-only mode is enabled, so remote provider '{self.llm_config.provider}' "
                "is not available offline."
            )

        if self.llm_config.is_openai_compatible():
            return ChatOpenAI(
                model=self.llm_config.model_name,
                api_key=self.llm_config.openai_api_key or "unused",
                base_url=self.llm_config.base_url,
                temperature=temperature,
                timeout=60,
                max_retries=0,
                streaming=True,
            )

        return ChatOllama(
            model=self.llm_config.ollama_model,
            base_url=self.llm_config.ollama_base_url,
            temperature=temperature,
            reasoning=False,
            num_predict=512,
            client_kwargs={"timeout": 60},
            async_client_kwargs={"timeout": 60},
        )

    def _extract_text(self, content: Any) -> str:
        """Normalize LangChain message content into plain text."""
        if isinstance(content, str):
            return content

        if isinstance(content, (AIMessage, AIMessageChunk)):
            return self._extract_text(content.content)

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(self._extract_text(item))
            return "".join(parts)

        if isinstance(content, dict):
            if content.get("type") == "text":
                return str(content.get("text", ""))
            return str(content.get("content", ""))

        return str(content) if content is not None else ""

    def to_dict(self) -> dict:
        """Export session state for frontend"""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "transcript_count": len(self.transcript),
            "chat_count": len(self.chat_history),
            "summary": self.summary.text if self.summary else None,
        }


class SessionManager:
    """Manages multiple meeting sessions"""

    def __init__(self, llm_config: LLMConfig, meeting_config: MeetingConfig):
        self.llm_config = llm_config
        self.meeting_config = meeting_config
        self.sessions: dict[str, MeetingSession] = {}
        self.store: Optional["MeetingStore"] = None

    def create_session(self) -> MeetingSession:
        """Create a new meeting session"""
        session_id = str(uuid4())[:8]
        session = MeetingSession(session_id, self.llm_config, self.meeting_config, store=self.store)
        self.sessions[session_id] = session
        logger.info(f"Created new meeting session: {session_id}")
        return session

    def get_session(self, session_id: str) -> Optional[MeetingSession]:
        """Get existing session"""
        return self.sessions.get(session_id)

    def register_recovered_session(self, session_id: str, session: MeetingSession) -> MeetingSession:
        """Register a rehydrated session under its persisted identifier."""
        stale_keys = [
            key
            for key, existing in self.sessions.items()
            if existing is session and key != session_id
        ]
        for key in stale_keys:
            del self.sessions[key]

        session.session_id = session_id
        self.sessions[session_id] = session
        return session

    def delete_session(self, session_id: str) -> bool:
        """Delete a session"""
        if session_id in self.sessions:
            del self.sessions[session_id]
            logger.info(f"Deleted meeting session: {session_id}")
            return True
        return False

    def list_sessions(self) -> list[dict]:
        """List all sessions"""
        return [s.to_dict() for s in self.sessions.values()]

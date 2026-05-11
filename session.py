"""
Meeting Session Manager - Handles meeting context and LLM interactions.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional
from uuid import uuid4

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from config import LLMConfig, MeetingConfig

if TYPE_CHECKING:
    from persistence import MeetingStore

logger = logging.getLogger("meeting.session")

CHAT_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "You are a meeting assistant participating in or observing a meeting. "
        "You may only answer questions based on the provided meeting context; "
        "if the answer is not in the context, say so clearly.",
    ),
    (
        "human",
        "Meeting context:\n{context}\n\nUser question: {question}\n\n"
        "Please answer concisely and accurately. If the relevant information is not in the meeting content, say so.",
    ),
])

SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "你是一位专业的会议记录员，擅长从会议转录中提取结构化摘要。"
        "你的摘要必须保留所有具体数据、数字指标、说话人归属、分歧观点和技术细节。"
        "不要泛泛概括，要保留能回答具体问题的关键信息。",
    ),
    (
        "human",
        "请根据以下会议转录生成结构化摘要，严格遵循以下格式：\n\n"
        "## 会议主题\n（一句话概括）\n\n"
        "## 关键数据与指标\n（列出所有提到的具体数字、百分比、金额、时间等）\n\n"
        "## 议题与解决方案\n（每个议题必须包含：问题描述→具体方案→负责人，保持问题和方案的对应关系）\n\n"
        "## 分歧与争论\n（如有不同意见，列出双方观点和各自论据）\n\n"
        "## 决策与共识\n（明确列出达成的决定及依据）\n\n"
        "## 行动项\n（格式：[负责人] 任务内容 — 截止时间）\n\n"
        "## 待跟进事项\n（未解决的问题和后续计划）\n\n"
        "重要规则：\n"
        "- 必须保留原始发言中的具体数字和数据\n"
        "- 每个议题的问题和解决方案必须配对出现，不要只写问题不写方案\n"
        "- 保留说话人（如A、B、C）的具体观点归属\n\n"
        "会议转录：\n{transcript_text}",
    ),
])

REWRITE_SYSTEM = (
    "你是一位会议转录改写员。将每段转录改写为最简洁版本。\n"
    "规则：\n"
    "1. 保留所有数字、百分比、金额、人名、缩写\n"
    "2. 保留所有决策、提议、反对意见、问题\n"
    "3. 去掉语气词（嗯、啊、呃、那个、就是说）\n"
    "4. 去掉客套和过渡语（我建议把、我发现一个问题：、大家的意见都很好）\n"
    "5. 精简句子结构，用最少的字表达同样意思\n"
    "6. 不要省略任何事实信息，不要添加原文没有的内容\n"
    "7. 严格保持原段落顺序和数量，每行一段\n"
    "8. 输出格式：[时间] 改写后的文本（不要加编号或其他前缀）"
)

INCREMENTAL_SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        "你是一位专业的会议记录员。你将收到一份已有的会议摘要和新增的会议内容。"
        "请将新内容合并到已有摘要中，更新相关部分。"
        "合并规则：\n"
        "1. 保留已有摘要中仍然相关的信息\n"
        "2. 将新内容按主题归入对应章节\n"
        "3. 如新内容推翻了旧决策，更新决策并注明变更\n"
        "4. 必须保留所有具体数字、数据指标和说话人归属\n"
        "5. 新增的行动项追加到行动项列表，注明负责人\n"
        "6. 合并后更新关键数据与指标部分",
    ),
    (
        "human",
        "## 已有摘要\n{existing_summary}\n\n"
        "## 新增会议内容\n{new_transcript}\n\n"
        "请将新增内容合并到已有摘要中，保持相同的结构格式。输出完整的更新后摘要。",
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
    capture_start_time: float = 0.0
    capture_duration: float = 0.0
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
        self.template: Optional[dict] = None
        self.last_summarized_ordinal: int = 0
        self.created_at = datetime.now().isoformat()

        # Rewrite state: maps segment index → rewritten text
        self.rewritten_segments: dict[int, str] = {}
        self._last_rewritten_idx: int = 0
        self._rewriting_lock = asyncio.Lock()

        self._chat_model_cache: dict[float, ChatOpenAI] = {}
        self._transcript_lock = asyncio.Lock()

    def add_transcript_segment(
        self,
        speaker: str,
        text: str,
        start_time: float,
        end_time: float,
        *,
        capture_start_time: float | None = None,
        capture_duration: float | None = None,
    ) -> TranscriptSegment:
        """Add a transcript segment (thread-safe)"""
        resolved_capture_start = float(capture_start_time if capture_start_time is not None else start_time)
        resolved_capture_duration = float(
            capture_duration if capture_duration is not None else max(end_time - start_time, 0.0)
        )
        segment = TranscriptSegment(
            id=str(uuid4())[:8],
            speaker=speaker,
            text=text,
            start_time=start_time,
            end_time=end_time,
            capture_start_time=resolved_capture_start,
            capture_duration=resolved_capture_duration,
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

    @staticmethod
    def _format_segment(seg: TranscriptSegment, include_speaker: bool = False) -> str:
        """Format a single segment with compact timestamps and no speaker label."""
        start = int(seg.start_time) if seg.start_time == int(seg.start_time) else seg.start_time
        end = int(seg.end_time) if seg.end_time == int(seg.end_time) else seg.end_time
        if include_speaker:
            return f"[{start}-{end}] {seg.speaker}: {seg.text}"
        return f"[{start}-{end}] {seg.text}"

    def get_transcript_text(self, max_chars: Optional[int] = None) -> str:
        """Get formatted transcript text for LLM context (compact: no speaker labels)."""
        lines = [self._format_segment(seg) for seg in self.transcript]
        full_text = "\n".join(lines)

        if max_chars and len(full_text) > max_chars:
            full_text = "..." + full_text[-(max_chars - 3):]

        return full_text

    def get_new_transcript_text(self, max_chars: Optional[int] = None) -> str:
        """Get formatted transcript text for segments after last_summarized_ordinal."""
        new_segments = self.transcript[self.last_summarized_ordinal:]
        lines = [self._format_segment(seg) for seg in new_segments]
        full_text = "\n".join(lines)

        if max_chars and len(full_text) > max_chars:
            full_text = "..." + full_text[-(max_chars - 3):]

        return full_text

    def get_recent_transcript_text(self, max_minutes: int = 5) -> str:
        """Get formatted transcript text for segments within the last N minutes."""
        if not self.transcript:
            return ""
        latest_time = self.transcript[-1].end_time
        cutoff = latest_time - (max_minutes * 60)
        recent = [s for s in self.transcript if s.end_time >= cutoff]
        return "\n".join(self._format_segment(seg) for seg in recent)

    @staticmethod
    def _parse_rewritten_lines(raw: str) -> list[str]:
        """Parse LLM rewrite output, stripping any numbering or labels."""
        import re
        lines = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Strip leading numbering: "1. ", "1) ", "- ", "• "
            line = re.sub(r'^[\d]+[\.\)]\s*', '', line)
            line = re.sub(r'^[-•]\s*', '', line)
            # Strip markdown bold markers
            line = line.replace('**', '')
            if line:
                lines.append(line)
        return lines

    async def rewrite_segments_chunked(self, chunk_size: int = 10, overlap: int = 2) -> int:
        """Rewrite transcript segments in overlapping chunks using LLM.

        Each chunk includes `overlap` segments from the previous chunk for
        semantic continuity. Only the non-overlapping new segments are stored.

        Args:
            chunk_size: number of new segments per chunk
            overlap: number of segments to carry over from previous chunk

        Returns:
            number of segments rewritten in this call
        """
        total = len(self.transcript)
        if total <= self._last_rewritten_idx:
            return 0

        async with self._rewriting_lock:
            # Re-check after acquiring lock (another task may have finished)
            total = len(self.transcript)
            if total <= self._last_rewritten_idx:
                return 0

            chunks_rewritten = 0

            while self._last_rewritten_idx < total:
                start = self._last_rewritten_idx
                end = min(start + chunk_size, total)
                new_segments = self.transcript[start:end]

                # Build overlap context from the end of previous chunk
                overlap_lines = []
                if start > 0 and overlap > 0:
                    ol_start = max(0, start - overlap)
                    for seg in self.transcript[ol_start:start]:
                        overlap_lines.append(self._format_segment(seg))

                # Assemble prompt
                parts = []
                if overlap_lines:
                    parts.append("【上下文，已处理过，仅供语境参考】")
                    parts.extend(overlap_lines)
                    parts.append("")
                parts.append("【需要改写的新段落】")
                for seg in new_segments:
                    parts.append(self._format_segment(seg))
                user_msg = "\n".join(parts)

                try:
                    llm = self._create_chat_model(temperature=0.2)
                    from langchain_core.messages import SystemMessage, HumanMessage
                    response = await llm.ainvoke([
                        SystemMessage(content=REWRITE_SYSTEM),
                        HumanMessage(content=user_msg),
                    ])
                    raw = self._extract_text(response.content)
                    rewritten = self._parse_rewritten_lines(raw)

                    # Store only the new (non-overlapping) segments
                    for i, text in enumerate(rewritten):
                        idx = start + i
                        if idx < total:
                            self.rewritten_segments[idx] = text

                    chunks_rewritten += 1
                    logger.info(f"Rewrote chunk {chunks_rewritten}: segments {start}-{end}")

                except Exception as e:
                    logger.error(f"Rewrite failed for chunk {start}-{end}: {e}")
                    # On failure, mark these segments as processed (use original text)
                    for i in range(start, end):
                        self.rewritten_segments[i] = self.transcript[i].text

                self._last_rewritten_idx = end

            return chunks_rewritten

    def get_rewritten_transcript_text(self, max_chars: Optional[int] = None) -> str:
        """Get transcript text using rewritten versions where available, original otherwise."""
        lines = []
        for i, seg in enumerate(self.transcript):
            start = int(seg.start_time) if seg.start_time == int(seg.start_time) else seg.start_time
            end = int(seg.end_time) if seg.end_time == int(seg.end_time) else seg.end_time
            text = self.rewritten_segments.get(i, seg.text)
            lines.append(f"[{start}-{end}] {text}")
        full_text = "\n".join(lines)
        if max_chars and len(full_text) > max_chars:
            full_text = "..." + full_text[-(max_chars - 3):]
        return full_text

    def _get_recent_rewritten_text(self, max_minutes: int = 5) -> str:
        """Get recent segments using rewritten versions where available."""
        if not self.transcript:
            return ""
        latest_time = self.transcript[-1].end_time
        cutoff = latest_time - (max_minutes * 60)
        lines = []
        for i, seg in enumerate(self.transcript):
            if seg.end_time < cutoff:
                continue
            start = int(seg.start_time) if seg.start_time == int(seg.start_time) else seg.start_time
            end = int(seg.end_time) if seg.end_time == int(seg.end_time) else seg.end_time
            text = self.rewritten_segments.get(i, seg.text)
            lines.append(f"[{start}-{end}] {text}")
        return "\n".join(lines)

    def get_context_for_llm(self) -> str:
        """Build context string for LLM with transcript + chat history.

        When use_progressive_context is enabled and a summary exists,
        sends only the summary + recent transcript window instead of the
        full transcript — dramatically reducing TTFT for long meetings.
        """
        parts = []
        cfg = self.meeting_config

        # Choose text source: rewritten if available, otherwise original
        has_rewrites = bool(self.rewritten_segments)

        if cfg.use_progressive_context and self.summary and self.summary.text.strip():
            # Progressive mode: summary + recent N minutes
            if has_rewrites:
                recent_text = self._get_recent_rewritten_text(cfg.recent_window_minutes)
            else:
                recent_text = self.get_recent_transcript_text(cfg.recent_window_minutes)
            if recent_text:
                parts.append(f"【最近转录（近{cfg.recent_window_minutes}分钟）】\n{recent_text}")
            parts.append(f"【会议摘要】\n{self.summary.text}")
        else:
            # Fallback: full transcript (no summary yet, or feature disabled)
            if has_rewrites:
                transcript_text = self.get_rewritten_transcript_text(max_chars=cfg.max_context_chars)
            else:
                transcript_text = self.get_transcript_text(max_chars=cfg.max_context_chars)
            if transcript_text:
                parts.append(f"【会议转录】\n{transcript_text}")
            if self.summary and self.summary.text.strip():
                parts.append(f"【会议摘要】\n{self.summary.text}")

        # Recent chat history (always included)
        if self.chat_history:
            chat_lines = []
            for msg in self.chat_history[-10:]:
                role_name = "你" if msg.role == "user" else "AI"
                chat_lines.append(f"{role_name}: {msg.content}")
            parts.append(f"【问答记录】\n" + "\n".join(chat_lines))

        return "\n\n".join(parts) if parts else "(No meeting content yet)"

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
            return f"Sorry, AI service is temporarily unavailable: {str(e)}"

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

            raise RuntimeError("AI returned empty content")
        except Exception as e:
            logger.error(f"Streaming LLM call failed: {e}")
            raise RuntimeError(f"AI service temporarily unavailable: {e}") from e

    def _summary_max_chars(self) -> int | None:
        """Compute max transcript chars for summary based on model context window.

        Uses ~3 chars per token as a rough estimate for mixed CJK/English text.
        Reserves ~20% of the context for the summary prompt + system message + output.
        Returns None (no limit) if the model context is very large (>= 64K tokens).
        """
        tokens = self.llm_config.effective_context_tokens
        if tokens >= 65536:
            return None  # No artificial limit for large-context models
        # Reserve 20% for prompt overhead + output, use 80% for transcript
        usable_tokens = int(tokens * 0.8)
        return usable_tokens * 3  # ~3 chars per token for CJK/English mix

    async def update_summary(self) -> Optional[str]:
        """
        Generate/update meeting summary.
        Uses incremental mode when an existing summary is available:
        merges existing summary with new transcript segments only.
        Falls back to full summary for first-time generation.
        Uses self.template if set, otherwise falls back to default prompts.
        Non-blocking.
        """
        if len(self.transcript) < 3:
            return None

        try:
            use_incremental = (
                self.summary is not None
                and self.summary.text.strip()
                and self.last_summarized_ordinal > 0
                and self.last_summarized_ordinal < len(self.transcript)
            )

            max_chars = self._summary_max_chars()

            if use_incremental:
                # Incremental: existing summary + new segments only
                new_text = self.get_new_transcript_text(max_chars=max_chars)
                if not new_text.strip():
                    return self.summary.text

                if self.template:
                    system = self.template["system_prompt"]
                    # Use incremental merge prompt with template's system prompt
                    user = (
                        "## Existing Summary\n{existing_summary}\n\n"
                        "## New Meeting Content\n{new_transcript}\n\n"
                        "Update the summary to incorporate the new content. "
                        "Output the updated summary in concise bullet point format."
                    )
                    if self.template.get("language"):
                        user += f"\n\nPlease output the summary in {self.template['language']}."
                    prompt = [
                        SystemMessage(content=system),
                        HumanMessage(content=user.format(
                            existing_summary=self.summary.text,
                            new_transcript=new_text,
                        )),
                    ]
                else:
                    prompt = INCREMENTAL_SUMMARY_PROMPT.format_messages(
                        existing_summary=self.summary.text,
                        new_transcript=new_text,
                    )
            else:
                # Full summary: first time or forced regeneration
                transcript_text = self.get_transcript_text(max_chars=max_chars)

                if self.template:
                    system = self.template["system_prompt"]
                    user = self.template["user_prompt"]
                    if "{transcript_text}" not in user:
                        user += "\n\n{transcript_text}"
                    if self.template.get("language"):
                        user += f"\n\nPlease output the summary in {self.template['language']}."
                    prompt = [
                        SystemMessage(content=system),
                        HumanMessage(content=user.format(transcript_text=transcript_text)),
                    ]
                else:
                    prompt = SUMMARY_PROMPT.format_messages(transcript_text=transcript_text)

            response = await self._create_chat_model(temperature=0.2).ainvoke(prompt)
            summary_text = self._extract_text(response.content)

            self.summary = MeetingSummary(
                text=summary_text.strip(),
                turn_count=len(self.transcript),
            )
            self.last_summarized_ordinal = len(self.transcript)

            if self.store is not None:
                template_id = self.template.get("id") if self.template else None
                self.store.upsert_summary(
                    self.session_id,
                    self.created_at,
                    self.summary.text,
                    self.summary.updated_at,
                    self.summary.turn_count,
                    template_id=template_id,
                    last_summarized_ordinal=self.last_summarized_ordinal,
                )
            return self.summary.text

        except Exception as e:
            logger.error(f"Summary generation failed: {e}")
            return None

    def _create_chat_model(self, temperature: float):
        """Create (or reuse cached) LangChain chat model for the configured provider."""
        if self.llm_config.local_only and not self.llm_config.is_local_provider():
            raise RuntimeError(
                f"local-only mode is enabled, so remote provider '{self.llm_config.provider}' "
                "is not available offline."
            )

        cached = self._chat_model_cache.get(temperature)
        if cached is not None:
            return cached

        client = ChatOpenAI(
            model=self.llm_config.model_name,
            api_key=self.llm_config.api_key or "unused",
            base_url=self.llm_config.base_url,
            temperature=temperature,
            timeout=300,
            max_retries=0,
            streaming=True,
        )
        self._chat_model_cache[temperature] = client
        return client

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

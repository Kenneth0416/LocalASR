"""
Meeting Realtime Voice - Main Server
WebSocket server for real-time transcription + AI chat.
"""

import asyncio
import json
import logging
import os
import struct
import sys
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import requests
import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from asr import (
    ASRService,
    FinalOnlyASRRouter,
    PreviewFinalASRRouter,
    RealtimeTranscriptEvent,
    TransformerChunkingConfig,
    WebRTCVADMeetingTranscriber,
)
from config import load_config
from persistence import MeetingStore
from session import MeetingSession, MeetingSummary, SessionManager
from runtime_checks import build_runtime_snapshot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
logger = logging.getLogger("meeting.server")

# Load configuration
llm_config, asr_config, preview_asr_config, realtime_vad_config, server_config, meeting_config = load_config()

# Initialize services
disable_preview_asr = os.getenv("DISABLE_PREVIEW_ASR", "").strip().lower() in {"1", "true", "yes"}

asr_service = ASRService(asr_config)
preview_asr_service = None if disable_preview_asr else ASRService(preview_asr_config)
meeting_store = MeetingStore(server_config.database_path)
session_manager = SessionManager(llm_config, meeting_config)
session_manager.store = meeting_store

runtime_snapshot = None
cached_llm_status = "unknown"


AUDIO_PACKET_MAGIC = b"MRV1"
FRAME_SAMPLES = max(1, int(round(asr_config.sample_rate * (realtime_vad_config.frame_ms / 1000.0))))
SUPPORTED_PYTHON_SERIES = {(3, 11), (3, 12)}


def build_transformer_chunking_config(config) -> TransformerChunkingConfig:
    """Build the semantic chunker config from runtime ASR settings."""
    return TransformerChunkingConfig(
        min_chunk_sec=config.min_chunk_sec,
        preferred_chunk_sec=config.preferred_chunk_sec,
        max_chunk_sec=config.max_chunk_sec,
        overlap_sec=config.overlap_sec,
        endpoint_silence_sec=config.endpoint_silence_sec,
        boundary_search_sec=config.boundary_search_sec,
        semantic_pause_sec=config.semantic_pause_sec,
        semantic_soft_pause_sec=config.semantic_soft_pause_sec,
        semantic_force_commit_sec=config.semantic_force_commit_sec,
    )


def build_realtime_transcriber(session_state: dict) -> WebRTCVADMeetingTranscriber:
    """Build the realtime VAD transcriber with preview/final ASR routing."""
    if preview_asr_service is not None:
        router = PreviewFinalASRRouter(
            preview_asr_service,
            asr_service,
            language_getter=lambda: session_state.get("language"),
            context_getter=lambda: session_state.get("asr_prompt"),
        )
    else:
        router = FinalOnlyASRRouter(
            asr_service,
            language_getter=lambda: session_state.get("language"),
            context_getter=lambda: session_state.get("asr_prompt"),
        )
    return WebRTCVADMeetingTranscriber(
        router=router,
        sample_rate=asr_config.sample_rate,
        config=realtime_vad_config,
    )


@dataclass(frozen=True)
class AudioFrame:
    seq: int
    pcm_chunk: bytes
    sample_count: int


@dataclass(frozen=True)
class AudioQueueItem:
    event_type: str
    frames: tuple[AudioFrame, ...] = ()
    last_seq: int = -1
    reason: str = "stop"


class AudioPacketDecoder:
    """Decode framed audio packets while keeping legacy raw PCM compatibility."""

    def __init__(self, frame_samples: int):
        self.frame_samples = frame_samples
        self.frame_bytes = frame_samples * 2
        self._legacy_buffer = bytearray()
        self._next_seq = 0
        self.last_seq = -1

    def decode(self, payload: bytes) -> tuple[AudioFrame, ...]:
        if not payload:
            return ()

        if payload.startswith(AUDIO_PACKET_MAGIC):
            frames = self._decode_packet(payload)
        else:
            frames = self._decode_legacy_pcm(payload)

        if frames:
            self.last_seq = frames[-1].seq
        return frames

    def flush_legacy_tail(self) -> tuple[AudioFrame, ...]:
        aligned_len = len(self._legacy_buffer) - (len(self._legacy_buffer) % 2)
        if aligned_len <= 0:
            self._legacy_buffer.clear()
            return ()

        pcm_chunk = bytes(self._legacy_buffer[:aligned_len])
        self._legacy_buffer.clear()
        frame = AudioFrame(
            seq=self._next_seq,
            pcm_chunk=pcm_chunk,
            sample_count=aligned_len // 2,
        )
        self._next_seq += 1
        self.last_seq = frame.seq
        return (frame,)

    def _decode_packet(self, payload: bytes) -> tuple[AudioFrame, ...]:
        if len(payload) < 6:
            raise ValueError("audio packet header too short")

        frame_count = struct.unpack_from("<H", payload, 4)[0]
        offset = 6
        frames: list[AudioFrame] = []

        for _ in range(frame_count):
            if offset + 6 > len(payload):
                raise ValueError("audio frame header truncated")

            seq, sample_count = struct.unpack_from("<IH", payload, offset)
            offset += 6
            byte_len = sample_count * 2
            if offset + byte_len > len(payload):
                raise ValueError("audio frame payload truncated")

            pcm_chunk = payload[offset:offset + byte_len]
            offset += byte_len
            frames.append(
                AudioFrame(
                    seq=seq,
                    pcm_chunk=pcm_chunk,
                    sample_count=sample_count,
                )
            )
            self._next_seq = max(self._next_seq, seq + 1)

        if offset != len(payload):
            raise ValueError("audio packet contains trailing bytes")

        return tuple(frames)

    def _decode_legacy_pcm(self, payload: bytes) -> tuple[AudioFrame, ...]:
        aligned_payload = payload[: len(payload) - (len(payload) % 2)]
        if not aligned_payload:
            return ()

        self._legacy_buffer.extend(aligned_payload)
        frames: list[AudioFrame] = []

        while len(self._legacy_buffer) >= self.frame_bytes:
            pcm_chunk = bytes(self._legacy_buffer[:self.frame_bytes])
            del self._legacy_buffer[:self.frame_bytes]
            frames.append(
                AudioFrame(
                    seq=self._next_seq,
                    pcm_chunk=pcm_chunk,
                    sample_count=self.frame_samples,
                )
            )
            self._next_seq += 1

        return tuple(frames)


class SessionAudioRecorder:
    """Persist the live meeting PCM stream into a per-session WAV file."""

    def __init__(self, session_id: str, sample_rate: int, output_dir: Path):
        self.session_id = session_id
        self.sample_rate = sample_rate
        self.output_dir = output_dir
        self.file_path: Path | None = None
        self.byte_count = 0
        self.error_message: str | None = None
        self._writer: wave.Wave_write | None = None

    def append_pcm(self, pcm_chunk: bytes) -> None:
        if not pcm_chunk or self.error_message:
            return

        try:
            if self._writer is None:
                self._open_writer()
            self._writer.writeframesraw(pcm_chunk)
            self.byte_count += len(pcm_chunk)
        except Exception as e:
            self.error_message = str(e)
            logger.error(
                "Failed to persist meeting audio for session %s: %s",
                self.session_id,
                e,
                exc_info=True,
            )
            self._cleanup_failed_file()

    def finalize(self) -> Path | None:
        writer = self._writer
        self._writer = None

        if writer is not None:
            try:
                writer.close()
            except Exception as e:
                self.error_message = str(e)
                logger.error(
                    "Failed to finalize meeting audio for session %s: %s",
                    self.session_id,
                    e,
                    exc_info=True,
                )
                self._cleanup_failed_file()
                return None

        if self.file_path and self.file_path.exists():
            return self.file_path
        return None

    def _open_writer(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.file_path = self.output_dir / f"{timestamp}-{self.session_id}.wav"
        writer = wave.open(str(self.file_path), "wb")
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(self.sample_rate)
        self._writer = writer

    def _cleanup_failed_file(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is not None:
            try:
                writer.close()
            except Exception:
                logger.debug("Failed to close broken recording writer", exc_info=True)

        if self.file_path and self.file_path.exists():
            try:
                self.file_path.unlink()
            except OSError:
                logger.warning("Failed to remove incomplete recording %s", self.file_path, exc_info=True)

        self.file_path = None
        self.byte_count = 0


# FastAPI app
app = FastAPI(title="Meeting Realtime Voice")

app.add_middleware(
    CORSMiddleware,
    allow_origins=server_config.cors_origins,
    allow_credentials=(
        server_config.cors_allow_credentials
        and server_config.cors_origins != ["*"]
    ),
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_runtime_warnings() -> list[str]:
    warnings: list[str] = []
    python_series = sys.version_info[:2]

    if python_series not in SUPPORTED_PYTHON_SERIES:
        warnings.append(
            "Python 3.11/3.12 is the supported runtime for release builds; newer versions are best-effort only."
        )

    if asr_config.device in {"auto", "mps"} and python_series >= (3, 13):
        warnings.append(
            "MPS + Python 3.13+ has shown unstable model loading; prefer Python 3.11/3.12 or switch ASR_DEVICE=cpu."
        )

    if server_config.cors_origins == ["*"]:
        warnings.append(
            "CORS currently allows all origins; keep this local-only or tighten CORS_ORIGINS before publishing."
        )

    if server_config.cors_origins == ["*"] and server_config.cors_allow_credentials:
        warnings.append(
            "CORS is configured with wildcard origins and credentials; tighten this before any networked deployment."
        )

    return warnings


def _service_status(service) -> dict[str, str | None]:
    state_getter = getattr(service, "initialization_state", None)
    error_getter = getattr(service, "initialization_error", None)

    state = state_getter() if callable(state_getter) else ("ready" if getattr(service, "_initialized", False) else "idle")
    error = error_getter() if callable(error_getter) else None
    return {
        "state": state,
        "error": error,
    }


def get_asr_status() -> dict[str, object]:
    final_status = _service_status(asr_service)
    if preview_asr_service is not None:
        preview_status = _service_status(preview_asr_service)
        aggregate_state = final_status["state"]
        if final_status["state"] == "ready" and preview_status["state"] != "ready":
            aggregate_state = "degraded"
        preview_enabled = preview_status["state"] == "ready"
    else:
        preview_status = {"state": "disabled", "error": None}
        aggregate_state = final_status["state"]
        preview_enabled = False

    return {
        "state": aggregate_state,
        "error": final_status["error"],
        "final": final_status,
        "preview": preview_status,
        "preview_enabled": preview_enabled,
    }


def get_llm_status(timeout_sec: float = 3.0) -> str:
    try:
        if llm_config.local_only and not llm_config.is_local_provider():
            return f"blocked: local-only mode forbids provider '{llm_config.provider}'"

        if llm_config.is_openai_compatible():
            resp = requests.get(
                f"{llm_config.base_url}/models",
                headers={"Authorization": f"Bearer {llm_config.openai_api_key}"},
                timeout=timeout_sec,
            )
            return "ok" if resp.ok else f"error: {resp.status_code}"

        resp = requests.get(f"{llm_config.ollama_base_url}/api/tags", timeout=timeout_sec)
        return "ok" if resp.ok else f"error: {resp.status_code}"
    except Exception as e:
        return f"unreachable: {e}"


def refresh_runtime_snapshot(llm_status: str | None = None):
    global runtime_snapshot, cached_llm_status
    asr_status = get_asr_status()
    if llm_status is None:
        llm_status = cached_llm_status
    else:
        cached_llm_status = llm_status

    runtime_snapshot = build_runtime_snapshot(
        llm_config,
        asr_config,
        server_config,
        asr_state=str(asr_status["final"]["state"]),
        asr_error=asr_status["final"]["error"],
        llm_status=llm_status,
    )
    return runtime_snapshot


def is_readiness_snapshot_healthy(snapshot) -> bool:
    capability_states = (
        snapshot.capabilities.get("transcription_realtime"),
        snapshot.capabilities.get("summary_local"),
        snapshot.capabilities.get("chat_local"),
    )
    return all(capability is not None and capability.state == "ready" for capability in capability_states)


def get_meeting_or_404(session_id: str) -> dict:
    meeting = meeting_store.get_meeting(session_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


def build_transcript_text(meeting: dict) -> str:
    lines: list[str] = []
    for segment in meeting.get("transcript", []):
        lines.append(
            f'{segment.get("speaker", "发言人")} '
            f'[{float(segment.get("start_time", 0.0)):.1f}s-{float(segment.get("end_time", 0.0)):.1f}s]: '
            f'{segment.get("text", "")}'
        )
    return "\n".join(lines)


def build_meeting_markdown(meeting: dict) -> str:
    parts = [
        f"# Meeting {meeting['session_id']}",
        "",
        f"- Created: {meeting.get('created_at', '')}",
        f"- Ended: {meeting.get('ended_at', '') or 'N/A'}",
        f"- Status: {meeting.get('status', 'unknown')}",
        f"- Transcript Segments: {meeting.get('transcript_count', 0)}",
        f"- Chat Messages: {meeting.get('chat_count', 0)}",
    ]

    if meeting.get("recording_path"):
        parts.extend(
            [
                f"- Recording: {meeting['recording_path']}",
                f"- Recording Bytes: {meeting.get('recording_bytes', 0)}",
            ]
        )

    summary = (meeting.get("summary") or "").strip()
    if summary:
        parts.extend(["", "## Summary", "", summary])

    transcript_text = build_transcript_text(meeting)
    if transcript_text:
        parts.extend(["", "## Transcript", "", transcript_text])

    chat_history = meeting.get("chat_history", [])
    if chat_history:
        parts.extend(["", "## Chat", ""])
        for message in chat_history:
            role = "User" if message.get("role") == "user" else "Assistant"
            parts.append(f"**{role}:** {message.get('content', '')}")

    return "\n".join(parts).strip() + "\n"


async def regenerate_meeting_summary(session_id: str) -> dict:
    meeting = get_meeting_or_404(session_id)
    temp_session = MeetingSession(f"{session_id}-summary", llm_config, meeting_config)
    for segment in meeting.get("transcript", []):
        temp_session.add_transcript_segment(
            speaker=str(segment.get("speaker", "发言人")),
            text=str(segment.get("text", "")),
            start_time=float(segment.get("start_time", 0.0)),
            end_time=float(segment.get("end_time", 0.0)),
        )

    summary = await temp_session.update_summary()
    if not summary:
        raise HTTPException(status_code=400, detail="Not enough transcript to generate summary")

    meeting_store.upsert_summary(
        session_id,
        meeting.get("created_at") or datetime.now().isoformat(),
        summary,
        temp_session.summary.updated_at if temp_session.summary else datetime.now().isoformat(),
        len(temp_session.transcript),
    )
    return get_meeting_or_404(session_id)


@app.post("/api/meetings/{session_id}/chat")
async def chat_on_meeting(session_id: str, payload: dict = Body(...)):
    """
    Send a chat message and get AI response for a meeting.
    Works without WebSocket for upload mode sessions.
    """
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    meeting = get_meeting_or_404(session_id)
    session = session_manager.get_session(session_id)
    if session is None:
        # Recreate session from persisted data
        session = session_manager.create_session()
        session = session_manager.register_recovered_session(session_id, session)
        session.transcript = [
            type("Segment", (), {
                "id": seg.get("id", ""),
                "speaker": seg.get("speaker", "发言人"),
                "text": seg.get("text", ""),
                "start_time": float(seg.get("start_time", seg.get("start", 0.0))),
                "end_time": float(seg.get("end_time", seg.get("end", 0.0))),
            })()
            for seg in meeting.get("transcript", [])
        ]
        session.chat_history = [
            type("Msg", (), {
                "id": msg.get("id", ""),
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
            })()
            for msg in meeting.get("chat_history", [])
        ]
        summary_text = (meeting.get("summary") or "").strip()
        if summary_text:
            session.summary = MeetingSummary(
                text=summary_text,
                updated_at=str(meeting.get("summary_updated_at") or datetime.now().isoformat()),
                turn_count=int(meeting.get("summary_turn_count") or 0),
            )
        session.state = {"language": meeting.get("language", None)}

    try:
        answer = await session.ask_llm(question)
        return {"answer": answer}
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")
@app.on_event("startup")
async def on_startup():
    for warning in get_runtime_warnings():
        logger.warning(warning)
    services_to_init = [asr_service]
    if not disable_preview_asr and preview_asr_service is not None:
        services_to_init.append(preview_asr_service)
    for service in services_to_init:
        initialize = getattr(service, "initialize", None)
        if callable(initialize):
            await initialize()
    llm_status = await asyncio.to_thread(get_llm_status)
    snapshot = refresh_runtime_snapshot(llm_status=llm_status)
    for warning in snapshot.warnings:
        logger.warning(warning)


@app.on_event("shutdown")
async def on_shutdown():
    services_to_shutdown = []
    if not disable_preview_asr and preview_asr_service is not None:
        services_to_shutdown.append(preview_asr_service)
    services_to_shutdown.append(asr_service)
    for service in services_to_shutdown:
        shutdown = getattr(service, "shutdown", None)
        if callable(shutdown):
            await shutdown()


# ============================================================================
# WebSocket: /ws/meeting - Main meeting session with transcription
# ============================================================================

@app.websocket("/ws/meeting")
async def websocket_meeting(websocket: WebSocket):
    """Main meeting WebSocket - handles both transcription and chat"""
    await websocket.accept()
    logger.info("New meeting connection")

    # Create new session
    session = session_manager.create_session()

    # Audio queue state
    audio_queue: asyncio.Queue[AudioQueueItem] = asyncio.Queue(maxsize=server_config.audio_queue_maxsize)
    audio_decoder = AudioPacketDecoder(frame_samples=FRAME_SAMPLES)
    session_state: dict = {"language": None, "asr_prompt": None}
    audio_recorder = SessionAudioRecorder(
        session_id=session.session_id,
        sample_rate=asr_config.sample_rate,
        output_dir=RECORDINGS_DIR,
    )
    audio_worker_task = asyncio.create_task(
        audio_worker(session, websocket, audio_queue, session_state, audio_recorder)
    )
    recording_started = False

    try:
        asr_status = get_asr_status()
        final_asr_status = asr_status["final"]
        preview_asr_status = asr_status["preview"]
        # Send ready message
        await websocket.send_json({
            "type": "ready",
            "session_id": session.session_id,
            "asr_device": asr_config.device,
            "asr_ready": final_asr_status["state"] == "ready",
            "asr_init_timeout_sec": asr_config.init_timeout_sec,
            "asr_model": str(asr_config.model_path),
            "final_asr_model": str(asr_config.model_path),
            "preview_asr_model": str(preview_asr_config.model_path),
            "asr_language": asr_config.language,
            "asr_state": asr_status["state"],
            "final_asr_state": final_asr_status["state"],
            "preview_asr_state": preview_asr_status["state"],
            "preview_enabled": asr_status["preview_enabled"],
            "llm_provider": llm_config.provider,
            "llm_model": llm_config.model_name,
            "runtime_warnings": get_runtime_warnings(),
            "runtime": refresh_runtime_snapshot(llm_status=cached_llm_status).to_dict(),
        })

        while True:
            # Receive message (JSON or binary audio)
            data = await websocket.receive()
            if data.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect

            # Handle JSON messages
            if "text" in data:
                try:
                    msg = json.loads(data["text"])
                    msg_type = msg.get("type", "")

                    if msg_type == "start":
                        session_state["language"] = msg.get("language") or None
                        session_state["asr_prompt"] = str(msg.get("asr_prompt", "") or "").strip() or None
                        await websocket.send_json({
                            "type": "status",
                            "message": "正在准备转录模型..."
                        })

                        try:
                            await asr_service.wait_ready(timeout=asr_config.init_timeout_sec)
                        except Exception as e:
                            recording_started = False
                            error_message = str(e)
                            if "timed out" in error_message:
                                user_message = (
                                    f"转录模型初始化超时（>{int(asr_config.init_timeout_sec)} 秒），"
                                    "请检查本地 ASR 模型和 server.log"
                                )
                            else:
                                user_message = f"转录模型初始化失败: {error_message}"
                            await websocket.send_json({
                                "type": "error",
                                "message": user_message
                            })
                            continue

                        recording_started = True
                        await websocket.send_json({
                            "type": "start_ack",
                            "message": "会议开始，正在转录..."
                        })

                    elif msg_type in {"stop", "eos"}:
                        if recording_started:
                            recording_started = False
                            tail_frames = audio_decoder.flush_legacy_tail()
                            if tail_frames:
                                await audio_queue.put(
                                    AudioQueueItem(event_type="audio", frames=tail_frames)
                                )

                            await audio_queue.put(
                                AudioQueueItem(
                                    event_type="eos",
                                    last_seq=msg.get("last_seq", audio_decoder.last_seq),
                                    reason="stop",
                                )
                            )
                            await audio_worker_task
                        return

                    elif msg_type == "chat":
                        # User asked a question
                        question = msg.get("question", "").strip()
                        if question:
                            # Non-blocking LLM call
                            asyncio.create_task(
                                handle_chat(session, question, websocket)
                            )

                    elif msg_type == "summary":
                        # Request summary update
                        asyncio.create_task(
                            handle_summary_request(session, websocket)
                        )

                    elif msg_type == "ping":
                        await websocket.send_json({"type": "pong"})

                except json.JSONDecodeError:
                    logger.warning("Invalid JSON received")

            # Handle binary audio data
            elif "bytes" in data:
                if not recording_started:
                    continue

                audio_chunk = data["bytes"]
                if not audio_chunk:
                    continue

                try:
                    frames = audio_decoder.decode(audio_chunk)
                except ValueError as e:
                    logger.warning("Invalid audio packet: %s", e)
                    await websocket.send_json({
                        "type": "error",
                        "message": f"音频包格式错误: {e}",
                    })
                    continue

                if frames:
                    await audio_queue.put(
                        AudioQueueItem(event_type="audio", frames=frames)
                    )

    except WebSocketDisconnect:
        logger.info(f"Meeting session {session.session_id} disconnected")
        if recording_started:
            recording_started = False
            tail_frames = audio_decoder.flush_legacy_tail()
            if tail_frames:
                await audio_queue.put(
                    AudioQueueItem(event_type="audio", frames=tail_frames)
                )
            await audio_queue.put(
                AudioQueueItem(
                    event_type="eos",
                    last_seq=audio_decoder.last_seq,
                    reason="disconnect",
                )
            )
            try:
                await asyncio.wait_for(
                    audio_worker_task,
                    timeout=5,
                )
            except Exception:
                logger.debug("Disconnect flush did not finish before timeout", exc_info=True)

    except Exception as e:
        logger.error(f"WebSocket error: {e}")

    finally:
        if not audio_worker_task.done():
            audio_worker_task.cancel()
            try:
                await audio_worker_task
            except asyncio.CancelledError:
                pass

        # Cleanup
        session_manager.delete_session(session.session_id)
        logger.info(f"Meeting session {session.session_id} closed")


async def audio_worker(
    session: MeetingSession,
    websocket: WebSocket,
    audio_queue: asyncio.Queue[AudioQueueItem],
    session_state: dict,
    audio_recorder: SessionAudioRecorder,
):
    """Continuously process queued audio with final transcript updates."""
    transcriber = build_realtime_transcriber(session_state)

    try:
        while True:
            item = await audio_queue.get()
            try:
                if item.event_type == "audio":
                    pcm_chunk = b"".join(frame.pcm_chunk for frame in item.frames)
                    audio_recorder.append_pcm(pcm_chunk)
                    events = await transcriber.push_pcm(pcm_chunk)
                    for event in events:
                        if event.is_final:
                            await emit_final_transcript_segment(session, event, websocket)
                        else:
                            await emit_preview_transcript_segment(session, event, websocket)
                        await send_transcribe_done(websocket, event)
                    continue

                if item.event_type == "eos":
                    events = await transcriber.flush(reason=item.reason)
                    for event in events:
                        if event.is_final:
                            await emit_final_transcript_segment(session, event, websocket)
                        else:
                            await emit_preview_transcript_segment(session, event, websocket)
                        await send_transcribe_done(websocket, event)

                    recording_path = audio_recorder.finalize()
                    ended_at = datetime.now().isoformat()
                    meeting_store.complete_session(
                        session.session_id,
                        session.created_at,
                        ended_at,
                        recording_path=str(recording_path) if recording_path else None,
                        recording_bytes=audio_recorder.byte_count,
                        recording_error=audio_recorder.error_message,
                        status="completed" if item.reason == "stop" else "disconnected",
                    )
                    if item.reason != "disconnect":
                        await websocket.send_json({
                            "type": "stopped",
                            "message": "会议结束",
                            "last_seq": item.last_seq,
                            "reason": item.reason,
                            "recording_path": str(recording_path) if recording_path else None,
                            "recording_bytes": audio_recorder.byte_count,
                            "recording_error": audio_recorder.error_message,
                        })
                    break
            finally:
                audio_queue.task_done()
    finally:
        audio_recorder.finalize()


async def send_transcribe_done(websocket: WebSocket, event: RealtimeTranscriptEvent):
    await websocket.send_json({
        "type": "transcribe_done",
        "processing_time": event.processing_time,
        "phase": event.event_type,
        "segment_id": event.segment_id,
        "revision": event.revision,
        "is_final": event.is_final,
        "cut_reason": event.cut_reason,
    })


async def emit_preview_transcript_segment(
    session: MeetingSession,
    event: RealtimeTranscriptEvent,
    websocket: WebSocket,
):
    """Broadcast a preview transcript update without persistence."""
    try:
        text = event.text.strip()
        if not text:
            return

        segment, accepted = session.upsert_live_preview_segment(
            event.segment_id,
            event.revision,
            speaker="发言人",
            text=text,
            start_time=event.start_time,
            end_time=event.end_time,
        )
        if not accepted:
            return

        await websocket.send_json({
            "type": "transcript",
            "segment": {
                "id": "",
                "segment_id": event.segment_id,
                "revision": event.revision,
                "is_final": event.is_final,
                "cut_reason": event.cut_reason,
                "speaker": segment.speaker,
                "text": segment.text,
                "start": segment.start_time,
                "end": segment.end_time,
            },
            "total_segments": len(session.transcript),
            "processing_time": event.processing_time,
        })
    except Exception as e:
        logger.error("Audio preview processing error: %s", e)
        await websocket.send_json({
            "type": "error",
            "message": f"转写失败: {str(e)}"
        })


async def emit_final_transcript_segment(
    session: MeetingSession,
    event: RealtimeTranscriptEvent,
    websocket: WebSocket,
):
    """Persist and broadcast a finalized transcript emission."""
    try:
        session.clear_live_preview_segment(event.segment_id)
        text = event.text.strip()
        if not text:
            return

        segment = session.add_transcript_segment(
            speaker="发言人",
            text=text,
            start_time=event.start_time,
            end_time=event.end_time,
        )

        await websocket.send_json({
            "type": "transcript",
            "segment": {
                "id": segment.id,
                "segment_id": event.segment_id,
                "revision": event.revision,
                "is_final": event.is_final,
                "cut_reason": event.cut_reason,
                "speaker": segment.speaker,
                "text": segment.text,
                "start": segment.start_time,
                "end": segment.end_time,
            },
            "total_segments": len(session.transcript),
            "processing_time": event.processing_time,
        })

        if len(session.transcript) % meeting_config.summary_interval_turns == 0:
            asyncio.create_task(handle_summary_request(session, websocket))

    except Exception as e:
        logger.error(f"Audio processing error: {e}")
        await websocket.send_json({
            "type": "error",
            "message": f"转写失败: {str(e)}"
        })


async def handle_chat(session: MeetingSession, question: str, websocket: WebSocket):
    """Handle user chat question - non-blocking"""
    assistant_message_id = str(uuid4())[:8]
    answer_parts: list[str] = []

    try:
        await websocket.send_json({
            "type": "chat_status",
            "status": "thinking",
            "message": "AI 思考中..."
        })

        await websocket.send_json({
            "type": "chat_stream_start",
            "message_id": assistant_message_id,
            "question": question,
        })

        async for delta in session.stream_llm_answer(question):
            answer_parts.append(delta)
            await websocket.send_json({
                "type": "chat_stream_delta",
                "message_id": assistant_message_id,
                "delta": delta,
            })

        answer = "".join(answer_parts).strip()
        if not answer:
            raise RuntimeError("AI 返回了空内容")

        session.add_chat_message("user", question)
        session.add_chat_message("assistant", answer, message_id=assistant_message_id)

        # Send response
        await websocket.send_json({
            "type": "chat_response",
            "message_id": assistant_message_id,
            "question": question,
            "answer": answer,
        })

    except Exception as e:
        logger.error(f"Chat error: {e}")
        await websocket.send_json({
            "type": "chat_error",
            "message_id": assistant_message_id,
            "message": f"AI 回答失败: {str(e)}"
        })


async def handle_summary_request(session: MeetingSession, websocket: WebSocket):
    """Update meeting summary - non-blocking"""
    try:
        await websocket.send_json({
            "type": "summary_status",
            "status": "updating",
            "message": "正在更新摘要..."
        })

        summary = await session.update_summary()

        if summary:
            await websocket.send_json({
                "type": "summary_update",
                "summary": summary,
                "transcript_count": len(session.transcript),
            })

    except Exception as e:
        logger.error(f"Summary error: {e}")


# ============================================================================
# HTTP Endpoints
# ============================================================================

@app.get("/api/health")
async def health_check():
    """Cheap liveness endpoint for process-level health."""
    asr_status = get_asr_status()
    final_asr_status = asr_status["final"]
    preview_asr_status = asr_status["preview"]
    snapshot = refresh_runtime_snapshot(llm_status=cached_llm_status).to_dict()
    return {
        "status": "ok",
        "app": "meeting-realtime-voice",
        "python_version": sys.version.split()[0],
        "runtime_warnings": get_runtime_warnings(),
        "runtime": snapshot,
        "asr_ready": final_asr_status["state"] == "ready",
        "asr_state": asr_status["state"],
        "asr_error": asr_status["error"],
        "asr_model": str(asr_config.model_path),
        "final_asr_model": str(asr_config.model_path),
        "preview_asr_model": str(preview_asr_config.model_path),
        "final_asr_state": final_asr_status["state"],
        "preview_asr_state": preview_asr_status["state"],
        "preview_enabled": asr_status["preview_enabled"],
        "asr_device": asr_config.device,
        "llm_provider": llm_config.provider,
        "llm_model": llm_config.model_name,
        "active_sessions": len(session_manager.sessions),
        "audio_queue_maxsize": server_config.audio_queue_maxsize,
    }


@app.get("/api/readiness")
async def readiness_check():
    """Dependency-oriented readiness endpoint."""
    asr_status = get_asr_status()
    llm_status = await asyncio.to_thread(get_llm_status)
    snapshot = refresh_runtime_snapshot(llm_status=llm_status)
    status = "ok" if is_readiness_snapshot_healthy(snapshot) and asr_status["preview_enabled"] else "degraded"

    return {
        "status": status,
        "runtime": snapshot.to_dict(),
        "asr": asr_status,
        "llm_status": llm_status,
        "active_sessions": len(session_manager.sessions),
    }


@app.get("/api/meetings")
async def list_meeting_history():
    """List persisted meeting history."""
    return {"meetings": meeting_store.list_meetings(limit=server_config.history_limit)}


@app.get("/api/meetings/{session_id}")
async def get_meeting_detail(session_id: str):
    """Fetch one persisted meeting with transcript, chat, and summary."""
    return get_meeting_or_404(session_id)


@app.get("/api/meetings/{session_id}/export.txt")
async def export_meeting_text(session_id: str):
    """Export transcript as plain text."""
    meeting = get_meeting_or_404(session_id)
    filename = f"meeting-{session_id}.txt"
    return PlainTextResponse(
        build_transcript_text(meeting) or "",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/meetings/{session_id}/export.md")
async def export_meeting_markdown(session_id: str):
    """Export meeting context as markdown."""
    meeting = get_meeting_or_404(session_id)
    filename = f"meeting-{session_id}.md"
    return PlainTextResponse(
        build_meeting_markdown(meeting),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/meetings/{session_id}/export.json")
async def export_meeting_json(session_id: str):
    """Export meeting detail as JSON."""
    meeting = get_meeting_or_404(session_id)
    filename = f"meeting-{session_id}.json"
    return JSONResponse(
        meeting,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/meetings/{session_id}/recording")
async def download_meeting_recording(session_id: str):
    """Download a persisted meeting recording if present."""
    meeting = get_meeting_or_404(session_id)
    recording_path = meeting.get("recording_path")
    if not recording_path:
        raise HTTPException(status_code=404, detail="Recording not available")

    path = Path(str(recording_path))
    if not path.exists():
        raise HTTPException(status_code=404, detail="Recording file not found")

    return FileResponse(str(path), media_type="audio/wav", filename=path.name)


@app.patch("/api/meetings/{session_id}/transcript/{segment_id}")
async def update_meeting_transcript_segment(
    session_id: str,
    segment_id: str,
    payload: dict = Body(...),
):
    """Update one persisted transcript segment."""
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    updated = meeting_store.update_transcript_segment(
        session_id,
        segment_id,
        text=text,
        speaker=payload.get("speaker"),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Transcript segment not found")

    return {
        "segment": updated,
        "meeting": get_meeting_or_404(session_id),
    }


@app.post("/api/meetings/{session_id}/summary")
async def regenerate_summary(session_id: str):
    """Rebuild a persisted meeting summary from the stored transcript."""
    return await regenerate_meeting_summary(session_id)


@app.delete("/api/meetings/{session_id}")
async def delete_meeting(session_id: str):
    """Delete one persisted meeting and its recording file."""
    deleted = meeting_store.delete_meeting(session_id, delete_recording=True)
    if not deleted:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"status": "deleted", "session_id": session_id}


@app.post("/api/upload")
async def upload_audio_transcribe(
    file: UploadFile = File(...),
    language: str = Form(default=""),
    asr_prompt: str = Form(default=""),
):
    """
    Upload an audio file for transcription.
    Supports WAV, MP3, M4A, OGG, FLAC formats.
    Creates a new meeting session with the transcription results.
    """
    import io

    # Validate file type
    content_type = file.content_type or ""
    supported_types = [
        "audio/wav", "audio/x-wav",
        "audio/mp3", "audio/mpeg",
        "audio/m4a", "audio/x-m4a",
        "audio/ogg", "audio/flac",
        "audio/x-flac",
    ]
    if content_type not in supported_types and not any(
        file.filename.lower().endswith(ext) for ext in [".wav", ".mp3", ".m4a", ".ogg", ".flac"]
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {content_type}. Supported: WAV, MP3, M4A, OGG, FLAC"
        )

    # Read file content
    file_content = await file.read()

    # Check file size (limit to 500MB)
    max_size = 500 * 1024 * 1024
    if len(file_content) > max_size:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 500MB.")

    if len(file_content) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")

    logger.info(f"Transcribing uploaded file: {file.filename} ({len(file_content)} bytes)")

    try:
        # Transcribe the audio file
        lang = language if language else None
        prompt = str(asr_prompt or "").strip() or None
        result = await asr_service.transcribe_wav(
            file_content,
            language=lang,
            context=prompt,
        )

        # Create a new session and link to store
        session = session_manager.create_session()
        session.state = {"language": lang}
        session.store = meeting_store

        # Build transcript segments from result
        segments = []
        if result.segments:
            for seg in result.segments:
                segment = session.add_transcript_segment(
                    speaker="Speaker 1",
                    text=seg.get("text", "").strip(),
                    start_time=float(seg.get("start", 0.0)),
                    end_time=float(seg.get("end", 0.0)),
                )
                segments.append({
                    "id": segment.id,
                    "speaker": segment.speaker,
                    "text": segment.text,
                    "start_time": segment.start_time,
                    "end_time": segment.end_time,
                    "timestamp": segment.timestamp,
                })
        elif result.text:
            # No segment timestamps, create a single segment
            duration = result.audio_duration or 0.0
            segment = session.add_transcript_segment(
                speaker="Speaker 1",
                text=result.text,
                start_time=0.0,
                end_time=duration,
            )
            segments.append({
                "id": segment.id,
                "speaker": segment.speaker,
                "text": segment.text,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "timestamp": segment.timestamp,
            })

        # Complete the session
        ended_at = datetime.now().isoformat()
        meeting_store.complete_session(
            session.session_id,
            session.created_at,
            ended_at,
            recording_path=None,
            recording_bytes=0,
            recording_error=None,
            status="completed",
        )

        if len(segments) < 3:
            summary_state = "unavailable"
            summary_message = "转录段数少于 3 段，暂不生成摘要"
        else:
            summary_state = "queued"
            summary_message = "摘要已排队生成"
            asyncio.create_task(regenerate_meeting_summary(session.session_id))

        logger.info(f"Uploaded file transcription complete: {len(segments)} segments, session={session.session_id}")

        return {
            "session_id": session.session_id,
            "filename": file.filename,
            "segments": segments,
            "full_text": result.text,
            "audio_duration": result.audio_duration,
            "processing_time": result.processing_time,
            "summary_state": summary_state,
            "summary_message": summary_message,
        }

    except Exception as e:
        logger.error(f"Upload transcription failed: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")


@app.get("/api/sessions")
async def list_sessions():
    """List active meeting sessions when explicitly enabled."""
    if not server_config.expose_session_api:
        raise HTTPException(status_code=404, detail="Not found")
    return {"sessions": session_manager.list_sessions()}


# ============================================================================
# Static Files (Frontend)
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RECORDINGS_DIR = Path(
    os.path.expanduser(server_config.recordings_dir or str(BASE_DIR / "recordings"))
)

if not RECORDINGS_DIR.is_absolute():
    RECORDINGS_DIR = BASE_DIR / RECORDINGS_DIR

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    """Serve frontend"""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(index_path))
    return {"message": "Meeting Realtime Voice API", "docs": "/docs"}


# ============================================================================
# Main
# ============================================================================

def main():
    logger.info(f"Starting Meeting Realtime Voice Server...")
    logger.info(f"ASR Model: {asr_config.model_path}")
    logger.info(f"ASR Device: {asr_config.device}")
    logger.info(f"LLM Provider: {llm_config.provider}")
    logger.info(f"LLM Model: {llm_config.model_name}")
    logger.info(f"Server: {server_config.host}:{server_config.port}")

    uvicorn.run(
        app,
        host=server_config.host,
        port=server_config.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

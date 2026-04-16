"""
WebSocket endpoint and audio processing workers.

This module registers the /ws/meeting route on the shared FastAPI app.
Imported by server.py at module level so decorators run at import time.
"""

import asyncio
import json
import logging
import sys
import wave
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from server_app import app
from asr import (
    RealtimeTranscriptEvent,
    WebRTCVADMeetingTranscriber,
)
from audio_protocol import AudioPacketDecoder
from server_types import AudioQueueItem

logger = logging.getLogger("meeting.ws")

# Global refs injected by server.py before app creation
_app_refs: dict = {}


def _inject(
    asr_service,
    asr_config,
    llm_config,
    meeting_config,
    server_config,
    realtime_vad_config,
    session_manager,
    meeting_store,
    build_realtime_transcriber,
    RECORDINGS_DIR,
    get_asr_status,
    get_llm_status,
    get_runtime_warnings,
):
    _app_refs.update(
        asr_service=asr_service,
        asr_config=asr_config,
        llm_config=llm_config,
        meeting_config=meeting_config,
        server_config=server_config,
        realtime_vad_config=realtime_vad_config,
        session_manager=session_manager,
        meeting_store=meeting_store,
        build_realtime_transcriber=build_realtime_transcriber,
        RECORDINGS_DIR=RECORDINGS_DIR,
        get_asr_status=get_asr_status,
        get_llm_status=get_llm_status,
        get_runtime_warnings=get_runtime_warnings,
    )


# ============================================================================
# Audio recording
# ============================================================================


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


# ============================================================================
# WebSocket endpoint
# ============================================================================


@app.websocket("/ws/meeting")
async def websocket_meeting(websocket: WebSocket):
    """Main meeting WebSocket - handles both transcription and chat"""
    _server = sys.modules["server"]
    session_manager = _app_refs["session_manager"]
    asr_config = _app_refs["asr_config"]
    llm_config = _app_refs["llm_config"]
    meeting_config = _app_refs["meeting_config"]
    server_config = _app_refs["server_config"]
    realtime_vad_config = _app_refs["realtime_vad_config"]
    asr_service = _server.asr_service
    meeting_store = _app_refs["meeting_store"]
    build_realtime_transcriber = _server.build_realtime_transcriber
    RECORDINGS_DIR = _app_refs["RECORDINGS_DIR"]
    get_asr_status = _app_refs["get_asr_status"]
    get_runtime_warnings = _app_refs["get_runtime_warnings"]

    await websocket.accept()
    logger.info("New meeting connection")

    session = session_manager.create_session()

    audio_queue: asyncio.Queue[AudioQueueItem] = asyncio.Queue(maxsize=server_config.audio_queue_maxsize)
    frame_samples = max(1, int(round(asr_config.sample_rate * (realtime_vad_config.frame_ms / 1000.0))))
    audio_decoder = AudioPacketDecoder(frame_samples=frame_samples)
    session_state: dict = {"language": None, "asr_prompt": None}
    audio_recorder = SessionAudioRecorder(
        session_id=session.session_id,
        sample_rate=asr_config.sample_rate,
        output_dir=sys.modules["server"].RECORDINGS_DIR,
    )
    audio_worker_task = asyncio.create_task(
        audio_worker(
            session,
            websocket,
            audio_queue,
            session_state,
            audio_recorder,
        )
    )
    recording_started = False

    try:
        asr_status = get_asr_status()
        await websocket.send_json({
            "type": "ready",
            "session_id": session.session_id,
            "asr_device": asr_config.device,
            "asr_ready": asr_status["state"] == "ready",
            "asr_init_timeout_sec": asr_config.init_timeout_sec,
            "asr_model": str(asr_config.model_path),
            "asr_language": asr_config.language,
            "asr_state": asr_status["state"],
            "llm_provider": llm_config.provider,
            "llm_model": llm_config.model_name,
            "runtime_warnings": get_runtime_warnings(),
            "runtime": sys.modules["server"].refresh_runtime_snapshot(
                llm_status=sys.modules["server"].cached_llm_status
            ).to_dict(),
        })

        while True:
            data = await websocket.receive()
            if data.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect

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
                        question = msg.get("question", "").strip()
                        if question:
                            asyncio.create_task(
                                handle_chat(session, question, websocket)
                            )

                    elif msg_type == "summary":
                        asyncio.create_task(
                            handle_summary_request(session, websocket)
                        )

                    elif msg_type == "ping":
                        await websocket.send_json({"type": "pong"})

                except json.JSONDecodeError:
                    logger.warning("Invalid JSON received")

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

        session_manager.delete_session(session.session_id)
        logger.info(f"Meeting session {session.session_id} closed")


# ============================================================================
# Audio worker
# ============================================================================


async def audio_worker(
    session,
    websocket: WebSocket,
    audio_queue: asyncio.Queue[AudioQueueItem],
    session_state: dict,
    audio_recorder: SessionAudioRecorder,
):
    """Continuously process queued audio with final transcript updates."""
    _server = sys.modules["server"]
    build_realtime_transcriber = _server.build_realtime_transcriber
    meeting_config = _app_refs["meeting_config"]
    meeting_store = _server.meeting_store

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
                        await emit_final_transcript_segment(session, event, websocket)
                        await send_transcribe_done(websocket, event)
                    continue

                if item.event_type == "eos":
                    events = await transcriber.flush(reason=item.reason)
                    for event in events:
                        await emit_final_transcript_segment(session, event, websocket)
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
        "phase": "final",
        "segment_id": event.segment_id,
        "revision": event.revision,
        "is_final": True,
        "cut_reason": event.cut_reason,
    })

async def emit_final_transcript_segment(session, event: RealtimeTranscriptEvent, websocket: WebSocket):
    """Persist and broadcast a finalized transcript emission."""
    meeting_config = sys.modules["server"].meeting_config

    try:
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
        logger.error(f"Audio processing error: {e}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"转写失败: {str(e)}"
            })
        except Exception:
            pass


async def handle_chat(session, question: str, websocket: WebSocket):
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


async def handle_summary_request(session, websocket: WebSocket):
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

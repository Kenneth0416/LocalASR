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

# Track active audio_worker tasks for graceful shutdown
_active_workers: set[asyncio.Task] = set()

# Track fire-and-forget tasks (chat, summary) for cleanup on shutdown
_spawned_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Create and track a fire-and-forget task."""
    task = asyncio.create_task(coro)
    _spawned_tasks.add(task)
    task.add_done_callback(_spawned_tasks.discard)
    return task


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


async def shutdown_workers(timeout: float = 10.0) -> None:
    """Cancel all active audio workers and spawned tasks on shutdown."""
    if not _active_workers and not _spawned_tasks:
        return
    logger.info(
        "Shutting down %d active audio worker(s) and %d spawned task(s)",
        len(_active_workers), len(_spawned_tasks),
    )
    for task in _active_workers:
        task.cancel()
    for task in _spawned_tasks:
        task.cancel()
    all_tasks = _active_workers | _spawned_tasks
    _, pending = await asyncio.wait(all_tasks, timeout=timeout)
    if pending:
        logger.warning("%d task(s) did not finish within %.0fs", len(pending), timeout)
    _active_workers.clear()
    _spawned_tasks.clear()


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
    session_manager = _app_refs["session_manager"]
    asr_config = _app_refs["asr_config"]
    llm_config = _app_refs["llm_config"]
    meeting_config = _app_refs["meeting_config"]
    server_config = _app_refs["server_config"]
    realtime_vad_config = _app_refs["realtime_vad_config"]
    asr_service = _app_refs["asr_service"]
    meeting_store = _app_refs["meeting_store"]
    build_realtime_transcriber = _app_refs["build_realtime_transcriber"]
    RECORDINGS_DIR = _app_refs["RECORDINGS_DIR"]
    get_asr_status = _app_refs["get_asr_status"]
    get_runtime_warnings = _app_refs["get_runtime_warnings"]

    # Per-connection spawned tasks (chat, summary) for cleanup on disconnect
    _conn_tasks: set[asyncio.Task] = set()

    def _spawn_conn(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        _conn_tasks.add(task)
        _spawned_tasks.add(task)
        task.add_done_callback(_conn_tasks.discard)
        task.add_done_callback(_spawned_tasks.discard)
        return task

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
        output_dir=RECORDINGS_DIR,
    )
    audio_worker_task = asyncio.create_task(
        audio_worker(
            session,
            websocket,
            audio_queue,
            session_state,
            audio_recorder,
            spawn_fn=_spawn_conn,
        )
    )
    _active_workers.add(audio_worker_task)
    audio_worker_task.add_done_callback(_active_workers.discard)
    recording_started = False

    try:
        asr_status = get_asr_status()
        await websocket.send_json({
            "type": "ready",
            "session_id": session.session_id,
            "asr_backend": asr_config.backend,
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
                            "message": "Preparing transcription model..."
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
                            "message": "Meeting started, transcribing..."
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
                            _spawn_conn(handle_chat(session, question, websocket))

                    elif msg_type == "summary":
                        template_id = msg.get("template_id") or None
                        _spawn_conn(handle_summary_request(session, websocket, template_id))

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
                        "message": f"Audio packet format error: {e}",
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
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Session %s: audio worker did not finish within 30s after disconnect; cancelling",
                    session.session_id,
                )
            except Exception:
                logger.debug("Disconnect flush did not finish before timeout", exc_info=True)

    except Exception as e:
        logger.error(f"WebSocket error: {e}")

    finally:
        # Cancel any spawned tasks (chat, summary) for this connection
        for task in _conn_tasks:
            task.cancel()
        if _conn_tasks:
            await asyncio.gather(*_conn_tasks, return_exceptions=True)

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
    *,
    spawn_fn=None,
):
    """Continuously process queued audio with final transcript updates."""
    if spawn_fn is None:
        spawn_fn = _spawn
    build_realtime_transcriber = _app_refs["build_realtime_transcriber"]
    meeting_config = _app_refs["meeting_config"]
    meeting_store = _app_refs["meeting_store"]

    transcriber, preprocessor = build_realtime_transcriber(session_state)

    def _notify_drain():
        try:
            audio_queue.put_nowait(AudioQueueItem(event_type="drain"))
        except asyncio.QueueFull:
            # Queue is full with audio frames; the next audio or EOS item will
            # call _drain_finished_tasks anyway, so the completed result won't
            # be lost — it will be emitted on that next iteration.
            pass

    transcriber._on_final_done = _notify_drain
    session._transcriber = transcriber
    session._audio_queue = audio_queue

    _recording_finalized = False
    try:
        while True:
            item = await audio_queue.get()
            try:
                if item.event_type == "drain":
                    events = await transcriber._drain_finished_tasks()
                    for event in events:
                        await emit_final_transcript_segment(session, event, websocket, spawn_fn=spawn_fn)
                        await send_transcribe_done(websocket, event)
                    continue

                if item.event_type == "audio":
                    pcm_chunk = b"".join(frame.pcm_chunk for frame in item.frames)
                    audio_recorder.append_pcm(pcm_chunk)
                    pcm_chunk = preprocessor.process(pcm_chunk)
                    events = await transcriber.push_pcm(pcm_chunk)
                    for event in events:
                        await emit_final_transcript_segment(session, event, websocket, spawn_fn=spawn_fn)
                        await send_transcribe_done(websocket, event)
                    continue

                if item.event_type == "eos":
                    events = await transcriber.flush(reason=item.reason)
                    for event in events:
                        await emit_final_transcript_segment(session, event, websocket, spawn_fn=spawn_fn)
                        await send_transcribe_done(websocket, event)

                    recording_path = audio_recorder.finalize()
                    _recording_finalized = True
                    ended_at = datetime.now().isoformat()
                    meeting_store.complete_session(
                        session.session_id,
                        session.created_at,
                        ended_at,
                        recording_path=str(recording_path) if recording_path else None,
                        recording_bytes=audio_recorder.byte_count,
                        recording_error=audio_recorder.error_message,
                        status="completed" if item.reason == "stop" else "disconnected",
                        source="live",
                    )
                    if item.reason != "disconnect":
                        await websocket.send_json({
                            "type": "stopped",
                            "message": "Meeting ended",
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
        if not _recording_finalized:
            audio_recorder.finalize()
        session._transcriber = None
        session._audio_queue = None


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

async def emit_final_transcript_segment(session, event: RealtimeTranscriptEvent, websocket: WebSocket, *, spawn_fn=None):
    """Persist and broadcast a finalized transcript emission."""
    if spawn_fn is None:
        spawn_fn = _spawn
    meeting_config = _app_refs["meeting_config"]

    try:
        text = event.text.strip()
        if not text:
            return

        segment = session.add_transcript_segment(
            speaker="Voice",
            text=text,
            start_time=event.start_time,
            end_time=event.end_time,
            capture_start_time=event.capture_start_time,
            capture_duration=event.capture_duration,
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
                "capture_start_time": segment.capture_start_time,
                "capture_duration": segment.capture_duration,
            },
            "total_segments": len(session.transcript),
            "processing_time": event.processing_time,
        })

        if len(session.transcript) % meeting_config.summary_interval_turns == 0:
            spawn_fn(handle_summary_request(session, websocket))

        # Background rewrite: every 10 new segments, rewrite the latest chunk
        rewrite_interval = 10
        unrewritten = len(session.transcript) - session._last_rewritten_idx
        if unrewritten >= rewrite_interval:
            spawn_fn(session.rewrite_segments_chunked())

    except Exception as e:
        logger.error(f"Audio processing error: {e}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Transcription failed: {str(e)}"
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
            "message": "AI thinking..."
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
            raise RuntimeError("AI returned empty content")

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
            "message": f"AI answer failed: {str(e)}"
        })


async def handle_summary_request(session, websocket: WebSocket, template_id: str | None = None):
    """Update meeting summary - non-blocking"""
    try:
        await websocket.send_json({
            "type": "summary_status",
            "status": "updating",
            "message": "Updating summary..."
        })

        # Load template if template_id provided
        if template_id:
            meeting_store = _app_refs["meeting_store"]
            template = meeting_store.get_template(template_id)
            if template:
                session.template = template

        summary = await session.update_summary()

        if summary:
            await websocket.send_json({
                "type": "summary_update",
                "summary": summary,
                "transcript_count": len(session.transcript),
                "template_id": template_id or (session.template.get("id") if session.template else None),
            })

    except Exception as e:
        logger.error(f"Summary error: {e}")
        try:
            await websocket.send_json({
                "type": "summary_error",
                "message": f"Summary update failed: {e}",
            })
        except Exception:
            pass

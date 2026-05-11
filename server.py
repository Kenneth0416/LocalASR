"""
Meeting Realtime Voice - Main Server Facade

This module initializes services and serves as the entry point.
Actual routes are registered in focused modules:
- server_app.py  : FastAPI app and config loading
- ws_handler.py  : WebSocket endpoint and audio workers
- http_endpoints.py : HTTP REST endpoints

For backward compatibility, all public names are re-exported here.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure this module is accessible as "server" even when run as __main__,
# because ws_handler.py and http_endpoints.py use sys.modules["server"].
if __name__ == "__main__" and "server" not in sys.modules:
    sys.modules["server"] = sys.modules["__main__"]

import requests
from fastapi import Body, HTTPException

from asr import (
    ASRService,
    FinalOnlyASRRouter,
    RealtimeTranscriptEvent,
    WebRTCVADMeetingTranscriber,
)
from upload_tasks import UploadTaskTracker
from persistence import MeetingStore
from runtime_checks import build_runtime_snapshot
from session import ChatMessage, MeetingSession, MeetingSummary, SessionManager, TranscriptSegment
from server_app import app, get_configs, get_app

# Re-export configs from server_app
(llm_config, asr_config, realtime_vad_config, server_config, meeting_config,
 noise_suppression_config, agc_config, silero_vad_config, vad_backend) = get_configs()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
logger = logging.getLogger("meeting.server")

# Initialize services
asr_service = ASRService(asr_config)
meeting_store = MeetingStore(server_config.database_path)
session_manager = SessionManager(llm_config, meeting_config)
session_manager.store = meeting_store
upload_task_tracker = UploadTaskTracker()

cached_llm_status = "unknown"
SUPPORTED_PYTHON_SERIES = {(3, 11), (3, 12)}

# Exposed for tests
runtime_snapshot = None


# ── Config builders ────────────────────────────────────────────────────────────


def build_transformer_chunking_config(config):
    from asr import TransformerChunkingConfig
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


def build_realtime_transcriber(session_state: dict):
    """Build the realtime transcriber and preprocessor."""
    from audio_preprocessor import AudioPreprocessor

    router = FinalOnlyASRRouter(
        asr_service,
        language_getter=lambda: session_state.get("language"),
        context_getter=lambda: session_state.get("asr_prompt"),
    )

    # Select VAD backend
    if vad_backend == "silero":
        try:
            from vad import SileroVADTranscriber
            transcriber = SileroVADTranscriber(
                router=router,
                sample_rate=asr_config.sample_rate,
                config=silero_vad_config,
            )
        except RuntimeError as e:
            logger.warning("Silero VAD unavailable, falling back to WebRTC: %s", e)
            transcriber = WebRTCVADMeetingTranscriber(
                router=router,
                sample_rate=asr_config.sample_rate,
                config=realtime_vad_config,
            )
    else:
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            sample_rate=asr_config.sample_rate,
            config=realtime_vad_config,
        )

    # Build preprocessing chain
    ns_processor = None
    if noise_suppression_config.enabled:
        try:
            from noise_suppression import RNNoiseProcessor
            ns_processor = RNNoiseProcessor(
                sample_rate=asr_config.sample_rate,
                library_path=noise_suppression_config.library_path,
            )
            logger.info("RNNoise noise suppression enabled")
        except RuntimeError as e:
            logger.warning("RNNoise unavailable, disabling noise suppression: %s", e)

    agc_processor = None
    if agc_config.enabled:
        from agc import AGCProcessor
        agc_processor = AGCProcessor(
            config=agc_config,
            sample_rate=asr_config.sample_rate,
        )
        logger.info("AGC enabled (target=%.1f dB)", agc_config.target_rms_db)

    preprocessor = AudioPreprocessor(
        noise_suppression=ns_processor,
        agc=agc_processor,
    )

    return transcriber, preprocessor


# ── Status helpers ──────────────────────────────────────────────────────────────


def get_runtime_warnings() -> list[str]:
    warnings: list[str] = []
    python_series = sys.version_info[:2]

    if python_series not in SUPPORTED_PYTHON_SERIES:
        warnings.append(
            "Python 3.11/3.12 is the supported runtime for release builds; newer versions are best-effort only."
        )

    if python_series >= (3, 13):
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

    state = state_getter() if callable(state_getter) else (
        "ready" if getattr(service, "_initialized", False) else "idle"
    )
    error = error_getter() if callable(error_getter) else None
    return {
        "state": state,
        "error": error,
    }


def get_asr_status() -> dict[str, object]:
    status = _service_status(asr_service)
    return {
        "state": status["state"],
        "error": status["error"],
    }


def get_llm_status(timeout_sec: float = 3.0) -> str:
    try:
        if llm_config.local_only and not llm_config.is_local_provider():
            return f"blocked: local-only mode forbids provider '{llm_config.provider}'"

        # Both llamacpp and openai use the OpenAI-compatible /v1/models endpoint
        resp = requests.get(
            f"{llm_config.base_url}/v1/models",
            headers={"Authorization": f"Bearer {llm_config.api_key}"},
            timeout=timeout_sec,
        )
        return "ok" if resp.ok else f"error: {resp.status_code}"
    except Exception as e:
        return f"unreachable: {e}"


async def warmup_llm() -> None:
    """Send a minimal request to pre-warm the LLM server (KV cache allocation)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{llm_config.base_url}/v1/chat/completions",
                json={
                    "model": llm_config.model_name,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "temperature": 0.0,
                },
            )
            if resp.status_code == 200:
                logger.info("LLM warmup completed")
            else:
                logger.warning(f"LLM warmup returned status {resp.status_code}")
    except Exception as e:
        logger.warning(f"LLM warmup failed (non-fatal): {e}")


def refresh_runtime_snapshot(llm_status: str | None = None):
    global cached_llm_status, runtime_snapshot
    asr_status = get_asr_status()
    if llm_status is None:
        llm_status = cached_llm_status
    else:
        cached_llm_status = llm_status

    runtime_snapshot = build_runtime_snapshot(
        llm_config,
        asr_config,
        server_config,
        asr_state=str(asr_status["state"]),
        asr_error=asr_status["error"],
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


# ── Meeting helpers ────────────────────────────────────────────────────────────


def get_meeting_or_404(session_id: str) -> dict:
    meeting = meeting_store.get_meeting(session_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


def build_transcript_text(meeting: dict) -> str:
    lines: list[str] = []
    for segment in meeting.get("transcript", []):
        lines.append(
            f'{segment.get("speaker", "Voice")} '
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
        parts.extend([
            f"- Recording: {meeting['recording_path']}",
            f"- Recording Bytes: {meeting.get('recording_bytes', 0)}",
        ])

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


async def regenerate_meeting_summary(session_id: str, template_id: str | None = None) -> dict:
    meeting = get_meeting_or_404(session_id)
    temp_session = MeetingSession(f"{session_id}-summary", llm_config, meeting_config)

    # Load template if provided
    if template_id:
        template = meeting_store.get_template(template_id)
        if template:
            temp_session.template = template

    for segment in meeting.get("transcript", []):
        temp_session.add_transcript_segment(
            speaker=str(segment.get("speaker", "Voice")),
            text=str(segment.get("text", "")),
            start_time=float(segment.get("start_time", 0.0)),
            end_time=float(segment.get("end_time", 0.0)),
            capture_start_time=float(segment.get("capture_start_time", segment.get("start_time", 0.0))),
            capture_duration=float(
                segment.get(
                    "capture_duration",
                    max(float(segment.get("end_time", 0.0)) - float(segment.get("start_time", 0.0)), 0.0),
                )
            ),
        )

    summary = await temp_session.update_summary()
    if not summary:
        raise HTTPException(status_code=400, detail="Not enough transcript to generate summary")

    used_template_id = temp_session.template.get("id") if temp_session.template else None
    meeting_store.upsert_summary(
        session_id,
        meeting.get("created_at") or datetime.now().isoformat(),
        summary,
        temp_session.summary.updated_at if temp_session.summary else datetime.now().isoformat(),
        len(temp_session.transcript),
        template_id=used_template_id,
        last_summarized_ordinal=len(temp_session.transcript),
    )
    return get_meeting_or_404(session_id)


async def chat_on_meeting(session_id: str, payload: dict = Body(...)) -> dict:
    """Send a chat message and get AI response for a meeting. Works without WebSocket."""
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    meeting = get_meeting_or_404(session_id)
    session, created_by_us = _rehydrate_session(session_id, meeting)

    try:
        answer = await session.ask_llm(question)
        return {"answer": answer}
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=f"Chat failed: {str(e)}")
    finally:
        if created_by_us:
            session_manager.delete_session(session_id)


def _rehydrate_session(session_id: str, meeting: dict) -> tuple[MeetingSession, bool]:
    """Restore a MeetingSession from persisted meeting data. Returns (session, created_by_us)."""
    session = session_manager.get_session(session_id)
    created_by_us = False
    if session is None:
        session = session_manager.create_session()
        session = session_manager.register_recovered_session(session_id, session)
        created_by_us = True
        session.transcript = [
            TranscriptSegment(
                id=seg.get("id", ""),
                speaker=seg.get("speaker", "Voice"),
                text=seg.get("text", ""),
                start_time=float(seg.get("start_time", seg.get("start", 0.0))),
                end_time=float(seg.get("end_time", seg.get("end", 0.0))),
                capture_start_time=float(
                    seg.get("capture_start_time", seg.get("start_time", seg.get("start", 0.0)))
                ),
                capture_duration=float(
                    seg.get(
                        "capture_duration",
                        max(
                            float(seg.get("end_time", seg.get("end", 0.0)))
                            - float(seg.get("start_time", seg.get("start", 0.0))),
                            0.0,
                        ),
                    )
                ),
            )
            for seg in meeting.get("transcript", [])
        ]
        session.chat_history = [
            ChatMessage(
                id=msg.get("id", ""),
                role=msg.get("role", "user"),
                content=msg.get("content", ""),
            )
            for msg in meeting.get("chat_history", [])
        ]
        summary_text = (meeting.get("summary") or "").strip()
        if summary_text:
            session.summary = MeetingSummary(
                text=summary_text,
                updated_at=str(meeting.get("summary_updated_at") or datetime.now().isoformat()),
                turn_count=int(meeting.get("summary_turn_count") or 0),
            )
        session.last_summarized_ordinal = int(meeting.get("last_summarized_ordinal") or 0)
        session.state = {"language": meeting.get("language", None)}
    return session, created_by_us


async def chat_on_meeting_stream(session_id: str, question: str):
    """Stream AI answer for a meeting via SSE. Returns an async generator of SSE data lines."""
    import json

    meeting = get_meeting_or_404(session_id)
    session, created_by_us = _rehydrate_session(session_id, meeting)

    try:
        full_answer = []
        async for delta in session.stream_llm_answer(question):
            full_answer.append(delta)
            yield f"data: {json.dumps({'delta': delta})}\n\n"
        answer_text = "".join(full_answer)
        session.add_chat_message("user", question)
        session.add_chat_message("assistant", answer_text)
        yield f"data: {json.dumps({'done': True, 'answer': answer_text})}\n\n"
    except Exception as e:
        logger.error(f"Streaming chat error: {e}")
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    finally:
        if created_by_us:
            session_manager.delete_session(session_id)


# ── Lifecycle ─────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup():
    meeting_store.init_preset_templates()
    for warning in get_runtime_warnings():
        logger.warning(warning)
    initialize = getattr(asr_service, "initialize", None)
    if callable(initialize):
        await initialize()
    llm_status = await asyncio.to_thread(get_llm_status)
    snapshot = refresh_runtime_snapshot(llm_status=llm_status)
    for warning in snapshot.warnings:
        logger.warning(warning)
    if llm_status == "ok":
        await warmup_llm()


@app.on_event("shutdown")
async def on_shutdown():
    await upload_task_tracker.shutdown()
    await ws_handler.shutdown_workers()
    # Cancel any spawned HTTP tasks (e.g. summary generation)
    for task in http_endpoints._spawned_tasks:
        task.cancel()
    if http_endpoints._spawned_tasks:
        await asyncio.gather(*http_endpoints._spawned_tasks, return_exceptions=True)
    http_endpoints._spawned_tasks.clear()
    shutdown = getattr(asr_service, "shutdown", None)
    if callable(shutdown):
        await shutdown()


# ── Paths ──────────────────────────────────────────────────────────────────────


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RECORDINGS_DIR = Path(
    os.path.expanduser(server_config.recordings_dir or str(BASE_DIR / "recordings"))
)

if not RECORDINGS_DIR.is_absolute():
    RECORDINGS_DIR = BASE_DIR / RECORDINGS_DIR

if STATIC_DIR.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

app.state.STATIC_DIR = STATIC_DIR

# ── Inject refs into handler modules ──────────────────────────────────────────


import ws_handler
import http_endpoints
from ws_handler import handle_chat

ws_handler._inject(
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

http_endpoints._inject(
    asr_service=asr_service,
    asr_config=asr_config,
    llm_config=llm_config,
    meeting_config=meeting_config,
    server_config=server_config,
    session_manager=session_manager,
    meeting_store=meeting_store,
    RECORDINGS_DIR=RECORDINGS_DIR,
    get_asr_status=get_asr_status,
    get_llm_status=get_llm_status,
    get_runtime_warnings=get_runtime_warnings,
    is_readiness_snapshot_healthy=is_readiness_snapshot_healthy,
    build_meeting_markdown=build_meeting_markdown,
    build_transcript_text=build_transcript_text,
    regenerate_meeting_summary=regenerate_meeting_summary,
    chat_on_meeting_impl=chat_on_meeting,
    chat_on_meeting_stream_impl=chat_on_meeting_stream,
    upload_task_tracker=upload_task_tracker,
)

# ── SPA catch-all (MUST be after all API route registrations) ─────────────────
from fastapi.responses import FileResponse

@app.get("/{path:path}")
async def serve_spa(path: str):
    """Serve index.html for client-side routing; serve static files that exist on disk."""
    if path.startswith("api/") or path.startswith("static/") or path in ("docs", "openapi.json", "redoc"):
        raise HTTPException(status_code=404, detail="Not found")
    # Serve files that exist in STATIC_DIR (e.g. audio-worklet.js from Vite public/)
    static_file = STATIC_DIR / path
    if static_file.is_file() and ".." not in path:
        return FileResponse(str(static_file))
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "Meeting Realtime Voice API", "docs": "/docs"}


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    logger.info(f"Starting Meeting Realtime Voice Server...")
    logger.info(f"ASR Model: {asr_config.model_path}")
    logger.info(f"ASR Backend: {asr_config.backend}")
    logger.info(f"LLM Provider: {llm_config.provider}")
    logger.info(f"LLM Model: {llm_config.model_name}")
    logger.info(f"Server: {server_config.host}:{server_config.port}")

    import uvicorn
    uvicorn.run(
        app,
        host=server_config.host,
        port=server_config.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()


# ── Re-exports ─────────────────────────────────────────────────────────────────


__all__ = [
    "app",
    "get_app",
    "asr_service",
    "asr_config",
    "llm_config",
    "meeting_config",
    "server_config",
    "realtime_vad_config",
    "session_manager",
    "meeting_store",
    "build_realtime_transcriber",
    "build_transformer_chunking_config",
    "get_asr_status",
    "get_llm_status",
    "get_runtime_warnings",
    "refresh_runtime_snapshot",
    "runtime_snapshot",
    "is_readiness_snapshot_healthy",
    "get_meeting_or_404",
    "build_transcript_text",
    "build_meeting_markdown",
    "regenerate_meeting_summary",
    "chat_on_meeting",
    "runtime_snapshot",
    "RealtimeTranscriptEvent",
    "handle_chat",
    "on_startup",
    "on_shutdown",
    "main",
]

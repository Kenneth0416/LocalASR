"""
HTTP API endpoints.

This module registers all REST routes on the shared FastAPI app.
Imported by server.py at module level so decorators run at import time.
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from fastapi import Body, File, Form, HTTPException, UploadFile

from server_app import app

from asr import ASRService
from server_types import AudioQueueItem

logger = logging.getLogger("meeting.http")

_app_refs: dict = {}


def _inject(
    asr_service,
    asr_config,
    llm_config,
    meeting_config,
    server_config,
    session_manager,
    meeting_store,
    RECORDINGS_DIR,
    get_asr_status,
    get_llm_status,
    get_runtime_warnings,
    is_readiness_snapshot_healthy,
    build_meeting_markdown,
    build_transcript_text,
    regenerate_meeting_summary,
    chat_on_meeting_impl,
):
    _app_refs.update(
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
        chat_on_meeting_impl=chat_on_meeting_impl,
    )


# Re-export for backward compatibility with existing code that imports from here
def get_meeting_or_404(session_id: str) -> dict:
    meeting = sys.modules["server"].meeting_store.get_meeting(session_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


# ============================================================================
# Meeting chat (HTTP version)
# ============================================================================


@app.post("/api/meetings/{session_id}/chat")
async def chat_on_meeting(session_id: str, payload: dict = Body(...)):
    """Send a chat message and get AI response for a meeting (REST version)."""
    _chat_on_meeting = _app_refs["chat_on_meeting_impl"]
    return await _chat_on_meeting(session_id, payload)


# ============================================================================
# Health & Readiness
# ============================================================================


@app.get("/api/health")
async def health_check():
    """Cheap liveness endpoint for process-level health."""
    # Read patched module-level vars at call time so test patches apply
    _server = sys.modules["server"]
    asr_status = _server.get_asr_status()
    asr_config = _app_refs["asr_config"]
    llm_config = _app_refs["llm_config"]
    server_config = _app_refs["server_config"]
    session_manager = _app_refs["session_manager"]
    get_runtime_warnings = _app_refs["get_runtime_warnings"]
    snapshot = _server.refresh_runtime_snapshot(
        llm_status=_server.cached_llm_status
    ).to_dict()

    return {
        "status": "ok",
        "app": "meeting-realtime-voice",
        "python_version": sys.version.split()[0],
        "runtime_warnings": get_runtime_warnings(),
        "runtime": snapshot,
        "asr_ready": asr_status["state"] == "ready",
        "asr_state": asr_status["state"],
        "asr_error": asr_status["error"],
        "asr_model": str(asr_config.model_path),
        "asr_device": asr_config.device,
        "llm_provider": llm_config.provider,
        "llm_model": llm_config.model_name,
        "active_sessions": len(session_manager.sessions),
        "audio_queue_maxsize": server_config.audio_queue_maxsize,
    }


@app.get("/api/readiness")
async def readiness_check():
    """Dependency-oriented readiness endpoint."""
    _server = sys.modules["server"]
    asr_status = _server.get_asr_status()
    llm_status = await asyncio.to_thread(_server.get_llm_status)
    snapshot = _server.refresh_runtime_snapshot(llm_status=llm_status)
    status = "ok" if (
        _server.is_readiness_snapshot_healthy(snapshot)
        and asr_status["state"] == "ready"
    ) else "degraded"

    return {
        "status": status,
        "runtime": snapshot.to_dict(),
        "asr": asr_status,
        "llm_status": llm_status,
        "active_sessions": len(_app_refs["session_manager"].sessions),
    }


# ============================================================================
# Meeting CRUD
# ============================================================================


@app.get("/api/meetings")
async def list_meeting_history():
    """List persisted meeting history."""
    _server = sys.modules["server"]
    return {"meetings": _server.meeting_store.list_meetings(limit=_server.server_config.history_limit)}


@app.get("/api/meetings/{session_id}")
async def get_meeting_detail(session_id: str):
    """Fetch one persisted meeting with transcript, chat, and summary."""
    return get_meeting_or_404(session_id)


@app.get("/api/meetings/{session_id}/export.txt")
async def export_meeting_text(session_id: str):
    """Export transcript as plain text."""
    meeting = get_meeting_or_404(session_id)
    build_transcript_text = _app_refs["build_transcript_text"]
    filename = f"meeting-{session_id}.txt"
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        build_transcript_text(meeting) or "",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/meetings/{session_id}/export.md")
async def export_meeting_markdown(session_id: str):
    """Export meeting context as markdown."""
    meeting = get_meeting_or_404(session_id)
    build_meeting_markdown = _app_refs["build_meeting_markdown"]
    filename = f"meeting-{session_id}.md"
    from fastapi.responses import PlainTextResponse
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
    from fastapi.responses import JSONResponse
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

    from fastapi.responses import FileResponse
    return FileResponse(str(path), media_type="audio/wav", filename=path.name)


@app.patch("/api/meetings/{session_id}/transcript/{segment_id}")
async def update_meeting_transcript_segment(
    session_id: str,
    segment_id: str,
    payload: dict = Body(...),
):
    """Update one persisted transcript segment."""
    meeting_store = sys.modules["server"].meeting_store

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
    regenerate_meeting_summary = _app_refs["regenerate_meeting_summary"]
    return await regenerate_meeting_summary(session_id)


@app.delete("/api/meetings/{session_id}")
async def delete_meeting(session_id: str):
    """Delete one persisted meeting and its recording file."""
    meeting_store = sys.modules["server"].meeting_store
    deleted = meeting_store.delete_meeting(session_id, delete_recording=True)
    if not deleted:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"status": "deleted", "session_id": session_id}


# ============================================================================
# File upload transcription
# ============================================================================


@app.post("/api/upload")
async def upload_audio_transcribe(
    file: UploadFile = File(...),
    language: str = Form(default=""),
    asr_prompt: str = Form(default=""),
):
    """Upload an audio file for transcription. Supports WAV, MP3, M4A, OGG, FLAC."""
    _server = sys.modules["server"]
    asr_service = _server.asr_service
    session_manager = _server.session_manager
    meeting_store = _server.meeting_store
    meeting_config = _app_refs["meeting_config"]
    llm_config = _app_refs["llm_config"]
    regenerate_meeting_summary = _server.regenerate_meeting_summary

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

    file_content = await file.read()

    max_size = 500 * 1024 * 1024
    if len(file_content) > max_size:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 500MB.")
    if len(file_content) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")

    logger.info(f"Transcribing uploaded file: {file.filename} ({len(file_content)} bytes)")

    try:
        lang = language if language else None
        prompt = str(asr_prompt or "").strip() or None
        result = await asr_service.transcribe_wav(
            file_content,
            language=lang,
            context=prompt,
        )

        session = session_manager.create_session()
        session.state = {"language": lang}
        session.store = meeting_store

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
    _server = sys.modules["server"]
    if not _server.server_config.expose_session_api:
        raise HTTPException(status_code=404, detail="Not found")
    return {"sessions": _server.session_manager.list_sessions()}


@app.get("/")
async def root():
    """Serve frontend"""
    from server_app import app as _app
    # STATIC_DIR is injected into app state by server.py
    STATIC_DIR = getattr(_app.state, "STATIC_DIR", None)
    if STATIC_DIR and STATIC_DIR.exists():
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            from fastapi.responses import FileResponse
            return FileResponse(str(index_path))
    return {"message": "Meeting Realtime Voice API", "docs": "/docs"}

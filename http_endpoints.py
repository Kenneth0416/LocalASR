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
from uuid import uuid4

from fastapi import Body, File, Form, HTTPException, UploadFile

from server_app import app

from asr import ASRService
from server_types import AudioQueueItem

logger = logging.getLogger("meeting.http")

_app_refs: dict = {}

# Track fire-and-forget tasks for cleanup on shutdown
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
    chat_on_meeting_stream_impl,
    upload_task_tracker,
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
        chat_on_meeting_stream_impl=chat_on_meeting_stream_impl,
        upload_task_tracker=upload_task_tracker,
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


@app.post("/api/meetings/{session_id}/chat/stream")
async def chat_on_meeting_stream(session_id: str, payload: dict = Body(...)):
    """Stream AI answer for a meeting via SSE."""
    from fastapi.responses import StreamingResponse
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    _stream = _app_refs["chat_on_meeting_stream_impl"]
    return StreamingResponse(
        _stream(session_id, question),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        "asr_backend": asr_config.backend,
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


@app.get("/api/pipeline")
async def pipeline_stats():
    """Per-session live pipeline depth for debugging."""
    session_manager = _app_refs["session_manager"]
    server_config = _app_refs["server_config"]
    sessions = []
    for session in session_manager.sessions.values():
        transcriber = getattr(session, "_transcriber", None)
        audio_queue = getattr(session, "_audio_queue", None)
        entry = {
            "session_id": session.session_id,
            "audio_queue_depth": audio_queue.qsize() if audio_queue else None,
            "audio_queue_maxsize": server_config.audio_queue_maxsize,
        }
        if transcriber is not None:
            entry.update(transcriber.pipeline_stats())
        sessions.append(entry)
    return {"sessions": sessions}


# ============================================================================
# Template CRUD
# ============================================================================


@app.get("/api/templates")
async def list_templates():
    """List all summary templates."""
    meeting_store = sys.modules["server"].meeting_store
    return {"templates": meeting_store.list_templates()}


@app.post("/api/templates")
async def create_template(payload: dict = Body(...)):
    """Create a new summary template."""
    meeting_store = sys.modules["server"].meeting_store
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    system_prompt = str(payload.get("system_prompt", "")).strip()
    if not system_prompt:
        raise HTTPException(status_code=400, detail="system_prompt is required")
    user_prompt = str(payload.get("user_prompt", "")).strip()
    if not user_prompt:
        raise HTTPException(status_code=400, detail="user_prompt is required")

    from uuid import uuid4
    template_id = f"custom-{uuid4().hex[:8]}"
    template = meeting_store.create_template(
        template_id=template_id,
        name=name,
        description=str(payload.get("description", "")),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_format=str(payload.get("output_format", "markdown")),
        language=str(payload.get("language", "")),
        is_preset=False,
    )
    return {"template": template}


@app.get("/api/templates/{template_id}")
async def get_template(template_id: str):
    """Get a single template."""
    meeting_store = sys.modules["server"].meeting_store
    template = meeting_store.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"template": template}


@app.put("/api/templates/{template_id}")
async def update_template(template_id: str, payload: dict = Body(...)):
    """Update a template."""
    meeting_store = sys.modules["server"].meeting_store
    existing = meeting_store.get_template(template_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Template not found")

    fields = {}
    for key in ("name", "description", "system_prompt", "user_prompt", "output_format", "language"):
        if key in payload:
            fields[key] = str(payload[key])

    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    meeting_store.update_template(template_id, **fields)
    return {"template": meeting_store.get_template(template_id)}


@app.delete("/api/templates/{template_id}")
async def delete_template(template_id: str):
    """Delete a custom template. Preset templates cannot be deleted."""
    meeting_store = sys.modules["server"].meeting_store
    deleted = meeting_store.delete_template(template_id)
    if not deleted:
        existing = meeting_store.get_template(template_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Template not found")
        raise HTTPException(status_code=400, detail="Preset templates cannot be deleted")
    return {"status": "deleted", "template_id": template_id}


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
async def regenerate_summary(session_id: str, payload: dict = Body(None)):
    """Rebuild a persisted meeting summary from the stored transcript."""
    template_id = None
    if payload and isinstance(payload, dict):
        template_id = payload.get("template_id") or None
    regenerate_meeting_summary = _app_refs["regenerate_meeting_summary"]
    return await regenerate_meeting_summary(session_id, template_id=template_id)


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
    """Upload an audio file for async transcription. Supports WAV, MP3, M4A, OGG, FLAC."""
    _server = sys.modules["server"]
    tracker = _app_refs["upload_task_tracker"]
    RECORDINGS_DIR = _app_refs["RECORDINGS_DIR"]

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

    # Determine file extension
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1]

    # Save to tmp file
    task_id = str(uuid4())
    tmp_dir = RECORDINGS_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"upload-{task_id}{ext}"
    tmp_path.write_bytes(file_content)

    logger.info(f"Queued upload transcription: {file.filename} ({len(file_content)} bytes), task_id={task_id}")

    # Create task in tracker
    tracker.create(task_id=task_id, filename=file.filename or "unknown", tmp_path=str(tmp_path))

    # Launch background processing and track for cancellation
    bg_task = _spawn(_process_upload(task_id, tracker))
    tracker.get(task_id)._asyncio_task = bg_task

    return {"task_id": task_id, "status": "queued", "filename": file.filename}


async def _process_upload(task_id: str, tracker):
    """Background worker that processes an uploaded audio file."""
    _server = sys.modules["server"]
    asr_service = _server.asr_service
    session_manager = _server.session_manager
    meeting_store = _server.meeting_store
    asr_config = _app_refs["asr_config"]
    regenerate_meeting_summary = _server.regenerate_meeting_summary

    task = tracker.get(task_id)
    if task is None:
        return

    tmp_path = Path(task.tmp_path) if task.tmp_path else None
    upload_session = None

    try:
        tracker.update(task_id, status="processing")

        file_content = tmp_path.read_bytes()
        lang = None  # language not passed in this path; kept for future use

        # Estimate audio duration from file size
        estimated_sec = len(file_content) / 32000
        use_batched = estimated_sec >= asr_config.upload_min_audio_sec
        if use_batched:
            logger.info(
                "Task %s: long audio (~%.0fs estimated), using VAD-chunked batch inference",
                task_id, estimated_sec,
            )
            result = await asr_service.transcribe_wav_batched(
                file_content,
                language=lang,
                context=None,
            )
        else:
            result = await asr_service.transcribe_wav(
                file_content,
                language=lang,
                context=None,
            )

        upload_session = session_manager.create_session()
        session = upload_session
        session.state = {"language": lang}
        tracker.update(task_id, session_id=session.session_id)

        segments = []
        if result.segments:
            for seg in result.segments:
                segment = session.add_transcript_segment(
                    speaker="Speaker 1",
                    text=seg.get("text", "").strip(),
                    start_time=float(seg.get("start", 0.0)),
                    end_time=float(seg.get("end", 0.0)),
                    capture_start_time=float(seg.get("start", 0.0)),
                    capture_duration=max(float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)), 0.0),
                )
                segments.append({
                    "id": segment.id,
                    "speaker": segment.speaker,
                    "text": segment.text,
                    "start_time": segment.start_time,
                    "end_time": segment.end_time,
                    "capture_start_time": segment.capture_start_time,
                    "capture_duration": segment.capture_duration,
                    "timestamp": segment.timestamp,
                })
        elif result.text:
            duration = result.audio_duration or 0.0
            segment = session.add_transcript_segment(
                speaker="Speaker 1",
                text=result.text,
                start_time=0.0,
                end_time=duration,
                capture_start_time=0.0,
                capture_duration=duration,
            )
            segments.append({
                "id": segment.id,
                "speaker": segment.speaker,
                "text": segment.text,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "capture_start_time": segment.capture_start_time,
                "capture_duration": segment.capture_duration,
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
            source="upload",
        )

        summary_state = "unavailable"
        if len(segments) >= 3:
            summary_state = "queued"
            _spawn(regenerate_meeting_summary(session.session_id))

        result_data = {
            "session_id": session.session_id,
            "segments": segments,
            "full_text": result.text,
            "audio_duration": result.audio_duration,
            "processing_time": result.processing_time,
            "summary_state": summary_state,
        }

        tracker.update(task_id, status="completed", result=result_data)
        logger.info(
            "Task %s completed: %d segments, session=%s",
            task_id, len(segments), session.session_id,
        )

    except asyncio.CancelledError:
        tracker.update(task_id, status="failed", error="cancelled")
        logger.info("Task %s cancelled", task_id)
    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e)
        tracker.update(task_id, status="failed", error=str(e))
    finally:
        # Clean up upload session from SessionManager
        if upload_session is not None:
            session_manager.delete_session(upload_session.session_id)
        # Clean up tmp file
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        # Run tracker cleanup for expired tasks
        tracker.cleanup_older_than()


@app.get("/api/upload/{task_id}/status")
async def upload_task_status(task_id: str):
    """Get the status of an async upload transcription task."""
    tracker = _app_refs["upload_task_tracker"]
    task = tracker.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_dict()


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

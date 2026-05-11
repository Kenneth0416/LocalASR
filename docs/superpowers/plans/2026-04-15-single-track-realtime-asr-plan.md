# Single-Track Realtime ASR Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove preview/final dual-track realtime ASR and restore one VAD-segmented, final-only realtime transcription path across frontend, backend, config, docs, and tests.

**Architecture:** Keep WebRTC VAD utterance segmentation in `vad.py`, but collapse runtime wiring to one `ASRService` and one final-only router path. Remove preview-specific websocket/session/frontend state, replace live transcript upsert logic with final-only duplicate suppression keyed by persisted segment `id`, and update status endpoints plus docs so the whole system describes one ASR lane instead of a preview/final split.

**Tech Stack:** FastAPI, asyncio, Python `unittest`, Node test runner, Web Audio worklet, SQLite persistence, existing `ASRService`

---

## Planned File Changes

- Modify: `config.py:113-145,243-328` — remove preview timing/runtime config and shrink `load_config()` to one ASR config plus VAD config
- Modify: `server_app.py:11-40` — adjust config tuple unpacking to the new single-ASR shape
- Modify: `server.py:28-55,85-104,151-170,350-376` — remove preview service lifecycle, simplify realtime router wiring, and collapse ASR status to one lane
- Modify: `http_endpoints.py:93-148` — remove preview fields from health/readiness payloads
- Modify: `ws_handler.py:161-228,376-530` — send single-track `ready` metadata and final-only transcript events
- Modify: `asr.py:30-182` — delete `PreviewFinalASRRouter`, keep a single final-only router export
- Modify: `asr_types.py:45-96,232-249` — strip preview-only router/state fields from shared realtime types
- Modify: `vad.py:61-260` — remove preview scheduling/fallback logic and emit final-only events
- Modify: `session.py:67-175` — delete `LivePreviewSegment` and preview cache helpers
- Delete: `static/live-transcript-state.mjs` — remove preview-aware transcript state helper
- Create: `static/final-transcript-store.mjs` — tiny pure helper for final-segment duplicate suppression
- Modify: `static/app.js:8-10,38-70,733-766,1407-1444,1545-1605` — switch live transcript rendering to final-only append semantics
- Modify: `static/styles.css:712-724` — delete preview-specific transcript styling
- Delete: `tests/test_live_transcript_state.mjs` — remove preview/revision-specific frontend tests
- Create: `tests/test_final_transcript_store.mjs` — final-only duplicate suppression tests
- Modify: `tests/test_server_endpoints.py` — assert single-ASR startup/health/readiness/ready payloads
- Modify: `tests/test_server_websocket.py` — assert final-only realtime websocket behavior
- Modify: `tests/test_realtime_transcriber.py` — assert final-only VAD transcriber behavior
- Modify: `.env.example:34-46` — remove preview env vars from documented config
- Modify: `README.md:129-141,180` — document one realtime ASR lane and remove preview model guidance
- Modify: `docs/reports/PIPELINE_MERMAID_DIAGRAMS.md` — update diagrams or annotate preview/final flow as obsolete

## Task 1: Collapse Config, Startup, and Status Payloads to One ASR Lane

**Files:**
- Modify: `tests/test_server_endpoints.py`
- Modify: `config.py:113-145,243-328`
- Modify: `server_app.py:11-40`
- Modify: `server.py:28-55,85-104,151-170,350-376`
- Modify: `http_endpoints.py:93-148`
- Modify: `ws_handler.py:161-228`

- [ ] **Step 1: Write failing backend tests for single-track health and websocket ready payloads**

Add these tests to `tests/test_server_endpoints.py`:

```python
    def test_startup_event_initializes_single_asr_service(self):
        fake_asr = make_fake_asr(initialized=False)

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "get_runtime_warnings", return_value=[]), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "refresh_runtime_snapshot", return_value=make_runtime_snapshot("startup-single"), create=True):
            with TestClient(server.app):
                pass

        fake_asr.initialize.assert_awaited_once()

    def test_shutdown_event_shuts_down_single_asr_service(self):
        fake_asr = make_fake_asr(initialized=True)

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "get_runtime_warnings", return_value=[]), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "refresh_runtime_snapshot", return_value=make_runtime_snapshot("shutdown-single"), create=True):
            with TestClient(server.app):
                pass

        fake_asr.shutdown.assert_awaited_once()

    def test_health_endpoint_surfaces_single_asr_metadata(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_asr.initialization_state = lambda: "ready"
        fake_asr.initialization_error = lambda: None
        fake_runtime_snapshot = make_runtime_snapshot("health-single")

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "refresh_runtime_snapshot", return_value=fake_runtime_snapshot, create=True):
            with TestClient(server.app) as client:
                response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["asr_ready"], True)
        self.assertEqual(payload["asr_state"], "ready")
        self.assertEqual(payload["asr_model"], str(server.asr_config.model_path))
        self.assertNotIn("final_asr_model", payload)
        self.assertNotIn("preview_asr_model", payload)
        self.assertNotIn("final_asr_state", payload)
        self.assertNotIn("preview_asr_state", payload)
        self.assertNotIn("preview_enabled", payload)

    def test_readiness_degrades_when_single_asr_is_not_ready(self):
        fake_asr = make_fake_asr(initialized=False)
        fake_asr.initialization_state = lambda: "failed"
        fake_asr.initialization_error = lambda: "asr failed"
        fake_runtime_snapshot = make_runtime_snapshot("readiness-single")

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "refresh_runtime_snapshot", return_value=fake_runtime_snapshot, create=True):
            with TestClient(server.app) as client:
                response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["checks"]["asr"], "failed")
        self.assertNotIn("preview", payload["checks"])

    def test_websocket_ready_payload_omits_preview_metadata(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_runtime_snapshot = make_runtime_snapshot("ready-single")

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "refresh_runtime_snapshot", return_value=fake_runtime_snapshot, create=True):
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/meeting") as websocket:
                    ready = websocket.receive_json()

        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["asr_model"], str(server.asr_config.model_path))
        self.assertEqual(ready["asr_state"], "ready")
        self.assertNotIn("final_asr_model", ready)
        self.assertNotIn("preview_asr_model", ready)
        self.assertNotIn("final_asr_state", ready)
        self.assertNotIn("preview_asr_state", ready)
        self.assertNotIn("preview_enabled", ready)
```

- [ ] **Step 2: Run the failing backend tests**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_server_endpoints.ServerEndpointTests.test_startup_event_initializes_single_asr_service \
  tests.test_server_endpoints.ServerEndpointTests.test_shutdown_event_shuts_down_single_asr_service \
  tests.test_server_endpoints.ServerEndpointTests.test_health_endpoint_surfaces_single_asr_metadata \
  tests.test_server_endpoints.ServerEndpointTests.test_readiness_degrades_when_single_asr_is_not_ready \
  tests.test_server_endpoints.ServerEndpointTests.test_websocket_ready_payload_omits_preview_metadata \
  -v
```

Expected:

- FAIL because startup/shutdown tests still observe two ASR services
- FAIL because `/api/health` still returns `final_asr_*`, `preview_asr_*`, and `preview_enabled`
- FAIL because `/api/readiness` still reflects preview readiness logic
- FAIL because websocket `ready` still includes preview/final split metadata

- [ ] **Step 3: Remove preview config loading and single-ASR tuple mismatches**

Update `config.py` so `WebRTCVADConfig` drops preview timing fields and `load_config()` returns one ASR config:

```python
@dataclass
class WebRTCVADConfig:
    frame_ms: int = 20
    vad_aggressiveness: int = 2
    enter_speech_frames: int = 2
    endpoint_silence_frames: int = 36
    max_utterance_sec: float = 18.0
    pre_roll_sec: float = 0.2
    min_final_audio_sec: float = 0.1

    def __post_init__(self):
        self.frame_ms = int(self.frame_ms)
        self.vad_aggressiveness = int(self.vad_aggressiveness)
        self.enter_speech_frames = int(self.enter_speech_frames)
        self.endpoint_silence_frames = int(self.endpoint_silence_frames)
        self.max_utterance_sec = float(self.max_utterance_sec)
        self.pre_roll_sec = float(self.pre_roll_sec)
        self.min_final_audio_sec = float(self.min_final_audio_sec)

        if self.frame_ms not in {10, 20, 30}:
            raise ValueError("frame_ms must be one of 10, 20, 30")
        if not 0 <= self.vad_aggressiveness <= 3:
            raise ValueError("vad_aggressiveness must be between 0 and 3")
        if self.enter_speech_frames <= 0:
            raise ValueError("enter_speech_frames must be > 0")
        if self.endpoint_silence_frames <= 0:
            raise ValueError("endpoint_silence_frames must be > 0")
        if self.max_utterance_sec <= 0:
            raise ValueError("max_utterance_sec must be > 0")
        if self.pre_roll_sec < 0:
            raise ValueError("pre_roll_sec must be >= 0")
        if self.min_final_audio_sec < 0:
            raise ValueError("min_final_audio_sec must be >= 0")


def _build_webrtc_vad_config() -> WebRTCVADConfig:
    return WebRTCVADConfig(
        frame_ms=int(_env("WEBRTC_VAD_FRAME_MS", "20")),
        vad_aggressiveness=int(_env("WEBRTC_VAD_AGGRESSIVENESS", "2")),
        enter_speech_frames=int(_env("WEBRTC_VAD_ENTER_SPEECH_FRAMES", "2")),
        endpoint_silence_frames=int(_env("WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES", "36")),
        max_utterance_sec=float(_env("WEBRTC_VAD_MAX_UTTERANCE_SEC", "18.0")),
        pre_roll_sec=float(_env("WEBRTC_VAD_PRE_ROLL_SEC", "0.2")),
        min_final_audio_sec=float(_env("WEBRTC_VAD_MIN_FINAL_AUDIO_SEC", "0.1")),
    )


def load_config() -> tuple[LLMConfig, ASRConfig, WebRTCVADConfig, ServerConfig, MeetingConfig]:
    llm = LLMConfig(...)
    asr = _build_final_asr_config()
    realtime_vad = _build_webrtc_vad_config()
    server = ServerConfig(...)
    meeting = MeetingConfig(...)
    return llm, asr, realtime_vad, server, meeting
```

Update `server_app.py` to unpack the new tuple:

```python
_llm_config, _asr_config, _realtime_vad_config, _server_config, _meeting_config = load_config()


def get_configs():
    return (
        _llm_config,
        _asr_config,
        _realtime_vad_config,
        _server_config,
        _meeting_config,
    )
```

- [ ] **Step 4: Collapse runtime wiring and status payloads to one ASR service**

Update `server.py` to remove preview service wiring:

```python
from asr import (
    ASRService,
    FinalOnlyASRRouter,
    RealtimeTranscriptEvent,
    WebRTCVADMeetingTranscriber,
)

(llm_config, asr_config, realtime_vad_config, server_config, meeting_config) = get_configs()

asr_service = ASRService(asr_config)


def build_realtime_transcriber(session_state: dict) -> WebRTCVADMeetingTranscriber:
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


def get_asr_status() -> dict[str, object]:
    status = _service_status(asr_service)
    return {
        "state": status["state"],
        "error": status["error"],
    }


@app.on_event("startup")
async def on_startup():
    for warning in get_runtime_warnings():
        logger.warning(warning)
    initialize = getattr(asr_service, "initialize", None)
    if callable(initialize):
        await initialize()
    llm_status = await asyncio.to_thread(get_llm_status)
    snapshot = refresh_runtime_snapshot(llm_status=llm_status)
    for warning in snapshot.warnings:
        logger.warning(warning)


@app.on_event("shutdown")
async def on_shutdown():
    shutdown = getattr(asr_service, "shutdown", None)
    if callable(shutdown):
        await shutdown()
```

Update `http_endpoints.py` health/readiness payloads:

```python
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

    status = "ok" if (_server.is_readiness_snapshot_healthy(snapshot)
                      and asr_status["state"] == "ready") else "degraded"
```

Update websocket `ready` in `ws_handler.py`:

```python
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
```

- [ ] **Step 5: Run the backend tests again**

Run:

```bash
venv/bin/python -m unittest tests.test_server_endpoints -v
```

Expected:

- PASS
- startup/shutdown tests now assert one service only
- health/readiness/ready payload tests no longer see preview metadata

- [ ] **Step 6: Commit**

```bash
git add config.py server_app.py server.py http_endpoints.py ws_handler.py tests/test_server_endpoints.py
git commit -m "refactor: collapse realtime ASR runtime to one lane"
```

## Task 2: Make the Realtime VAD Path Final-Only

**Files:**
- Modify: `tests/test_realtime_transcriber.py`
- Modify: `tests/test_server_websocket.py`
- Modify: `asr.py:30-182`
- Modify: `asr_types.py:45-96,232-249`
- Modify: `vad.py:61-260`
- Modify: `session.py:67-175`
- Modify: `ws_handler.py:376-530`

- [ ] **Step 1: Replace preview-oriented realtime tests with final-only expectations**

Replace the preview-heavy tests in `tests/test_realtime_transcriber.py` with final-only cases like:

```python
class FakeRouter:
    def __init__(self, final_results=None):
        self.final_results = iter(final_results or [])
        self.final_calls = []

    async def transcribe_final(self, audio_tuple):
        self.final_calls.append(audio_tuple)
        return next(self.final_results)


class WebRTCVADMeetingTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_only_one_final_event_after_endpoint(self):
        router = FakeRouter(final_results=[LiteASRResult(text="最终定稿。", duration_sec=1.4)])
        decisions = [True] * 60 + [False] * 12
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            config=WebRTCVADConfig(frame_ms=20, enter_speech_frames=2, endpoint_silence_frames=8, max_utterance_sec=3.0),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )

        events = []
        for _ in range(len(decisions)):
            events.extend(await transcriber.push_frame(make_pcm((0.02, 1800)), sample_count=320))
        events.extend(await transcriber.flush())

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].is_final)
        self.assertEqual(events[0].text, "最终定稿。")
```

Add websocket assertions to `tests/test_server_websocket.py`:

```python
    def test_websocket_transcript_events_are_final_only(self):
        fake_transcriber = FakeRealtimeTranscriber(
            push_results=[[
                make_realtime_event(
                    event_type="final",
                    text="只发定稿",
                    segment_id=1,
                    revision=1,
                    is_final=True,
                    cut_reason="endpoint",
                )
            ]]
        )
        session = MeetingSession("session-final-only", server.llm_config, server.meeting_config, store=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(server, "asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server.session_manager, "create_session", return_value=session), \
                 patch.object(server.meeting_store, "complete_session"), \
                 patch.object(server, "RECORDINGS_DIR", Path(tmpdir)):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        websocket.receive_json()
                        websocket.send_text(json.dumps({"type": "start"}))
                        receive_until_type(websocket, "status")
                        receive_until_type(websocket, "start_ack")
                        websocket.send_bytes(make_pcm((0.1, 1800)))
                        transcript = receive_until_type(websocket, "transcript")
                        done = receive_until_type(websocket, "transcribe_done")

        self.assertEqual(transcript["segment"]["text"], "只发定稿")
        self.assertEqual(transcript["segment"]["id"], session.transcript[0].id)
        self.assertTrue(done["is_final"])
        self.assertEqual(done["phase"], "final")
```

- [ ] **Step 2: Run the realtime/backend tests to verify they fail**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_realtime_transcriber \
  tests.test_server_websocket.ServerWebSocketTests.test_websocket_transcript_events_are_final_only \
  -v
```

Expected:

- FAIL because the transcriber still emits preview events or preview-related fallback behavior
- FAIL because websocket still tries to call `emit_preview_transcript_segment()` and preview cache helpers

- [ ] **Step 3: Delete preview-only shared types, router exports, and session preview cache**

Simplify `asr_types.py` so the realtime dataclasses no longer model preview state:

```python
@dataclass(frozen=True)
class RealtimeTranscriptEvent:
    """A finalized realtime transcript emitted by the VAD flow."""
    event_type: str
    text: str
    start_time: float
    end_time: float
    processing_time: float
    segment_id: int
    revision: int
    is_final: bool
    cut_reason: str = "endpoint"


@dataclass
class UtteranceState:
    """Tracks one in-progress speech utterance within the VAD state machine."""
    segment_id: int
    revision: int
    start_sample: int
    end_sample: int | None
    last_speech_sample: int
    pcm_buffer: bytearray = field(default_factory=bytearray)
    sealed: bool = False
    final_task: Optional["asyncio.Task"] = None


class BaseASRRouter:
    """Interface that realtime ASR routers must implement."""

    async def transcribe_final(self, audio_tuple) -> LiteASRResult:
        raise NotImplementedError
```

Simplify `asr.py` to one exported router:

```python
class FinalOnlyASRRouter(BaseASRRouter):
    def __init__(
        self,
        final_service: "ASRService",
        *,
        language_getter: "Callable[[], str | None]",
        context_getter: "Callable[[], str | None]",
    ):
        from asr_service import ASRService

        self._final_service: ASRService = final_service
        self._language_getter = language_getter
        self._context_getter = context_getter
        self._previous_segment_text: str = ""

    def _build_context(self) -> str | None:
        static = self._context_getter() or ""
        prev = self._previous_segment_text
        if prev:
            combined = (static + "\n" + prev).strip()
            return combined if combined else None
        return static if static else None

    async def transcribe_final(self, audio_tuple) -> LiteASRResult:
        context = self._build_context()
        result = await self._final_service.transcribe_wav(
            audio_tuple,
            language=self._language_getter(),
            context=context,
        )
        text = (result.text or "").strip()
        if text:
            self._previous_segment_text = text
        return LiteASRResult(
            text=text,
            duration_sec=float(getattr(result, "audio_duration", 0.0) or 0.0),
        )
```

Delete preview cache types/helpers from `session.py`:

```python
class MeetingSession:
    def __init__(...):
        self.transcript: list[TranscriptSegment] = []
        self.chat_history: list[ChatMessage] = []
        self.summary: Optional[MeetingSummary] = None
        self.created_at = datetime.now().isoformat()
        self._transcript_lock = asyncio.Lock()
```

Also remove `LivePreviewSegment`, `upsert_live_preview_segment()`, `clear_live_preview_segment()`, and `clear_live_preview_segments()`.

- [ ] **Step 4: Remove preview scheduling from the VAD transcriber and websocket worker**

Make `vad.py` emit final-only events:

```python
class WebRTCVADMeetingTranscriber:
    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for offset in range(0, len(pcm_chunk), self._frame_bytes):
            frame = pcm_chunk[offset:offset + self._frame_bytes]
            if len(frame) != self._frame_bytes:
                break
            events.extend(await self.push_frame(frame, sample_count=self._frame_samples))
        events.extend(await self._drain_finished_tasks())
        return events

    async def push_frame(self, pcm_chunk: bytes, sample_count: Optional[int] = None) -> list[RealtimeTranscriptEvent]:
        sample_count = sample_count or self._frame_samples
        events: list[RealtimeTranscriptEvent] = []
        is_speech = self._vad.is_speech(pcm_chunk, self.sample_rate)
        ...
        if self._active is not None:
            if not created_now:
                self._active.pcm_buffer.extend(pcm_chunk)
            if is_speech:
                self._active.last_speech_sample = frame_end

            utterance_duration_sec = len(self._active.pcm_buffer) / (self.sample_rate * self._bytes_per_sample)
            if self._silence_run >= self.config.endpoint_silence_frames:
                self._seal_active("endpoint")
            elif utterance_duration_sec >= self.config.max_utterance_sec:
                self._seal_active("max_duration")

        events.extend(await self._drain_finished_tasks())
        return events
```

The final sealing path must stop using preview fallback:

```python
        async def run_final():
            try:
                result = await self.router.transcribe_final(
                    pcm16le_to_audio_tuple(snapshot, self.sample_rate)
                )
                text = (result.text or "").strip()
                if not text:
                    return segment_id, None
            except Exception:
                return segment_id, None

            return segment_id, RealtimeTranscriptEvent(
                event_type="final",
                text=text,
                start_time=start_time,
                end_time=end_time,
                processing_time=0.0,
                segment_id=segment_id,
                revision=revision,
                is_final=True,
                cut_reason=reason,
            )
```

Simplify `ws_handler.py` to one emit path:

```python
                    events = await transcriber.push_pcm(pcm_chunk)
                    for event in events:
                        await emit_final_transcript_segment(session, event, websocket)
                        await send_transcribe_done(websocket, event)

                if item.event_type == "eos":
                    events = await transcriber.flush(reason=item.reason)
                    for event in events:
                        await emit_final_transcript_segment(session, event, websocket)
                        await send_transcribe_done(websocket, event)


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
            "is_final": True,
            "cut_reason": event.cut_reason,
            "speaker": segment.speaker,
            "text": segment.text,
            "start": segment.start_time,
            "end": segment.end_time,
        },
        "total_segments": len(session.transcript),
        "processing_time": event.processing_time,
    })
```

- [ ] **Step 5: Run the realtime regression tests again**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_realtime_transcriber \
  tests.test_server_websocket \
  -v
```

Expected:

- PASS
- no preview event assertions remain
- buffered stop/disconnect paths still persist final transcript segments

- [ ] **Step 6: Commit**

```bash
git add asr.py vad.py session.py ws_handler.py tests/test_realtime_transcriber.py tests/test_server_websocket.py
git commit -m "refactor: make realtime VAD transcription final-only"
```

## Task 3: Simplify Frontend Live Transcript Rendering to Final-Only State

**Files:**
- Delete: `static/live-transcript-state.mjs`
- Create: `static/final-transcript-store.mjs`
- Modify: `static/app.js:8-10,38-70,733-766,1407-1444,1545-1605`
- Modify: `static/styles.css:712-724`
- Delete: `tests/test_live_transcript_state.mjs`
- Create: `tests/test_final_transcript_store.mjs`

- [ ] **Step 1: Write the failing frontend test for final-segment duplicate suppression**

Create `tests/test_final_transcript_store.mjs`:

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';

import { createFinalTranscriptStore } from '../static/final-transcript-store.mjs';

test('accepts the first persisted segment id and rejects duplicates', () => {
    const store = createFinalTranscriptStore();

    assert.equal(store.accept({ id: 'seg-1' }), true);
    assert.equal(store.accept({ id: 'seg-1' }), false);
    assert.equal(store.accept({ id: 'seg-2' }), true);
});

test('ignores empty ids so upload/history callers can guard upstream', () => {
    const store = createFinalTranscriptStore();

    assert.equal(store.accept({ id: '' }), false);
    assert.equal(store.accept({}), false);
});

test('reset clears tracked ids between meetings', () => {
    const store = createFinalTranscriptStore();

    assert.equal(store.accept({ id: 'seg-1' }), true);
    store.reset();
    assert.equal(store.accept({ id: 'seg-1' }), true);
});
```

- [ ] **Step 2: Run the frontend test to verify it fails**

Run:

```bash
node --test tests/test_final_transcript_store.mjs
```

Expected:

- FAIL with `ERR_MODULE_NOT_FOUND` because `static/final-transcript-store.mjs` does not exist yet

- [ ] **Step 3: Replace the preview-aware transcript store with a final-only helper and app wiring**

Create `static/final-transcript-store.mjs`:

```javascript
export function createFinalTranscriptStore() {
    const seenIds = new Set();

    return {
        accept(segment = {}) {
            const id = String(segment.id || '');
            if (!id || seenIds.has(id)) {
                return false;
            }
            seenIds.add(id);
            return true;
        },

        reset() {
            seenIds.clear();
        },
    };
}
```

Update `static/app.js` imports and state:

```javascript
import { createFinalTranscriptStore } from '/static/final-transcript-store.mjs';

const state = {
    ...
    asrPromptDraft: createAsrPromptDraft(),
    finalTranscriptStore: createFinalTranscriptStore(),
};
```

Replace `upsertTranscriptSegment()` with final-only append logic:

```javascript
function appendFinalTranscriptSegment(segment) {
    if (!state.finalTranscriptStore.accept(segment)) {
        return;
    }

    const empty = el.transcriptList.querySelector('.empty-state');
    if (empty) empty.remove();

    if (latestSegmentEl) {
        latestSegmentEl.classList.remove('latest');
    }

    const group = document.createElement('div');
    group.className = 'segment-group';
    group.innerHTML = buildTranscriptBlockMarkup(
        {
            id: segment.id,
            speaker: segment.speaker,
            text: segment.text,
            start: segment.start,
            end: segment.end,
        },
        {
            latest: true,
            editable: false,
        },
    );

    const nextNode = group.firstElementChild;
    el.transcriptList.appendChild(nextNode);
    latestSegmentEl = nextNode.querySelector('.transcript-block');
    el.transcriptList.scrollTop = el.transcriptList.scrollHeight;
    updateTranscriptCount();
}
```

Update JSON handling and cleanup:

```javascript
        case 'transcript':
            appendFinalTranscriptSegment(msg.segment);
            if (msg.processing_time) {
                setProcessingStatus(`${msg.processing_time.toFixed(2)}s`);
            }
            break;

function cleanup(options = {}) {
    ...
    state.finalTranscriptStore.reset();
    teardownRecordingResources();
    latestSegmentEl = null;
}

function resetUI() {
    ...
    state.finalTranscriptStore.reset();
    ...
}
```

Remove preview-only rendering branches from `buildTranscriptBlockMarkup()`:

```javascript
function buildTranscriptBlockMarkup(segment, options = {}) {
    const segmentId = String(segment.id || '');
    const speaker = String(segment.speaker || '发言人');
    const timeLabel = formatSegmentRange(segment);
    const editable = Boolean(options.editable);
    const isEditing = editable && state.currentEditingSegmentId === segmentId;
    ...
    return `
        <div class="segment-group">
            <div class="transcript-block${options.latest ? ' latest' : ''}" data-segment-id="${escapeHtml(segmentId)}">
                <div class="segment-meta">
                    <span class="segment-speaker">${escapeHtml(speaker)}</span>
                    ${actionMarkup || `<span class="segment-time">${escapeHtml(timeLabel)}</span>`}
                </div>
                ${actionMarkup ? `<div class="segment-time">${escapeHtml(timeLabel)}</div>` : ''}
                <div class="segment-text">${escapeHtml(String(segment.text || ''))}</div>
            </div>
        </div>
    `;
}
```

Delete `static/live-transcript-state.mjs` and `tests/test_live_transcript_state.mjs`.

Remove preview transcript styles from `static/styles.css`:

```css
.transcript-block.latest {
  border-left-color: var(--accent-hover);
  background: rgba(56, 189, 187, 0.06);
  box-shadow: 0 0 0 1px var(--accent-border), 0 4px 16px var(--accent-glow);
}
```

- [ ] **Step 4: Run the frontend tests again**

Run:

```bash
node --test \
  tests/test_final_transcript_store.mjs \
  tests/test_ui_formatters.mjs \
  tests/test_transcription_options.mjs
```

Expected:

- PASS
- no dependency on `static/live-transcript-state.mjs`

- [ ] **Step 5: Commit**

```bash
git add static/app.js static/final-transcript-store.mjs static/styles.css tests/test_final_transcript_store.mjs
git rm static/live-transcript-state.mjs tests/test_live_transcript_state.mjs
git commit -m "refactor: simplify live transcript UI to final-only state"
```

## Task 4: Clean Documentation and Run the Final Regression Sweep

**Files:**
- Modify: `.env.example:34-46`
- Modify: `README.md:129-141,180`
- Modify: `docs/reports/PIPELINE_MERMAID_DIAGRAMS.md`

- [ ] **Step 1: Update the documented config and architecture to one ASR lane**

Update `.env.example` by removing the preview block and preview VAD fields so the realtime config section looks like:

```env
# Realtime VAD
WEBRTC_VAD_FRAME_MS=20
WEBRTC_VAD_AGGRESSIVENESS=2
WEBRTC_VAD_ENTER_SPEECH_FRAMES=2
WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES=36
WEBRTC_VAD_MAX_UTTERANCE_SEC=18.0
WEBRTC_VAD_PRE_ROLL_SEC=0.2
WEBRTC_VAD_MIN_FINAL_AUDIO_SEC=0.1
```

Replace the README split-lane section with:

```md
### Realtime ASR

- realtime mode uses WebRTC VAD to segment live audio into utterances
- each utterance is transcribed once through `ASR_MODEL_PATH`
- only final transcript segments are shown in the live UI and persisted to meeting history
- summary generation and meeting QA remain grounded on persisted final transcript segments
```

Replace the Docker note:

```md
# 1. 下载 ASR 模型（首次需要）
pip install huggingface_hub
huggingface-cli download Qwen/Qwen3-ASR-1.7B --local-dir ~/whisper-models/Qwen3-ASR-1.7B
```

Update `docs/reports/PIPELINE_MERMAID_DIAGRAMS.md` so diagrams no longer depict a preview service or preview websocket branch.

- [ ] **Step 2: Run a grep sweep to verify preview runtime references are gone**

Run:

```bash
rg -n "preview_asr|preview_enabled|live_preview|preview_task|last_non_empty_preview_text|PREVIEW_ASR_|WEBRTC_VAD_PREVIEW_|createLiveTranscriptState|data-live-segment-id|test_live_transcript_state" \
  config.py server_app.py server.py http_endpoints.py ws_handler.py asr.py asr_types.py vad.py session.py static tests README.md .env.example
```

Expected:

- no matches in runtime code, runtime tests, README, or `.env.example`
- only historical references may remain under archived plan/spec/report files outside the searched paths

- [ ] **Step 3: Run the Python regression sweep**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_server_endpoints \
  tests.test_server_websocket \
  tests.test_realtime_transcriber \
  tests.test_upload \
  -v
```

Expected:

- PASS
- no preview-lane assertions or imports remain in the exercised suite

- [ ] **Step 4: Run the Node regression sweep**

Run:

```bash
node --test \
  tests/test_final_transcript_store.mjs \
  tests/test_ui_formatters.mjs \
  tests/test_transcription_options.mjs \
  tests/test_asr_prompt_state.mjs
```

Expected:

- PASS

- [ ] **Step 5: Commit**

```bash
git add .env.example README.md docs/reports/PIPELINE_MERMAID_DIAGRAMS.md
git commit -m "docs: document single-track realtime ASR"
```

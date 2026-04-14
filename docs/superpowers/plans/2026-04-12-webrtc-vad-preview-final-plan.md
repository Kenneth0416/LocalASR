# WebRTC VAD Preview/Final Realtime Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fixed-second realtime chunking with WebRTC VAD endpointing, emit low-latency preview revisions from Qwen3-ASR 0.6B, commit final transcript segments from Qwen3-ASR 1.7B, and keep preview/final updates tied to the same `segment_id`.

**Architecture:** Introduce a dedicated realtime orchestration layer that is separate from the existing semantic chunker: `config.py` owns `WebRTCVADConfig` and preview/final ASR runtime settings, `asr.py` owns a thin `PreviewFinalASRRouter` plus a new `WebRTCVADMeetingTranscriber`, `server.py` owns dual-ASR lifecycle wiring and preview/final emit paths, and the frontend switches from append-only transcript rendering to `segment_id`-based upsert. Final transcript ordering is preserved by utterance order, not inference completion order.

**Tech Stack:** FastAPI, asyncio, Python stdlib, `webrtcvad-wheels==2.0.14`, Qwen3-ASR via existing `ASRService`, browser Web Audio worklet, Node test runner, Python `unittest`

**Workspace Note:** This directory is not currently a git repository. Replace commit steps with “review changed files” checkpoints until git is initialized.

---

## Planned File Changes

- Modify: `requirements.txt` — add WebRTC VAD binding
- Modify: `pyproject.toml` — add WebRTC VAD dependency for packaged installs
- Modify: `config.py` — add preview ASR config and realtime VAD config loaders
- Modify: `.env.example` — add `PREVIEW_ASR_*` and `WEBRTC_VAD_*` examples
- Modify: `README.md` — document preview/final split and new tuning variables
- Modify: `asr.py` — add lightweight router result, dual-model router, utterance state, and `WebRTCVADMeetingTranscriber`
- Modify: `server.py` — wire dual ASR services, new realtime transcriber factory, preview/final emit helpers, readiness payload updates
- Modify: `session.py` — add in-memory live preview cache helpers
- Create: `static/live-transcript-state.mjs` — pure `segment_id`/`revision` upsert helper
- Modify: `static/app.js` — use upsert-based live transcript rendering instead of append-only
- Modify: `static/styles.css` — distinguish preview/live rows from finalized rows
- Modify: `tests/test_server_endpoints.py` — startup/readiness dual-ASR coverage
- Modify: `tests/test_realtime_transcriber.py` — replace fixed-chunk assumptions with WebRTC VAD utterance tests
- Modify: `tests/test_server_websocket.py` — preview/final websocket behavior
- Create: `tests/test_live_transcript_state.mjs` — frontend `segment_id` upsert tests

## Task 1: Add Dependency and Runtime Configuration Scaffolding

**Files:**
- Modify: `requirements.txt`
- Modify: `pyproject.toml`
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_server_endpoints.py`

- [ ] **Step 1: Add failing server endpoint tests for preview/final runtime fields**

Append these tests to `tests/test_server_endpoints.py`:

```python
    def test_startup_event_initializes_preview_and_final_asr_services(self):
        fake_final_asr = make_fake_asr(initialized=False)
        fake_preview_asr = make_fake_asr(initialized=False)
        fake_runtime_snapshot = make_runtime_snapshot("startup-dual")

        with patch.object(server, "asr_service", fake_final_asr), \
             patch.object(server, "preview_asr_service", fake_preview_asr), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "refresh_runtime_snapshot", return_value=fake_runtime_snapshot, create=True):
            with TestClient(server.app):
                pass

        fake_final_asr.initialize.assert_awaited_once()
        fake_preview_asr.initialize.assert_awaited_once()

    def test_health_endpoint_surfaces_preview_and_final_asr_metadata(self):
        fake_final_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)
        fake_final_asr.initialization_state = lambda: "ready"
        fake_final_asr.initialization_error = lambda: None
        fake_preview_asr.initialization_state = lambda: "ready"
        fake_preview_asr.initialization_error = lambda: None
        fake_runtime_snapshot = make_runtime_snapshot("health-dual")

        with patch.object(server, "asr_service", fake_final_asr), \
             patch.object(server, "preview_asr_service", fake_preview_asr), \
             patch.object(server, "refresh_runtime_snapshot", return_value=fake_runtime_snapshot, create=True):
            with TestClient(server.app) as client:
                response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["preview_asr_state"], "ready")
        self.assertEqual(payload["final_asr_state"], "ready")
        self.assertTrue(payload["preview_enabled"])
        self.assertIn("preview_asr_model", payload)
        self.assertIn("final_asr_model", payload)
```

- [ ] **Step 2: Run the endpoint tests to verify they fail**

Run:

```bash
venv/bin/python -m unittest tests.test_server_endpoints.ServerEndpointTests.test_startup_event_initializes_preview_and_final_asr_services tests.test_server_endpoints.ServerEndpointTests.test_health_endpoint_surfaces_preview_and_final_asr_metadata -v
```

Expected:

- `AttributeError` for `preview_asr_service`
- or missing `preview_asr_*`/`final_asr_*` fields in health payload

- [ ] **Step 3: Add the WebRTC VAD dependency**

Update `requirements.txt` to include:

```text
webrtcvad-wheels==2.0.14
```

Update `pyproject.toml` dependencies to include:

```toml
  "webrtcvad-wheels==2.0.14",
```

- [ ] **Step 4: Add preview/final config loaders and realtime VAD config**

Add this dataclass and loader support to `config.py`:

```python
@dataclass
class WebRTCVADConfig:
    frame_ms: int = 20
    vad_aggressiveness: int = 2
    enter_speech_frames: int = 2
    endpoint_silence_frames: int = 18
    preview_interval_sec: float = 1.2
    min_preview_audio_sec: float = 0.8
    max_utterance_sec: float = 18.0
    pre_roll_sec: float = 0.2
    min_final_audio_sec: float = 0.1

    def __post_init__(self):
        self.frame_ms = int(self.frame_ms)
        self.vad_aggressiveness = int(self.vad_aggressiveness)
        self.enter_speech_frames = int(self.enter_speech_frames)
        self.endpoint_silence_frames = int(self.endpoint_silence_frames)
        self.preview_interval_sec = float(self.preview_interval_sec)
        self.min_preview_audio_sec = float(self.min_preview_audio_sec)
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
        if self.preview_interval_sec <= 0:
            raise ValueError("preview_interval_sec must be > 0")
        if self.min_preview_audio_sec < 0:
            raise ValueError("min_preview_audio_sec must be >= 0")
        if self.max_utterance_sec <= 0:
            raise ValueError("max_utterance_sec must be > 0")
        if self.pre_roll_sec < 0:
            raise ValueError("pre_roll_sec must be >= 0")
        if self.min_final_audio_sec < 0:
            raise ValueError("min_final_audio_sec must be >= 0")


def _build_final_asr_config() -> ASRConfig:
    return ASRConfig(
        model_path=_env("ASR_MODEL_PATH", _env("FINAL_ASR_MODEL_PATH", os.path.expanduser("~/whisper-models/Qwen3-ASR-1.7B"))),
        aligner_path=_env("ASR_ALIGNER_PATH", _env("FINAL_ASR_ALIGNER_PATH", "")),
        aligner_backend=_env("ASR_ALIGNER_BACKEND", _env("FINAL_ASR_ALIGNER_BACKEND", "qwen3alignment")),
        language=_env("ASR_LANGUAGE", _env("FINAL_ASR_LANGUAGE", "")),
        device=_env("ASR_DEVICE", _env("FINAL_ASR_DEVICE", "auto")),
        init_timeout_sec=float(_env("ASR_INIT_TIMEOUT_SEC", _env("FINAL_ASR_INIT_TIMEOUT_SEC", "240"))),
        max_inference_batch_size=int(_env("ASR_MAX_INFERENCE_BATCH_SIZE", _env("FINAL_ASR_MAX_INFERENCE_BATCH_SIZE", "32"))),
        max_new_tokens=int(_env("ASR_MAX_NEW_TOKENS", _env("FINAL_ASR_MAX_NEW_TOKENS", "256"))),
        attn_implementation=_env("ASR_ATTN_IMPLEMENTATION", _env("FINAL_ASR_ATTN_IMPLEMENTATION", "auto")),
        sample_rate=int(_env("ASR_SAMPLE_RATE", _env("FINAL_ASR_SAMPLE_RATE", "16000"))),
    )


def _build_preview_asr_config(final_config: ASRConfig) -> ASRConfig:
    return ASRConfig(
        model_path=_env("PREVIEW_ASR_MODEL_PATH", os.path.expanduser("~/whisper-models/Qwen3-ASR-0.6B")),
        aligner_path="",
        aligner_backend=final_config.aligner_backend,
        language=_env("PREVIEW_ASR_LANGUAGE", final_config.language),
        device=_env("PREVIEW_ASR_DEVICE", final_config.device),
        init_timeout_sec=float(_env("PREVIEW_ASR_INIT_TIMEOUT_SEC", str(final_config.init_timeout_sec))),
        max_inference_batch_size=int(_env("PREVIEW_ASR_MAX_INFERENCE_BATCH_SIZE", "8")),
        max_new_tokens=int(_env("PREVIEW_ASR_MAX_NEW_TOKENS", "128")),
        attn_implementation=_env("PREVIEW_ASR_ATTN_IMPLEMENTATION", final_config.attn_implementation),
        sample_rate=final_config.sample_rate,
    )


def _build_webrtc_vad_config() -> WebRTCVADConfig:
    return WebRTCVADConfig(
        frame_ms=int(_env("WEBRTC_VAD_FRAME_MS", "20")),
        vad_aggressiveness=int(_env("WEBRTC_VAD_AGGRESSIVENESS", "2")),
        enter_speech_frames=int(_env("WEBRTC_VAD_ENTER_SPEECH_FRAMES", "2")),
        endpoint_silence_frames=int(_env("WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES", "18")),
        preview_interval_sec=float(_env("WEBRTC_VAD_PREVIEW_INTERVAL_SEC", "1.2")),
        min_preview_audio_sec=float(_env("WEBRTC_VAD_MIN_PREVIEW_AUDIO_SEC", "0.8")),
        max_utterance_sec=float(_env("WEBRTC_VAD_MAX_UTTERANCE_SEC", "18.0")),
        pre_roll_sec=float(_env("WEBRTC_VAD_PRE_ROLL_SEC", "0.2")),
        min_final_audio_sec=float(_env("WEBRTC_VAD_MIN_FINAL_AUDIO_SEC", "0.1")),
    )
```

Change the `load_config()` signature to:

```python
def load_config() -> tuple[LLMConfig, ASRConfig, ASRConfig, WebRTCVADConfig, ServerConfig, MeetingConfig]:
    local_only = _env_bool("LOCAL_ONLY_MODE", True)
    openai_base_url = os.getenv("OPENAI_BASE_URL", "")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    provider = "ollama"
    if openai_base_url:
        provider = "openai"
    elif not local_only and openai_api_key:
        provider = "openai"

    llm = LLMConfig(
        provider=provider,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:9b"),
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        local_only=local_only,
    )
    final_asr = _build_final_asr_config()
    preview_asr = _build_preview_asr_config(final_asr)
    realtime_vad = _build_webrtc_vad_config()
    server = ServerConfig(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8800")),
        cors_origins=_env_list("CORS_ORIGINS", ["http://127.0.0.1:8800", "http://localhost:8800"]),
        cors_allow_credentials=_env_bool("CORS_ALLOW_CREDENTIALS", False),
        expose_session_api=_env_bool("EXPOSE_SESSION_API", False),
        audio_queue_maxsize=int(os.getenv("AUDIO_QUEUE_MAXSIZE", "32")),
        recordings_dir=os.getenv("MEETING_RECORDINGS_DIR", "recordings"),
        database_path=os.getenv("MEETING_DB_PATH", "data/meeting_realtime_voice.sqlite3"),
        history_limit=int(os.getenv("MEETING_HISTORY_LIMIT", "100")),
    )
    meeting = MeetingConfig(
        max_context_messages=int(os.getenv("MAX_CONTEXT_MESSAGES", "50")),
        max_context_chars=int(os.getenv("MAX_CONTEXT_CHARS", "50000")),
        summary_interval_turns=int(os.getenv("SUMMARY_INTERVAL_TURNS", "30")),
    )
    return llm, final_asr, preview_asr, realtime_vad, server, meeting
```

- [ ] **Step 5: Wire dual ASR services and runtime payload fields in `server.py`**

Update the config bootstrap and server globals near the top of `server.py`:

```python
llm_config, asr_config, preview_asr_config, realtime_vad_config, server_config, meeting_config = load_config()

asr_service = ASRService(asr_config)
preview_asr_service = ASRService(preview_asr_config)
```

Add a helper and aggregate ASR status:

```python
def _service_status(service) -> dict[str, str | None]:
    state_getter = getattr(service, "initialization_state", None)
    error_getter = getattr(service, "initialization_error", None)
    state = state_getter() if callable(state_getter) else ("ready" if getattr(service, "_initialized", False) else "idle")
    error = error_getter() if callable(error_getter) else None
    return {"state": state, "error": error}


def get_asr_status() -> dict[str, object]:
    final_status = _service_status(asr_service)
    preview_status = _service_status(preview_asr_service)
    aggregate_state = final_status["state"]
    if aggregate_state == "ready" and preview_status["state"] != "ready":
        aggregate_state = "degraded"
    return {
        "state": aggregate_state,
        "error": final_status["error"],
        "final": final_status,
        "preview": preview_status,
        "preview_enabled": preview_status["state"] == "ready",
    }
```

Update startup and shutdown:

```python
@app.on_event("startup")
async def on_startup():
    for warning in get_runtime_warnings():
        logger.warning(warning)
    for service in (asr_service, preview_asr_service):
        initialize = getattr(service, "initialize", None)
        if callable(initialize):
            await initialize()
    llm_status = await asyncio.to_thread(get_llm_status)
    snapshot = refresh_runtime_snapshot(llm_status=llm_status)
    for warning in snapshot.warnings:
        logger.warning(warning)


@app.on_event("shutdown")
async def on_shutdown():
    for service in (preview_asr_service, asr_service):
        shutdown = getattr(service, "shutdown", None)
        if callable(shutdown):
            await shutdown()
```

Update health payload fields:

```python
    return {
        "status": "ok",
        "app": "meeting-realtime-voice",
        "python_version": sys.version.split()[0],
        "runtime_warnings": get_runtime_warnings(),
        "runtime": snapshot,
        "asr_model": str(asr_config.model_path),
        "preview_asr_model": str(preview_asr_config.model_path),
        "final_asr_model": str(asr_config.model_path),
        "preview_asr_state": asr_status["preview"]["state"],
        "final_asr_state": asr_status["final"]["state"],
        "preview_enabled": asr_status["preview_enabled"],
        "asr_ready": asr_status["final"]["state"] == "ready",
        "asr_state": asr_status["state"],
        "asr_error": asr_status["error"],
        "asr_device": asr_config.device,
        "llm_provider": llm_config.provider,
        "llm_model": llm_config.model_name,
        "active_sessions": len(session_manager.sessions),
        "audio_queue_maxsize": server_config.audio_queue_maxsize,
    }
```

Update the websocket `ready` payload similarly.

- [ ] **Step 6: Add new `.env.example` and README entries**

Append to `.env.example`:

```env
# Preview ASR runtime.
PREVIEW_ASR_MODEL_PATH=~/whisper-models/Qwen3-ASR-0.6B
PREVIEW_ASR_DEVICE=auto
PREVIEW_ASR_MAX_INFERENCE_BATCH_SIZE=8
PREVIEW_ASR_MAX_NEW_TOKENS=128

# WebRTC VAD realtime tuning.
WEBRTC_VAD_FRAME_MS=20
WEBRTC_VAD_AGGRESSIVENESS=2
WEBRTC_VAD_ENTER_SPEECH_FRAMES=2
WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES=18
WEBRTC_VAD_PREVIEW_INTERVAL_SEC=1.2
WEBRTC_VAD_MIN_PREVIEW_AUDIO_SEC=0.8
WEBRTC_VAD_MAX_UTTERANCE_SEC=18.0
WEBRTC_VAD_PRE_ROLL_SEC=0.2
WEBRTC_VAD_MIN_FINAL_AUDIO_SEC=0.1
```

Add this note to `README.md` under ASR:

```md
### Realtime Preview/Final Split

- realtime preview uses `PREVIEW_ASR_MODEL_PATH` and is intended for low-latency partial text
- finalized transcript segments use `ASR_MODEL_PATH`
- preview output is not persisted to meeting history
- finalized output is the only source for meeting summary and QA context
```

- [ ] **Step 7: Run the endpoint tests to verify they pass**

Run:

```bash
venv/bin/python -m unittest tests.test_server_endpoints.ServerEndpointTests.test_startup_event_initializes_preview_and_final_asr_services tests.test_server_endpoints.ServerEndpointTests.test_health_endpoint_surfaces_preview_and_final_asr_metadata -v
```

Expected:

- both tests PASS

- [ ] **Step 8: Review changed files**

Run:

```bash
rg -n "preview_asr_service|WebRTCVADConfig|webrtcvad-wheels|preview_asr_model|final_asr_model" config.py server.py requirements.txt pyproject.toml .env.example README.md
```

Expected:

- every new runtime/config entry appears exactly where intended

## Task 2: Implement the Router and WebRTC VAD Realtime Transcriber

**Files:**
- Modify: `asr.py`
- Modify: `tests/test_realtime_transcriber.py`

- [ ] **Step 1: Replace the fixed-chunk realtime tests with VAD utterance tests**

Replace the current `RealtimeMeetingTranscriberTests` block in `tests/test_realtime_transcriber.py` with:

```python
class FakeRouter:
    def __init__(self, preview_results=None, final_results=None):
        self.preview_results = iter(preview_results or [])
        self.final_results = iter(final_results or [])
        self.preview_calls = []
        self.final_calls = []

    async def transcribe_preview(self, audio_tuple):
        self.preview_calls.append(audio_tuple)
        return next(self.preview_results)

    async def transcribe_final(self, audio_tuple):
        self.final_calls.append(audio_tuple)
        return next(self.final_results)


class FakeVAD:
    def __init__(self, decisions):
        self._decisions = iter(decisions)

    def is_speech(self, _pcm_chunk, _sample_rate):
        return next(self._decisions)


class WebRTCVADMeetingTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_preview_revisions_then_final_with_same_segment_id(self):
        router = FakeRouter(
            preview_results=[
                LiteASRResult(text="我们这周先把接口", duration_sec=1.0),
                LiteASRResult(text="我们这周先把接口和回归", duration_sec=2.2),
            ],
            final_results=[
                LiteASRResult(text="我们这周先把接口和回归走完。", duration_sec=2.8),
            ],
        )
        decisions = [True] * 12 + [False] * 20
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=10,
                preview_interval_sec=1.0,
                min_preview_audio_sec=0.8,
                max_utterance_sec=8.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )

        events = []
        for _ in range(len(decisions)):
            events.extend(await transcriber.push_frame(make_pcm((0.02, 1800)), sample_count=320))

        previews = [event for event in events if not event.is_final]
        finals = [event for event in events if event.is_final]

        self.assertEqual([event.segment_id for event in previews], [1, 1])
        self.assertEqual([event.revision for event in previews], [1, 2])
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].segment_id, 1)
        self.assertEqual(finals[0].revision, 3)
        self.assertEqual(finals[0].text, "我们这周先把接口和回归走完。")

    async def test_pending_final_does_not_block_next_utterance(self):
        preview = LiteASRResult(text="第一句预览", duration_sec=0.8)
        final_1 = asyncio.Future()
        final_2 = asyncio.Future()
        final_1.set_result(LiteASRResult(text="第一句定稿。", duration_sec=1.5))
        final_2.set_result(LiteASRResult(text="第二句定稿。", duration_sec=0.9))

        class Router:
            async def transcribe_preview(self, _audio_tuple):
                return preview
            async def transcribe_final(self, _audio_tuple):
                if not hasattr(self, "called"):
                    self.called = 1
                    return await final_1
                return await final_2

        decisions = [True] * 6 + [False] * 12 + [True] * 6 + [False] * 12
        transcriber = WebRTCVADMeetingTranscriber(
            router=Router(),
            config=WebRTCVADConfig(frame_ms=20, enter_speech_frames=2, endpoint_silence_frames=8, preview_interval_sec=0.5, min_preview_audio_sec=0.4, max_utterance_sec=5.0),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )

        events = []
        for _ in range(len(decisions)):
            events.extend(await transcriber.push_frame(make_pcm((0.02, 1800)), sample_count=320))
        events.extend(await transcriber.flush(reason="flush"))

        finals = [event for event in events if event.is_final]
        self.assertEqual([event.segment_id for event in finals], [1, 2])
        self.assertEqual([event.text for event in finals], ["第一句定稿。", "第二句定稿。"])

    async def test_final_falls_back_to_last_non_empty_preview_text(self):
        router = FakeRouter(
            preview_results=[LiteASRResult(text="还有半句", duration_sec=0.8)],
            final_results=[LiteASRResult(text="", duration_sec=1.0)],
        )
        decisions = [True] * 8 + [False] * 14
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            config=WebRTCVADConfig(frame_ms=20, enter_speech_frames=2, endpoint_silence_frames=8, preview_interval_sec=0.5, min_preview_audio_sec=0.4, max_utterance_sec=6.0),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )

        events = []
        for _ in range(len(decisions)):
            events.extend(await transcriber.push_frame(make_pcm((0.02, 1800)), sample_count=320))

        finals = [event for event in events if event.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "还有半句")
        self.assertEqual(finals[0].cut_reason, "final_fallback")
```

- [ ] **Step 2: Run the realtime transcriber tests to verify they fail**

Run:

```bash
venv/bin/python -m unittest tests.test_realtime_transcriber -v
```

Expected:

- import error for `LiteASRResult` or `WebRTCVADMeetingTranscriber`
- or constructor/signature mismatch

- [ ] **Step 3: Add a lightweight router result and utterance state to `asr.py`**

Add these definitions near the existing realtime dataclasses:

```python
@dataclass(frozen=True)
class LiteASRResult:
    text: str
    duration_sec: float
    language: str | None = None
    confidence: float | None = None


@dataclass
class UtteranceState:
    segment_id: int
    revision: int
    start_sample: int
    end_sample: int | None
    last_speech_sample: int
    pcm_buffer: bytearray
    first_preview_emitted: bool = False
    last_preview_sample: int = 0
    sealed: bool = False
    final_task: asyncio.Task | None = None
    preview_task: asyncio.Task | None = None
    last_non_empty_preview_text: str = ""
```

Add a thin router:

```python
class PreviewFinalASRRouter:
    def __init__(self, preview_service: "ASRService", final_service: "ASRService", *, language_getter, context_getter):
        self._preview_service = preview_service
        self._final_service = final_service
        self._language_getter = language_getter
        self._context_getter = context_getter

    async def transcribe_preview(self, audio_tuple) -> LiteASRResult:
        result = await self._preview_service.transcribe_wav(
            audio_tuple,
            language=self._language_getter(),
            context=self._context_getter(),
        )
        return LiteASRResult(
            text=(result.text or "").strip(),
            duration_sec=float(result.audio_duration or 0.0),
        )

    async def transcribe_final(self, audio_tuple) -> LiteASRResult:
        result = await self._final_service.transcribe_wav(
            audio_tuple,
            language=self._language_getter(),
            context=self._context_getter(),
        )
        return LiteASRResult(
            text=(result.text or "").strip(),
            duration_sec=float(result.audio_duration or 0.0),
        )
```

- [ ] **Step 4: Implement the new transcriber with injectable VAD**

Add this class to `asr.py`:

```python
class WebRTCVADMeetingTranscriber:
    def __init__(self, router, sample_rate: int = 16000, config: WebRTCVADConfig | None = None, vad_factory=None):
        import webrtcvad

        self.router = router
        self.sample_rate = sample_rate
        self.config = config or WebRTCVADConfig()
        self._bytes_per_sample = 2
        self._frame_samples = int(self.sample_rate * (self.config.frame_ms / 1000.0))
        self._frame_bytes = self._frame_samples * self._bytes_per_sample
        self._vad = (vad_factory or webrtcvad.Vad)(self.config.vad_aggressiveness)
        self._sample_cursor = 0
        self._next_segment_id = 1
        self._next_final_to_emit = 1
        self._active: UtteranceState | None = None
        self._pending_finals: dict[int, RealtimeTranscriptEvent | None] = {}
        self._speech_run = 0
        self._silence_run = 0

    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for offset in range(0, len(pcm_chunk), self._frame_bytes):
            frame = pcm_chunk[offset:offset + self._frame_bytes]
            if len(frame) != self._frame_bytes:
                break
            events.extend(await self.push_frame(frame, sample_count=self._frame_samples))
        return events

    async def push_frame(self, pcm_chunk: bytes, sample_count: int | None = None) -> list[RealtimeTranscriptEvent]:
        sample_count = sample_count or self._frame_samples
        events: list[RealtimeTranscriptEvent] = []
        is_speech = self._vad.is_speech(pcm_chunk, self.sample_rate)
        frame_start = self._sample_cursor
        frame_end = frame_start + sample_count
        self._sample_cursor = frame_end

        if is_speech:
            self._speech_run += 1
            self._silence_run = 0
        else:
            self._speech_run = 0
            self._silence_run += 1

        if self._active is None and is_speech and self._speech_run >= self.config.enter_speech_frames:
            self._active = UtteranceState(
                segment_id=self._next_segment_id,
                revision=0,
                start_sample=frame_start,
                end_sample=None,
                last_speech_sample=frame_end,
                pcm_buffer=bytearray(),
            )
            self._next_segment_id += 1

        if self._active is not None:
            self._active.pcm_buffer.extend(pcm_chunk)
            if is_speech:
                self._active.last_speech_sample = frame_end

            events.extend(await self._maybe_emit_preview())

            utterance_duration_sec = len(self._active.pcm_buffer) / (self.sample_rate * self._bytes_per_sample)
            if self._silence_run >= self.config.endpoint_silence_frames:
                self._seal_active("endpoint")
            elif utterance_duration_sec >= self.config.max_utterance_sec:
                self._seal_active("max_duration")

        events.extend(await self._drain_finished_tasks())
        return events

    async def flush(self, reason: str = "flush") -> list[RealtimeTranscriptEvent]:
        if self._active is not None:
            self._seal_active(reason)
        return await self._await_all_pending(reason)
```

Add helper behavior under the class:

```python
    async def _maybe_emit_preview(self) -> list[RealtimeTranscriptEvent]:
        state = self._active
        if state is None or state.sealed:
            return []
        if state.preview_task is not None and not state.preview_task.done():
            return []

        current_samples = len(state.pcm_buffer) // self._bytes_per_sample
        current_sec = current_samples / self.sample_rate
        grown_samples = current_samples - state.last_preview_sample
        if current_sec < self.config.min_preview_audio_sec:
            return []
        if state.first_preview_emitted and (grown_samples / self.sample_rate) < self.config.preview_interval_sec:
            return []

        snapshot = bytes(state.pcm_buffer)
        next_revision = state.revision + 1
        segment_id = state.segment_id
        start_time = state.start_sample / self.sample_rate
        end_time = (state.start_sample + current_samples) / self.sample_rate

        async def run_preview():
            result = await self.router.transcribe_preview(pcm16le_to_audio_tuple(snapshot, self.sample_rate))
            return segment_id, next_revision, start_time, end_time, result

        state.preview_task = asyncio.create_task(run_preview(), name=f"preview-{segment_id}-{next_revision}")
        return []

    def _seal_active(self, reason: str) -> None:
        state = self._active
        if state is None or state.sealed:
            return
        state.sealed = True
        state.end_sample = state.start_sample + (len(state.pcm_buffer) // self._bytes_per_sample)
        snapshot = bytes(state.pcm_buffer)
        segment_id = state.segment_id
        revision = state.revision + 1
        start_time = state.start_sample / self.sample_rate
        end_time = state.end_sample / self.sample_rate
        fallback_text = state.last_non_empty_preview_text

        async def run_final():
            result = await self.router.transcribe_final(pcm16le_to_audio_tuple(snapshot, self.sample_rate))
            text = (result.text or "").strip() or fallback_text
            cut_reason = reason if (result.text or "").strip() else ("final_fallback" if fallback_text else reason)
            if not text:
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
                cut_reason=cut_reason,
            )

        state.final_task = asyncio.create_task(run_final(), name=f"final-{segment_id}")
        self._active = None
```

Add task-drain logic:

```python
    async def _drain_finished_tasks(self) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []

        for state in [self._active] if self._active is not None else []:
            if state.preview_task is not None and state.preview_task.done():
                segment_id, revision, start_time, end_time, result = await state.preview_task
                state.preview_task = None
                if result.text:
                    state.revision = revision
                    state.first_preview_emitted = True
                    state.last_preview_sample = len(state.pcm_buffer) // self._bytes_per_sample
                    state.last_non_empty_preview_text = result.text
                    events.append(RealtimeTranscriptEvent(
                        event_type="preview",
                        text=result.text,
                        start_time=start_time,
                        end_time=end_time,
                        processing_time=0.0,
                        segment_id=segment_id,
                        revision=revision,
                        is_final=False,
                        cut_reason="preview_tick",
                    ))

        # collect finished finals into ordered buffer
        # emit only in segment order
        return events + await self._emit_ready_finals_in_order()
```

Store outstanding final tasks in `self._final_tasks: dict[int, asyncio.Task]`, await any finished tasks inside `_drain_finished_tasks()`, copy their resolved events into `self._pending_finals`, and have `_emit_ready_finals_in_order()` pop only `self._next_final_to_emit`, then increment that counter until the next segment id is missing.

- [ ] **Step 5: Run the realtime transcriber tests to verify they pass**

Run:

```bash
venv/bin/python -m unittest tests.test_realtime_transcriber -v
```

Expected:

- all realtime transcriber tests PASS

- [ ] **Step 6: Review the transcriber surface**

Run:

```bash
rg -n "LiteASRResult|UtteranceState|PreviewFinalASRRouter|WebRTCVADMeetingTranscriber|final_fallback|preview_tick" asr.py tests/test_realtime_transcriber.py
```

Expected:

- one router, one utterance state, one WebRTC VAD transcriber, and explicit fallback handling

## Task 3: Wire Server Preview/Final Emit Paths and Session Live Preview Cache

**Files:**
- Modify: `server.py`
- Modify: `session.py`
- Modify: `tests/test_server_websocket.py`

- [ ] **Step 1: Add failing websocket tests for preview/final behavior**

Add the helper near the top of `tests/test_server_websocket.py`, then append the test method inside the existing `ServerWebSocketTests` class:

```python
class FakeRealtimeTranscriber:
    def __init__(self, push_events=None, flush_events=None):
        self._push_events = list(push_events or [])
        self._flush_events = list(flush_events or [])

    async def push_frame(self, _pcm_chunk, sample_count=None):
        if self._push_events:
            return self._push_events.pop(0)
        return []

    async def flush(self, reason="flush"):
        return list(self._flush_events)

```

Then append this method inside the existing `ServerWebSocketTests` class:

```python
    def test_preview_events_do_not_persist_but_final_event_does(self):
        preview = server.RealtimeTranscriptEvent(
            event_type="preview",
            text="我们这周先把接口",
            start_time=0.0,
            end_time=1.0,
            processing_time=0.01,
            segment_id=1,
            revision=1,
            is_final=False,
            cut_reason="preview_tick",
        )
        final = server.RealtimeTranscriptEvent(
            event_type="final",
            text="我们这周先把接口走完。",
            start_time=0.0,
            end_time=1.6,
            processing_time=0.02,
            segment_id=1,
            revision=2,
            is_final=True,
            cut_reason="endpoint",
        )

        fake_transcriber = FakeRealtimeTranscriber(
            push_events=[[preview], []],
            flush_events=[final],
        )

        with TemporaryDirectory() as tmpdir:
            store = MeetingStore(Path(tmpdir) / "meetings.sqlite3")
            with patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber), \
                 patch.object(server, "meeting_store", store), \
                 patch.object(server.session_manager, "store", store):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        websocket.receive_json()
                        websocket.send_text(json.dumps({"type": "start"}))
                        receive_until_type(websocket, "start_ack")

                        websocket.send_bytes(make_pcm((0.02, 1800)))
                        preview_msg = receive_until_type(websocket, "transcript")
                        self.assertEqual(preview_msg["segment"]["segment_id"], 1)
                        self.assertEqual(preview_msg["segment"]["revision"], 1)
                        self.assertFalse(preview_msg["segment"]["is_final"])
                        self.assertEqual(preview_msg["segment"]["id"], "")

                        websocket.send_text(json.dumps({"type": "stop"}))
                        final_msg = receive_until_type(websocket, "transcript")
                        self.assertTrue(final_msg["segment"]["is_final"])
                        self.assertNotEqual(final_msg["segment"]["id"], "")

                        stopped = receive_until_type(websocket, "stopped")
                        self.assertEqual(stopped["type"], "stopped")

            meetings = store.list_meetings(limit=1)
            self.assertEqual(len(meetings), 1)
            meeting = store.get_meeting(meetings[0]["session_id"])
            self.assertEqual(len(meeting["transcript"]), 1)
            self.assertEqual(meeting["transcript"][0]["text"], "我们这周先把接口走完。")
```

- [ ] **Step 2: Run the websocket test to verify it fails**

Run:

```bash
venv/bin/python -m unittest tests.test_server_websocket.ServerWebSocketTests.test_preview_events_do_not_persist_but_final_event_does -v
```

Expected:

- `AttributeError` for `build_realtime_transcriber`
- or preview event gets persisted because only one emit path exists

- [ ] **Step 3: Add live preview helpers to `session.py`**

Add an in-memory preview cache:

```python
        self.live_preview_segments: dict[int, dict[str, Any]] = {}
```

Add helper methods:

```python
    def upsert_live_preview_segment(self, *, segment_id: int, revision: int, text: str, start_time: float, end_time: float) -> None:
        current = self.live_preview_segments.get(segment_id)
        if current and revision <= int(current["revision"]):
            return
        self.live_preview_segments[segment_id] = {
            "segment_id": segment_id,
            "revision": revision,
            "text": text,
            "start_time": start_time,
            "end_time": end_time,
        }

    def clear_live_preview_segment(self, segment_id: int) -> None:
        self.live_preview_segments.pop(segment_id, None)
```

- [ ] **Step 4: Add a realtime transcriber factory to `server.py`**

Add this function:

```python
def build_realtime_transcriber(session_state: dict):
    router = PreviewFinalASRRouter(
        preview_service=preview_asr_service,
        final_service=asr_service,
        language_getter=lambda: session_state.get("language"),
        context_getter=lambda: session_state.get("asr_prompt"),
    )
    return WebRTCVADMeetingTranscriber(
        router=router,
        sample_rate=asr_config.sample_rate,
        config=realtime_vad_config,
    )
```

Use it in `audio_worker()` instead of instantiating `SemanticMeetingTranscriber` directly.

- [ ] **Step 5: Split preview and final emit helpers**

Replace the current single helper with:

```python
async def emit_preview_segment(session: MeetingSession, event: RealtimeTranscriptEvent, websocket: WebSocket):
    text = event.text.strip()
    if not text:
        return

    session.upsert_live_preview_segment(
        segment_id=event.segment_id,
        revision=event.revision,
        text=text,
        start_time=event.start_time,
        end_time=event.end_time,
    )
    await websocket.send_json({
        "type": "transcript",
        "segment": {
            "id": "",
            "segment_id": event.segment_id,
            "revision": event.revision,
            "is_final": False,
            "cut_reason": event.cut_reason,
            "speaker": "发言人",
            "text": text,
            "start": event.start_time,
            "end": event.end_time,
        },
        "total_segments": len(session.transcript),
        "processing_time": event.processing_time,
    })


async def emit_final_segment(session: MeetingSession, event: RealtimeTranscriptEvent, websocket: WebSocket):
    text = event.text.strip()
    if not text:
        session.clear_live_preview_segment(event.segment_id)
        return

    session.clear_live_preview_segment(event.segment_id)
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

    if len(session.transcript) % meeting_config.summary_interval_turns == 0:
        asyncio.create_task(handle_summary_request(session, websocket))
```

In `audio_worker()`, route by `event.is_final` and iterate frame-by-frame:

```python
                if item.event_type == "audio":
                    for frame in item.frames:
                        audio_recorder.append_pcm(frame.pcm_chunk)
                        events = await transcriber.push_frame(frame.pcm_chunk, sample_count=frame.sample_count)
                        for event in events:
                            if event.is_final:
                                await emit_final_segment(session, event, websocket)
                            else:
                                await emit_preview_segment(session, event, websocket)
                            await websocket.send_json({
                                "type": "transcribe_done",
                                "processing_time": event.processing_time,
                                "phase": event.event_type,
                                "segment_id": event.segment_id,
                                "revision": event.revision,
                                "is_final": event.is_final,
                                "cut_reason": event.cut_reason,
                            })
                    continue
```

Keep `flush(reason=item.reason)` for `eos`.

- [ ] **Step 6: Run the websocket tests to verify they pass**

Run:

```bash
venv/bin/python -m unittest tests.test_server_websocket -v
```

Expected:

- websocket tests PASS with preview/final behavior

- [ ] **Step 7: Review the new server path**

Run:

```bash
rg -n "build_realtime_transcriber|emit_preview_segment|emit_final_segment|live_preview_segments|preview_enabled" server.py session.py tests/test_server_websocket.py
```

Expected:

- one explicit factory, one preview emit helper, one final emit helper, and session-local preview cache support

## Task 4: Replace Append-Only Frontend Transcript Rendering with `segment_id` Upsert

**Files:**
- Create: `static/live-transcript-state.mjs`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Create: `tests/test_live_transcript_state.mjs`

- [ ] **Step 1: Add failing frontend state tests**

Create `tests/test_live_transcript_state.mjs` with this content:

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';

import { createLiveTranscriptState } from '../static/live-transcript-state.mjs';

test('creates a live entry on first preview segment', () => {
    const state = createLiveTranscriptState();
    const result = state.apply({
        id: '',
        segment_id: 7,
        revision: 1,
        is_final: false,
        text: '我们这周先把接口',
        start: 0,
        end: 1,
    });

    assert.equal(result.changed, true);
    assert.equal(state.values().length, 1);
    assert.equal(state.values()[0].segmentId, '7');
    assert.equal(state.values()[0].persisted, false);
});

test('replaces an existing entry only when revision is newer', () => {
    const state = createLiveTranscriptState();
    state.apply({ id: '', segment_id: 7, revision: 2, is_final: false, text: '旧文本', start: 0, end: 1 });
    const stale = state.apply({ id: '', segment_id: 7, revision: 1, is_final: false, text: '更旧文本', start: 0, end: 1 });
    const fresh = state.apply({ id: '', segment_id: 7, revision: 3, is_final: false, text: '新文本', start: 0, end: 2 });

    assert.equal(stale.changed, false);
    assert.equal(fresh.changed, true);
    assert.equal(state.values()[0].text, '新文本');
    assert.equal(state.values()[0].revision, 3);
});

test('promotes a live entry to persisted on final segment', () => {
    const state = createLiveTranscriptState();
    state.apply({ id: '', segment_id: 7, revision: 1, is_final: false, text: '预览', start: 0, end: 1 });
    state.apply({ id: 'seg-db-1', segment_id: 7, revision: 2, is_final: true, text: '定稿。', start: 0, end: 1.5 });

    assert.equal(state.values()[0].persisted, true);
    assert.equal(state.values()[0].id, 'seg-db-1');
    assert.equal(state.values()[0].text, '定稿。');
});

test('still works when backend emits only final segments', () => {
    const state = createLiveTranscriptState();
    const result = state.apply({ id: 'seg-db-2', segment_id: 8, revision: 1, is_final: true, text: '只有定稿。', start: 2, end: 3 });

    assert.equal(result.changed, true);
    assert.equal(state.values()[0].persisted, true);
    assert.equal(state.values()[0].segmentId, '8');
});
```

- [ ] **Step 2: Run the frontend test to verify it fails**

Run:

```bash
node --test tests/test_live_transcript_state.mjs
```

Expected:

- module not found for `static/live-transcript-state.mjs`

- [ ] **Step 3: Implement the pure live transcript upsert helper**

Create `static/live-transcript-state.mjs` with this content:

```javascript
function normalizeSegment(input = {}) {
    return {
        id: String(input.id || ''),
        segmentId: String(input.segment_id ?? ''),
        revision: Number(input.revision || 0),
        isFinal: Boolean(input.is_final),
        persisted: Boolean(input.is_final && input.id),
        speaker: String(input.speaker || '发言人'),
        text: String(input.text || ''),
        start: Number(input.start ?? input.start_time ?? 0),
        end: Number(input.end ?? input.end_time ?? 0),
        cutReason: String(input.cut_reason || ''),
    };
}

export function createLiveTranscriptState() {
    const bySegmentId = new Map();

    return {
        apply(input) {
            const next = normalizeSegment(input);
            if (!next.segmentId) {
                return { changed: false, entry: null };
            }

            const current = bySegmentId.get(next.segmentId);
            if (current && next.revision <= current.revision) {
                return { changed: false, entry: current };
            }

            const merged = current
                ? { ...current, ...next, persisted: current.persisted || next.persisted }
                : next;

            bySegmentId.set(next.segmentId, merged);
            return { changed: true, entry: merged };
        },

        get(segmentId) {
            return bySegmentId.get(String(segmentId)) || null;
        },

        values() {
            return [...bySegmentId.values()].sort((a, b) => Number(a.segmentId) - Number(b.segmentId));
        },
    };
}
```

- [ ] **Step 4: Integrate the helper into `static/app.js`**

At the top of `static/app.js`, add:

```javascript
import { createLiveTranscriptState } from '/static/live-transcript-state.mjs';
```

Add this state member:

```javascript
    liveTranscriptState: createLiveTranscriptState(),
```

Replace `addTranscriptSegment()` with:

```javascript
function upsertTranscriptSegment(segment) {
    const result = state.liveTranscriptState.apply(segment);
    if (!result.changed || !result.entry) return;

    const empty = el.transcriptList.querySelector('.empty-state');
    if (empty) empty.remove();

    const existing = el.transcriptList.querySelector(`[data-live-segment-id="${result.entry.segmentId}"]`);
    const markup = buildTranscriptBlockMarkup(
        {
            id: result.entry.id || result.entry.segmentId,
            speaker: result.entry.speaker,
            text: result.entry.text,
            start: result.entry.start,
            end: result.entry.end,
        },
        {
            latest: true,
            editable: false,
            liveSegmentId: result.entry.segmentId,
            preview: !result.entry.persisted,
        },
    );

    const wrapper = document.createElement('div');
    wrapper.innerHTML = markup.trim();
    const nextNode = wrapper.firstElementChild;

    if (existing) {
        existing.replaceWith(nextNode);
    } else {
        el.transcriptList.appendChild(nextNode);
    }

    latestSegmentEl = nextNode.querySelector('.transcript-block');
    el.transcriptList.scrollTop = el.transcriptList.scrollHeight;
    updateTranscriptCount();
}
```

Update the websocket branch:

```javascript
        case 'transcript':
            upsertTranscriptSegment(msg.segment);
            if (msg.processing_time) {
                setProcessingStatus(msg.processing_time.toFixed(2) + 's');
            }
            break;
```

Update `buildTranscriptBlockMarkup()` to accept:

```javascript
    const previewClass = options.preview ? ' preview' : '';
    const liveId = options.liveSegmentId ? ` data-live-segment-id="${escapeHtml(String(options.liveSegmentId))}"` : '';
```

and render:

```javascript
            <div class="transcript-block${options.latest ? ' latest' : ''}${previewClass}" data-segment-id="${escapeHtml(segmentId)}"${liveId}>
```

- [ ] **Step 5: Add a lightweight preview style**

In `static/styles.css`, ensure preview rows remain visually distinct:

```css
.transcript-block.preview {
    border-color: rgba(255, 173, 51, 0.45);
    background: rgba(255, 173, 51, 0.08);
}

.transcript-block.preview .segment-speaker {
    color: #b86a00;
}
```

- [ ] **Step 6: Run the frontend tests to verify they pass**

Run:

```bash
node --test tests/test_live_transcript_state.mjs tests/test_ui_formatters.mjs tests/test_transcription_options.mjs
```

Expected:

- all Node tests PASS

- [ ] **Step 7: Review the frontend integration surface**

Run:

```bash
rg -n "createLiveTranscriptState|upsertTranscriptSegment|data-live-segment-id|preview" static/app.js static/live-transcript-state.mjs static/styles.css tests/test_live_transcript_state.mjs
```

Expected:

- one pure helper module and one upsert entry point in `app.js`

## Task 5: Full Verification and Documentation Sweep

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Review: `config.py`, `asr.py`, `server.py`, `session.py`, `static/app.js`, `static/live-transcript-state.mjs`

- [ ] **Step 1: Re-run the focused Python suites**

Run:

```bash
venv/bin/python -m unittest tests.test_realtime_transcriber tests.test_server_websocket tests.test_server_endpoints -v
```

Expected:

- all targeted Python tests PASS

- [ ] **Step 2: Re-run the focused Node suites**

Run:

```bash
node --test tests/test_live_transcript_state.mjs tests/test_ui_formatters.mjs tests/test_transcription_options.mjs
```

Expected:

- all targeted Node tests PASS

- [ ] **Step 3: Run the existing upload and ASR service regressions**

Run:

```bash
venv/bin/python -m unittest tests.test_upload tests.test_asr_service -v
```

Expected:

- upload and ASR service regressions PASS

- [ ] **Step 4: Review for final-only compatibility and preview isolation**

Run:

```bash
rg -n "session.transcript|live_preview_segments|preview_enabled|segment_id|is_final|revision" server.py session.py static/app.js asr.py
```

Expected:

- preview state appears only in transcriber/session live cache/frontend live store
- `session.transcript` remains final-only

- [ ] **Step 5: Review changed files instead of committing**

Run:

```bash
ls -1 config.py asr.py server.py session.py static/app.js static/live-transcript-state.mjs static/styles.css tests/test_realtime_transcriber.py tests/test_server_websocket.py tests/test_server_endpoints.py tests/test_live_transcript_state.mjs README.md .env.example requirements.txt pyproject.toml
```

Expected:

- all planned files are present

- [ ] **Step 6: Record the rollout note in `README.md`**

Add this rollout note:

```md
### Rollout Note

The realtime transcript UI is compatible with both:

- final-only realtime transcript events
- preview + final transcript events that share a stable `segment_id`

This allows staged backend rollout without breaking the frontend.
```

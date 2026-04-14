# Phase 0 Local Offline Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the current `localhost` single-user app stable, local-only by default, and verification-green on the supported Python `3.12` release runtime without expanding product scope.

**Architecture:** Add a small runtime diagnostics layer (`runtime_checks.py`) that computes local dependency and capability state, then wire that state into FastAPI startup, readiness payloads, websocket bootstrap payloads, and the frontend. Fix the known upload/session correctness bugs, align docs and packaging around the supported local profile, and finish with a release-grade verification sweep.

**Tech Stack:** FastAPI, asyncio, Python stdlib (`pathlib`, `shutil`, `urllib.parse`), existing browser frontend, local Ollama, local ASR, `unittest`, Node test runner, headless Chrome e2e

**Workspace Note:** This directory is not currently a git repository. Replace commit steps with “review changed files” checkpoints until git is initialized.

---

## Planned File Changes

- Create: `runtime_checks.py` — local dependency probes and capability snapshot builders
- Modify: `config.py` — local-only flags and local-provider helpers
- Modify: `server.py` — startup lifecycle wiring, readiness payloads, websocket bootstrap payloads, upload/session fixes
- Modify: `session.py` — recovered-session registration and local-only LLM enforcement
- Modify: `asr.py` — clarify grouped aligner segment behavior for user-facing timestamps
- Modify: `static/index.html` — local-only/runtime status UI mount points
- Modify: `static/app.js` — consume runtime snapshot, render local-only mode, degrade controls explicitly
- Modify: `static/styles.css` — styling for runtime status and local-only badge
- Modify: `README.md` — supported runtime, offline install path, dependency troubleshooting
- Modify: `.env.example` — local-only defaults
- Modify: `Dockerfile` — install `ffmpeg` for non-WAV upload parity
- Modify: `Makefile` — add release verification target
- Create: `tests/test_runtime_checks.py` — runtime snapshot tests
- Create: `tests/test_session_manager.py` — recovered-session registry tests
- Modify: `tests/test_server_endpoints.py` — startup and readiness coverage
- Modify: `tests/test_upload.py` — summary queue/degrade coverage
- Modify: `tests/test_asr_service.py` — align timestamp expectation with grouped user-facing segments
- Modify: `tests/test_phase1_e2e.mjs` — assert local-only badge/status on page load

### Task 1: Add Runtime Snapshot Groundwork

**Files:**
- Create: `runtime_checks.py`
- Modify: `config.py`
- Test: `tests/test_runtime_checks.py`

- [ ] **Step 1: Write the failing runtime snapshot tests**

Create `tests/test_runtime_checks.py` with this content:

```python
import unittest
from unittest.mock import patch

from config import ASRConfig, LLMConfig, ServerConfig
from runtime_checks import build_runtime_snapshot


class RuntimeChecksTests(unittest.TestCase):
    @patch("runtime_checks.os.path.exists", return_value=True)
    @patch("runtime_checks._is_parent_writable", return_value=True)
    @patch("runtime_checks.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_local_only_ollama_profile_reports_ready_capabilities(self, _which, _writable, _exists):
        llm = LLMConfig(
            provider="ollama",
            ollama_base_url="http://localhost:11434",
            ollama_model="qwen3.5:9b",
            local_only=True,
        )
        asr = ASRConfig(model_path="/tmp/asr-model")
        server = ServerConfig(
            host="127.0.0.1",
            recordings_dir="recordings",
            database_path="data/meeting_realtime_voice.sqlite3",
        )

        snapshot = build_runtime_snapshot(
            llm,
            asr,
            server,
            asr_state="ready",
            asr_error=None,
            llm_status="ok",
        )

        self.assertEqual(snapshot.mode, "local-only")
        self.assertEqual(snapshot.capabilities["transcription_realtime"].state, "ready")
        self.assertEqual(snapshot.capabilities["summary_local"].state, "ready")
        self.assertEqual(snapshot.capabilities["chat_local"].state, "ready")

    @patch("runtime_checks.os.path.exists", return_value=True)
    @patch("runtime_checks._is_parent_writable", return_value=True)
    @patch("runtime_checks.shutil.which", return_value=None)
    def test_missing_ffmpeg_degrades_transcoded_upload_only(self, _which, _writable, _exists):
        llm = LLMConfig(provider="ollama", ollama_base_url="http://localhost:11434", local_only=True)
        asr = ASRConfig(model_path="/tmp/asr-model")
        server = ServerConfig(host="127.0.0.1")

        snapshot = build_runtime_snapshot(
            llm,
            asr,
            server,
            asr_state="ready",
            asr_error=None,
            llm_status="ok",
        )

        self.assertEqual(snapshot.capabilities["transcription_upload_wav"].state, "ready")
        self.assertEqual(snapshot.capabilities["transcription_upload_transcoded"].state, "degraded")
        self.assertIn("ffmpeg", snapshot.capabilities["transcription_upload_transcoded"].detail.lower())

    @patch("runtime_checks.os.path.exists", return_value=True)
    @patch("runtime_checks._is_parent_writable", return_value=True)
    @patch("runtime_checks.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_remote_openai_endpoint_is_blocked_in_local_only_mode(self, _which, _writable, _exists):
        llm = LLMConfig(
            provider="openai",
            openai_base_url="https://api.openai.com/v1",
            openai_api_key="secret",
            local_only=True,
        )
        asr = ASRConfig(model_path="/tmp/asr-model")
        server = ServerConfig(host="127.0.0.1")

        snapshot = build_runtime_snapshot(
            llm,
            asr,
            server,
            asr_state="ready",
            asr_error=None,
            llm_status="blocked: remote provider disabled in local-only mode",
        )

        self.assertEqual(snapshot.mode, "local-only")
        self.assertEqual(snapshot.capabilities["summary_local"].state, "unavailable")
        self.assertEqual(snapshot.capabilities["chat_local"].state, "unavailable")
        self.assertTrue(snapshot.warnings)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
venv/bin/python -m unittest tests.test_runtime_checks -v
```

Expected:

- import error for `runtime_checks`
- or missing `LLMConfig.local_only`

- [ ] **Step 3: Implement the runtime snapshot module and config helpers**

Create `runtime_checks.py` with this content:

```python
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from config import ASRConfig, LLMConfig, ServerConfig


@dataclass(frozen=True)
class DependencyState:
    status: str
    detail: str = ""


@dataclass(frozen=True)
class CapabilityState:
    state: str
    detail: str = ""


@dataclass(frozen=True)
class RuntimeSnapshot:
    mode: str
    dependencies: dict[str, DependencyState]
    capabilities: dict[str, CapabilityState]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "dependencies": {
                key: {"status": value.status, "detail": value.detail}
                for key, value in self.dependencies.items()
            },
            "capabilities": {
                key: {"state": value.state, "detail": value.detail}
                for key, value in self.capabilities.items()
            },
            "warnings": list(self.warnings),
        }


def _is_parent_writable(path_value: str) -> bool:
    path = Path(os.path.expanduser(path_value))
    candidate = path if path.is_dir() else path.parent
    candidate.mkdir(parents=True, exist_ok=True)
    return os.access(candidate, os.W_OK)


def _is_loopback_host(hostname: str | None) -> bool:
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _local_provider_allowed(llm_config: LLMConfig) -> bool:
    if llm_config.provider == "ollama":
        parsed = urlparse(llm_config.ollama_base_url or "")
        return not parsed.hostname or _is_loopback_host(parsed.hostname)

    parsed = urlparse(llm_config.openai_base_url or "")
    return _is_loopback_host(parsed.hostname)


def build_runtime_snapshot(
    llm_config: LLMConfig,
    asr_config: ASRConfig,
    server_config: ServerConfig,
    *,
    asr_state: str,
    asr_error: str | None,
    llm_status: str,
) -> RuntimeSnapshot:
    warnings: list[str] = []

    asr_model_exists = os.path.exists(asr_config.model_path)
    ffmpeg_path = shutil.which("ffmpeg")
    recordings_writable = _is_parent_writable(server_config.recordings_dir)
    database_writable = _is_parent_writable(server_config.database_path)
    local_provider_ok = _local_provider_allowed(llm_config)

    if server_config.host not in {"127.0.0.1", "localhost", "::1"}:
        warnings.append(f"HOST={server_config.host} is not loopback-only.")

    if llm_config.local_only and not local_provider_ok:
        warnings.append("Remote LLM configuration is blocked in local-only mode.")

    dependencies = {
        "asr_model": DependencyState(
            status="ready" if asr_model_exists else "missing",
            detail=asr_config.model_path,
        ),
        "ffmpeg": DependencyState(
            status="ready" if ffmpeg_path else "missing",
            detail=ffmpeg_path or "ffmpeg not found in PATH",
        ),
        "recordings_dir": DependencyState(
            status="ready" if recordings_writable else "missing",
            detail=server_config.recordings_dir,
        ),
        "database_dir": DependencyState(
            status="ready" if database_writable else "missing",
            detail=server_config.database_path,
        ),
        "local_llm": DependencyState(
            status="ready" if llm_status.startswith("ok") and local_provider_ok else "missing",
            detail=llm_status,
        ),
    }

    transcription_state = "ready" if asr_state == "ready" and asr_model_exists else "unavailable"
    transcription_detail = asr_error or asr_state

    llm_capability_state = "ready" if llm_status.startswith("ok") and local_provider_ok else "unavailable"
    llm_capability_detail = llm_status if llm_status else "local llm unavailable"

    capabilities = {
        "transcription_realtime": CapabilityState(transcription_state, transcription_detail),
        "transcription_upload_wav": CapabilityState(transcription_state, transcription_detail),
        "transcription_upload_transcoded": CapabilityState(
            "ready" if transcription_state == "ready" and ffmpeg_path else "degraded",
            "ffmpeg available" if ffmpeg_path else "ffmpeg missing; WAV upload still supported",
        ),
        "summary_local": CapabilityState(llm_capability_state, llm_capability_detail),
        "chat_local": CapabilityState(llm_capability_state, llm_capability_detail),
        "history_read_write": CapabilityState(
            "ready" if recordings_writable and database_writable else "degraded",
            "local storage writable" if recordings_writable and database_writable else "check recordings/database paths",
        ),
    }

    return RuntimeSnapshot(
        mode="local-only" if llm_config.local_only else "mixed",
        dependencies=dependencies,
        capabilities=capabilities,
        warnings=warnings,
    )
```

Update `config.py`:

```python
from urllib.parse import urlparse
```

Add these fields and helper to `LLMConfig`:

```python
    local_only: bool = True

    def is_local_provider(self) -> bool:
        if self.provider == "ollama":
            parsed = urlparse(self.ollama_base_url or "")
            return not parsed.hostname or parsed.hostname in {"127.0.0.1", "localhost", "::1"}

        parsed = urlparse(self.openai_base_url or "")
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}
```

Update `load_config()` to populate `local_only`:

```python
    llm = LLMConfig(
        provider="openai" if os.getenv("OPENAI_API_KEY") else "ollama",
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:9b"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        local_only=_env_bool("LOCAL_ONLY_MODE", True),
    )
```

- [ ] **Step 4: Run the tests again**

Run:

```bash
venv/bin/python -m unittest tests.test_runtime_checks -v
```

Expected:

- all tests in `tests.test_runtime_checks` pass

- [ ] **Step 5: Review changed files**

Run:

```bash
ls runtime_checks.py tests/test_runtime_checks.py
rg -n "local_only|is_local_provider|build_runtime_snapshot" config.py runtime_checks.py
```

Expected:

- both files exist
- `config.py` shows the new local-only helpers

---

### Task 2: Wire Startup Lifecycle and Runtime Payloads

**Files:**
- Modify: `server.py`
- Test: `tests/test_server_endpoints.py`

- [ ] **Step 1: Add failing endpoint and startup tests**

Append these tests to `tests/test_server_endpoints.py`:

```python
    def test_startup_event_runs_asr_initialize(self):
        fake_asr = make_fake_asr(initialized=False)
        fake_asr.initialization_state = lambda: "idle"
        fake_asr.initialization_error = lambda: None

        with patch.object(server, "asr_service", fake_asr), patch.object(server, "get_llm_status", return_value="ok"):
            with TestClient(server.app):
                pass

        fake_asr.initialize.assert_awaited_once()

    def test_readiness_endpoint_returns_runtime_snapshot(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_asr.initialization_state = lambda: "ready"
        fake_asr.initialization_error = lambda: None

        with patch.object(server, "asr_service", fake_asr), patch.object(server, "get_llm_status", return_value="ok"), patch("runtime_checks.os.path.exists", return_value=True), patch("runtime_checks._is_parent_writable", return_value=True), patch("runtime_checks.shutil.which", return_value="/usr/bin/ffmpeg"):
            with TestClient(server.app) as client:
                response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["runtime"]["mode"], "local-only")
        self.assertIn("summary_local", payload["runtime"]["capabilities"])
```

- [ ] **Step 2: Run the targeted tests and confirm failure**

Run:

```bash
venv/bin/python -m unittest tests.test_server_endpoints.ServerEndpointTests.test_startup_event_runs_asr_initialize tests.test_server_endpoints.ServerEndpointTests.test_readiness_endpoint_returns_runtime_snapshot -v
```

Expected:

- startup test fails because `initialize` was not awaited
- readiness test fails because `runtime` payload is missing

- [ ] **Step 3: Implement startup wiring and runtime payload exposure**

Update the imports at the top of `server.py`:

```python
from runtime_checks import build_runtime_snapshot
```

Add a cached snapshot and refresh helper near the global service initialization:

```python
runtime_snapshot = None


def refresh_runtime_snapshot(llm_status: str | None = None):
    global runtime_snapshot
    asr_status = get_asr_status()
    runtime_snapshot = build_runtime_snapshot(
        llm_config,
        asr_config,
        server_config,
        asr_state=str(asr_status["state"]),
        asr_error=asr_status["error"],
        llm_status=llm_status or get_llm_status(),
    )
    return runtime_snapshot
```

Decorate the existing startup hook and refresh runtime state:

```python
@app.on_event("startup")
async def on_startup():
    snapshot = refresh_runtime_snapshot(llm_status=get_llm_status())
    for warning in get_runtime_warnings():
        logger.warning(warning)
    for warning in snapshot.warnings:
        logger.warning(warning)
    initialize = getattr(asr_service, "initialize", None)
    if callable(initialize):
        await initialize()
```

Expose runtime payloads in the websocket `ready` message:

```python
        snapshot = (runtime_snapshot or refresh_runtime_snapshot()).to_dict()
        await websocket.send_json({
            "type": "ready",
            "session_id": session.session_id,
            "asr_device": asr_config.device,
            "asr_ready": asr_service._initialized,
            "asr_init_timeout_sec": asr_config.init_timeout_sec,
            "asr_model": str(asr_config.model_path),
            "asr_language": asr_config.language,
            "asr_state": asr_status["state"],
            "llm_provider": llm_config.provider,
            "llm_model": llm_config.model_name,
            "runtime_warnings": get_runtime_warnings(),
            "runtime": snapshot,
        })
```

Update `/api/health` to include cached runtime state:

```python
    snapshot = (runtime_snapshot or refresh_runtime_snapshot()).to_dict()
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
```

Update `/api/readiness` to refresh and expose the live runtime snapshot:

```python
    asr_status = get_asr_status()
    llm_status = get_llm_status()
    snapshot = refresh_runtime_snapshot(llm_status=llm_status).to_dict()

    status = "ok"
    if asr_status["state"] == "failed" or not llm_status.startswith("ok"):
        status = "degraded"

    return {
        "status": status,
        "runtime": snapshot,
        "asr": asr_status,
        "llm_status": llm_status,
        "active_sessions": len(session_manager.sessions),
    }
```

- [ ] **Step 4: Run the targeted tests again**

Run:

```bash
venv/bin/python -m unittest tests.test_server_endpoints.ServerEndpointTests.test_startup_event_runs_asr_initialize tests.test_server_endpoints.ServerEndpointTests.test_readiness_endpoint_returns_runtime_snapshot -v
```

Expected:

- both tests pass

- [ ] **Step 5: Review changed files**

Run:

```bash
rg -n "@app.on_event\\(\"startup\"\\)|runtime_snapshot|\"runtime\":" server.py
```

Expected:

- startup hook is decorated
- websocket and readiness payloads include `runtime`

---

### Task 3: Fix Upload Summary Degradation and Session Rehydration

**Files:**
- Modify: `server.py`
- Modify: `session.py`
- Create: `tests/test_session_manager.py`
- Modify: `tests/test_upload.py`

- [ ] **Step 1: Add failing tests for rehydration and upload summary behavior**

Create `tests/test_session_manager.py` with:

```python
import unittest

from config import LLMConfig, MeetingConfig
from session import SessionManager


class SessionManagerRecoveryTests(unittest.TestCase):
    def test_register_recovered_session_replaces_temporary_key(self):
        manager = SessionManager(LLMConfig(), MeetingConfig())
        session = manager.create_session()
        temp_id = session.session_id

        manager.register_recovered_session("persisted-001", session)

        self.assertNotIn(temp_id, manager.sessions)
        self.assertIn("persisted-001", manager.sessions)
        self.assertIs(manager.sessions["persisted-001"], session)
        self.assertEqual(session.session_id, "persisted-001")
```

Append these tests to `tests/test_upload.py`:

```python
    def test_upload_skips_summary_task_when_transcript_is_short(self):
        with patch.object(server, "asr_service", self.fake_asr), patch("server.asyncio.create_task") as create_task:
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("test.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary_state"], "unavailable")
        create_task.assert_not_called()

    def test_upload_queues_summary_when_three_or_more_segments_exist(self):
        rich_result = FakeASRResult(
            segments=[
                {"text": "第一段", "start": 0.0, "end": 2.0},
                {"text": "第二段", "start": 2.0, "end": 4.0},
                {"text": "第三段", "start": 4.0, "end": 6.0},
            ]
        )
        rich_asr = make_fake_asr_with_result(rich_result)

        with patch.object(server, "asr_service", rich_asr), patch("server.asyncio.create_task") as create_task:
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("test.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary_state"], "queued")
        create_task.assert_called_once()
```

- [ ] **Step 2: Run the targeted tests and confirm failure**

Run:

```bash
venv/bin/python -m unittest tests.test_session_manager tests.test_upload.UploadEndpointTests.test_upload_skips_summary_task_when_transcript_is_short tests.test_upload.UploadEndpointTests.test_upload_queues_summary_when_three_or_more_segments_exist -v
```

Expected:

- missing `register_recovered_session`
- upload response missing `summary_state`
- summary queue behavior not matching the assertions

- [ ] **Step 3: Implement recovered-session registration and summary degradation**

Add this helper to `SessionManager` in `session.py`:

```python
    def register_recovered_session(self, session_id: str, session: MeetingSession) -> MeetingSession:
        stale_keys = [key for key, value in self.sessions.items() if value is session and key != session_id]
        for key in stale_keys:
            del self.sessions[key]

        session.session_id = session_id
        self.sessions[session_id] = session
        return session
```

In `MeetingSession._create_chat_model()`, block remote providers in local-only mode:

```python
        if self.llm_config.local_only and not self.llm_config.is_local_provider():
            raise RuntimeError("当前为本地离线模式，已禁用远程 LLM。请配置本地 Ollama。")
```

In `server.py`, update the session rehydration branch inside `chat_on_meeting()`:

```python
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
```

In `server.py`, update the upload route to skip the summary task for short transcripts and return explicit state:

```python
        summary_state = "unavailable"
        summary_message = "摘要需要至少 3 段转录。"
        if len(session.transcript) >= 3:
            summary_state = "queued"
            summary_message = "摘要已加入后台生成队列。"
            asyncio.create_task(regenerate_meeting_summary(session.session_id))

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
```

Delete the old unconditional line:

```python
        asyncio.create_task(regenerate_meeting_summary(session.session_id))
```

- [ ] **Step 4: Run the targeted tests again**

Run:

```bash
venv/bin/python -m unittest tests.test_session_manager tests.test_upload.UploadEndpointTests.test_upload_skips_summary_task_when_transcript_is_short tests.test_upload.UploadEndpointTests.test_upload_queues_summary_when_three_or_more_segments_exist -v
```

Expected:

- all targeted tests pass

- [ ] **Step 5: Review changed files**

Run:

```bash
rg -n "register_recovered_session|summary_state|summary_message" session.py server.py tests/test_upload.py tests/test_session_manager.py
```

Expected:

- new helper exists
- upload route now returns explicit summary state

---

### Task 4: Align ASR Timestamp Verification with Current Grouped Segment Behavior

**Files:**
- Modify: `asr.py`
- Modify: `tests/test_asr_service.py`

- [ ] **Step 1: Update the failing test first**

Rename the existing test in `tests/test_asr_service.py` and change its expectation to grouped user-facing output:

```python
    def test_transcribe_audio_array_groups_aligner_items_into_user_facing_segment(self):
        service = ASRService(ASRConfig(model_path="/tmp/fake-asr-model"))
        service._model = Mock()
        service._model.forced_aligner = object()
        service._model.transcribe.return_value = [
            SimpleNamespace(
                text="你好世界。",
                time_stamps=SimpleNamespace(
                    items=[
                        SimpleNamespace(text="你好", start_time=0.0, end_time=0.4),
                        SimpleNamespace(text="世界", start_time=0.4, end_time=0.8),
                        SimpleNamespace(text="。", start_time=0.8, end_time=0.9),
                    ]
                ),
            )
        ]

        result = service._transcribe_audio_array(
            audio=np.ones(16000, dtype=np.float32),
            sample_rate=16000,
            start_time=0.0,
        )

        self.assertTrue(result.has_timestamps)
        self.assertEqual(
            result.segments,
            [
                {"start": 0.0, "end": 0.9, "text": "你好世界。"},
            ],
        )
```

- [ ] **Step 2: Run the single test and verify the current code passes it**

Run:

```bash
venv/bin/python -m unittest tests.test_asr_service.ASRServiceTests.test_transcribe_audio_array_groups_aligner_items_into_user_facing_segment -v
```

Expected:

- PASS

- [ ] **Step 3: Clarify the intended behavior in `asr.py`**

Update the `_extract_segments_from_timestamps()` docstring in `asr.py` to state explicitly that this helper returns grouped user-facing segments, not raw aligner tokens:

```python
        """Extract grouped transcript segments from forced-aligner timestamps.

        The forced aligner may return one item per word or character. This helper
        merges those low-level items into user-facing transcript segments based on
        punctuation, pause boundaries, and segment duration caps so upload mode
        does not surface one-token-per-line transcript fragments.
        """
```

- [ ] **Step 4: Run the broader ASR service tests**

Run:

```bash
venv/bin/python -m unittest tests.test_asr_service -v
```

Expected:

- all tests in `tests.test_asr_service` pass

- [ ] **Step 5: Review changed files**

Run:

```bash
rg -n "grouped user-facing|one-token-per-line|你好世界。" asr.py tests/test_asr_service.py
```

Expected:

- the docstring and renamed test reflect the grouped-segment contract

---

### Task 5: Surface Local-Only Runtime State in the Frontend

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `static/styles.css`
- Modify: `tests/test_phase1_e2e.mjs`

- [ ] **Step 1: Add the failing e2e assertion**

Insert this assertion after the initial page-load wait in `tests/test_phase1_e2e.mjs`:

```javascript
        await waitForCondition(
            client,
            `document.querySelector('#runtimeModeBadge') && document.querySelector('#runtimeModeBadge').innerText.includes('本地模式')`,
            15000,
        );
```

- [ ] **Step 2: Run the e2e test and confirm failure**

Run:

```bash
node tests/test_phase1_e2e.mjs
```

Expected:

- timeout waiting for `#runtimeModeBadge`

- [ ] **Step 3: Add runtime UI mounts and frontend rendering**

In `static/index.html`, add this block inside `.header-meta`, near the existing connection badge:

```html
                <div class="runtime-mode-badge" id="runtimeModeBadge" hidden>
                    <span>本地模式</span>
                </div>
                <div class="runtime-status" id="runtimeStatus" hidden></div>
```

In `static/app.js`, add these element bindings:

```javascript
    runtimeModeBadge: $('runtimeModeBadge'),
    runtimeStatus: $('runtimeStatus'),
```

Add this renderer helper near the other status functions:

```javascript
function applyRuntimeSnapshot(runtime) {
    if (!runtime) return;

    if (runtime.mode === 'local-only') {
        el.runtimeModeBadge.hidden = false;
        el.runtimeModeBadge.textContent = '本地模式';
    } else {
        el.runtimeModeBadge.hidden = true;
    }

    const warnings = Array.isArray(runtime.warnings) ? runtime.warnings : [];
    const capabilities = runtime.capabilities || {};
    const summaryState = capabilities.summary_local?.state || 'unavailable';
    const chatState = capabilities.chat_local?.state || 'unavailable';

    const parts = [];
    if (summaryState !== 'ready') {
        parts.push('摘要不可用');
    }
    if (chatState !== 'ready') {
        parts.push('问答不可用');
    }

    if (parts.length || warnings.length) {
        el.runtimeStatus.hidden = false;
        el.runtimeStatus.textContent = [...parts, ...warnings].join(' · ');
    } else {
        el.runtimeStatus.hidden = true;
        el.runtimeStatus.textContent = '';
    }

    if (chatState !== 'ready' && !state.isMeetingActive && !state.selectedMeeting) {
        setChatComposerEnabled(false, '本地 LLM 未就绪；问答已禁用');
    }
}
```

Call the renderer from the websocket `ready` handler:

```javascript
        case 'ready':
            state.sessionId = msg.session_id;
            state.isConnected = true;
            applyRuntimeSnapshot(msg.runtime);
            setConnectionBadge('connected');
            setStatus('已连接');
            appendLog(`会话已创建: ${msg.session_id.slice(0, 8)}`);
            appendLog(`ASR: ${msg.asr_device} · LLM: ${msg.llm_model}`, 'info');
            break;
```

Call it once on page load from readiness:

```javascript
async function loadRuntimeSnapshot() {
    try {
        const payload = await fetchJson('/api/readiness');
        applyRuntimeSnapshot(payload.runtime);
    } catch (_) {
        // Keep the app usable even if readiness probing fails during initial load.
    }
}
```

At the bottom of the file, replace the last line:

```javascript
loadMeetingHistory({ preserveSelection: true, announce: false }).catch(() => {});
```

with:

```javascript
loadRuntimeSnapshot().catch(() => {});
loadMeetingHistory({ preserveSelection: true, announce: false }).catch(() => {});
```

In `static/styles.css`, add styles:

```css
.runtime-mode-badge {
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid var(--accent-border);
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 0.72rem;
  font-weight: 600;
}

.runtime-status {
  max-width: 280px;
  font-size: 0.72rem;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

- [ ] **Step 4: Run the e2e test again**

Run:

```bash
node tests/test_phase1_e2e.mjs
```

Expected:

- the runtime badge assertion passes
- the existing history/edit/export assertions continue to pass

- [ ] **Step 5: Review changed files**

Run:

```bash
rg -n "runtimeModeBadge|applyRuntimeSnapshot|本地模式" static/index.html static/app.js static/styles.css tests/test_phase1_e2e.mjs
```

Expected:

- all four files reference the new runtime status UI

---

### Task 6: Align Packaging, Env Defaults, and Release Verification

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `Dockerfile`
- Modify: `Makefile`

- [ ] **Step 1: Update environment defaults and docs for the supported local profile**

Update `.env.example` by adding this line under the OpenAI-compatible section:

```env
LOCAL_ONLY_MODE=true
```

Update `README.md`:

```md
## 当前定位

- 适合单机自托管和内部 beta。
- 默认只绑定 `127.0.0.1:8800`。
- Phase 0 发布路径默认为本地离线模式；远程 LLM 不属于默认支持路径。
- 当前支持的发布运行时是 Python `3.12`。
```

Add an offline dependency note under quick start:

```md
在开始前，请先确认：

- 已安装 Python `3.12`
- 已安装 `ffmpeg`（用于 MP3/M4A/OGG 等非 WAV 上传）
- 已准备本地 ASR 模型目录
- 已启动本地 Ollama，或接受“仅转录、无摘要/问答”的降级模式
```

Update the troubleshooting section with:

```md
### 非 WAV 上传失败

先确认本机或容器里可以执行 `ffmpeg -version`。
```

- [ ] **Step 2: Update container and Makefile support**

Update `Dockerfile` package installation line:

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
```

Add this target to `Makefile`:

```make
test-release: test-python test-ui test-e2e
```

Add this target to `Makefile`:

```make
smoke-local:
	curl -sf http://127.0.0.1:8800/api/health
	curl -sf http://127.0.0.1:8800/api/readiness
```

- [ ] **Step 3: Verify the file updates**

Run:

```bash
rg -n 'LOCAL_ONLY_MODE|Python 3.12|ffmpeg|test-release|smoke-local' README.md .env.example Dockerfile Makefile
```

Expected:

- each file shows the new local-profile defaults and verification targets

- [ ] **Step 4: Run the full verification sweep**

Run:

```bash
venv/bin/python -m unittest discover -s tests
node --test tests/test_ui_formatters.mjs
node tests/test_phase1_e2e.mjs
```

Expected:

- Python tests pass
- UI formatter test passes
- e2e test passes

- [ ] **Step 5: Review the final changed-file set**

Run:

```bash
rg --files runtime_checks.py config.py server.py session.py asr.py static README.md .env.example Dockerfile Makefile tests | sort
```

Expected:

- the changed files match the planned Phase 0 scope

---

## Spec Coverage Check

- local-only runtime profile: covered by Tasks 1, 2, 5
- startup/readiness lifecycle: covered by Task 2
- upload/session correctness: covered by Task 3
- explicit degraded UX: covered by Tasks 3 and 5
- release runtime/docs/container parity: covered by Task 6
- green verification baseline: covered by Tasks 4 and 6

## Placeholder Scan

- No `TODO`, `TBD`, or “handle appropriately” placeholders remain.
- Every new file has an explicit path.
- Every command includes an expected outcome.

## Type Consistency Check

- Runtime diagnostics revolve around `RuntimeSnapshot`, `DependencyState`, and `CapabilityState`.
- Local-only config uses `LLMConfig.local_only` and `LLMConfig.is_local_provider()`.
- Upload response uses `summary_state` and `summary_message` consistently.

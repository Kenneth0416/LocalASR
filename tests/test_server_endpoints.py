import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from persistence import MeetingStore
import server


def make_fake_asr(initialized: bool = True):
    fake_asr = type("FakeASRService", (), {})()
    fake_asr._initialized = initialized
    fake_asr.initialize = AsyncMock()
    fake_asr.shutdown = AsyncMock()
    return fake_asr


def make_fake_realtime_transcriber():
    fake_transcriber = type("FakeRealtimeTranscriber", (), {})()
    fake_transcriber.push_pcm = AsyncMock(return_value=[])
    fake_transcriber.flush = AsyncMock(return_value=[])
    return fake_transcriber


def make_runtime_snapshot(marker: str):
    payload = {
        "mode": "local-only",
        "dependencies": {"marker": marker},
        "capabilities": {
            "transcription_realtime": {"state": "ready", "detail": marker},
            "summary_local": {"state": "ready", "detail": marker},
            "chat_local": {"state": "ready", "detail": marker},
        },
        "warnings": [marker],
    }
    return SimpleNamespace(
        warnings=[marker],
        capabilities={
            "transcription_realtime": SimpleNamespace(state="ready", detail=marker),
            "summary_local": SimpleNamespace(state="ready", detail=marker),
            "chat_local": SimpleNamespace(state="ready", detail=marker),
        },
        to_dict=lambda payload=payload: payload,
    )


class ServerEndpointTests(unittest.TestCase):
    def _build_store_with_meeting(self, tmpdir: str, session_id: str = "meeting-001"):
        store = MeetingStore(Path(tmpdir) / "meetings.sqlite3")
        recording_path = Path(tmpdir) / "recordings" / f"{session_id}.wav"
        recording_path.parent.mkdir(parents=True, exist_ok=True)
        recording_path.write_bytes(b"RIFFdemo")
        created_at = "2026-04-07T12:00:00"

        store.insert_transcript_segment(
            session_id,
            created_at,
            SimpleNamespace(
                id="seg-1",
                speaker="发言人",
                text="我们决定在周五上线。",
                start_time=0.0,
                end_time=3.2,
                timestamp="2026-04-07T12:00:01",
            ),
            ordinal=1,
        )
        store.insert_chat_message(
            session_id,
            created_at,
            SimpleNamespace(
                id="chat-1",
                role="assistant",
                content="目前结论是周五上线。",
                timestamp="2026-04-07T12:00:10",
            ),
            ordinal=1,
        )
        store.upsert_summary(
            session_id,
            created_at,
            "会议确定周五上线。",
            updated_at="2026-04-07T12:00:20",
            turn_count=1,
        )
        store.complete_session(
            session_id,
            created_at,
            ended_at="2026-04-07T12:30:00",
            recording_path=str(recording_path),
            recording_bytes=recording_path.stat().st_size,
            recording_error=None,
            status="completed",
        )
        return store, recording_path

    def test_health_endpoint_returns_local_process_status(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)
        startup_runtime_snapshot = make_runtime_snapshot("startup")
        health_runtime_snapshot = make_runtime_snapshot("health-fresh")
        stale_runtime_snapshot = make_runtime_snapshot("health-stale")
        original_runtime_snapshot = server.runtime_snapshot
        original_cached_llm_status = server.cached_llm_status
        llm_status_calls: list[str] = []

        def refresh_side_effect(*args, **kwargs):
            llm_status_calls.append(kwargs.get("llm_status"))
            if len(llm_status_calls) == 1:
                return startup_runtime_snapshot
            return health_runtime_snapshot

        try:
            with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr), patch.object(
                server,
                "get_llm_status",
                side_effect=["startup-ok", AssertionError("health should not probe llm")],
            ), patch.object(
                server,
                "refresh_runtime_snapshot",
                side_effect=refresh_side_effect,
                create=True,
            ) as refresh_runtime_snapshot:
                with TestClient(server.app) as client:
                    server.runtime_snapshot = stale_runtime_snapshot
                    server.cached_llm_status = "startup-ok"
                    response = client.get("/api/health")
                    self.assertEqual(refresh_runtime_snapshot.call_count, 2)
        finally:
            server.runtime_snapshot = original_runtime_snapshot
            server.cached_llm_status = original_cached_llm_status

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["asr_ready"])
        self.assertIn("audio_queue_maxsize", payload)
        self.assertEqual(payload["runtime"], health_runtime_snapshot.to_dict())
        self.assertEqual(llm_status_calls, ["startup-ok", "startup-ok"])

    def test_sessions_endpoint_is_hidden_by_default(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)

        with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr):
            with TestClient(server.app) as client:
                response = client.get("/api/sessions")

        self.assertEqual(response.status_code, 404)

    def test_readiness_endpoint_surfaces_dependency_state(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)
        fake_runtime_snapshot = make_runtime_snapshot("readiness")

        with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr), patch.object(server, "get_llm_status", return_value="ok"), patch.object(server, "refresh_runtime_snapshot", return_value=fake_runtime_snapshot, create=True):
            with TestClient(server.app) as client:
                response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["asr"]["state"], "ready")
        self.assertEqual(payload["llm_status"], "ok")
        self.assertEqual(payload["runtime"], fake_runtime_snapshot.to_dict())

    def test_readiness_endpoint_degrades_when_local_llm_is_blocked_even_if_probe_is_ok(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)
        blocked_runtime_snapshot = SimpleNamespace(
            warnings=["blocked"],
            capabilities={
                "transcription_realtime": SimpleNamespace(state="ready", detail="ok"),
                "summary_local": SimpleNamespace(state="unavailable", detail="blocked by local-only policy"),
                "chat_local": SimpleNamespace(state="unavailable", detail="blocked by local-only policy"),
            },
            to_dict=lambda: {
                "mode": "local-only",
                "dependencies": {},
                "capabilities": {
                    "transcription_realtime": {"state": "ready", "detail": "ok"},
                    "summary_local": {"state": "unavailable", "detail": "blocked by local-only policy"},
                    "chat_local": {"state": "unavailable", "detail": "blocked by local-only policy"},
                },
                "warnings": ["blocked"],
            },
        )

        with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr), patch.object(server, "get_llm_status", return_value="ok"), patch.object(server, "refresh_runtime_snapshot", return_value=blocked_runtime_snapshot, create=True):
            with TestClient(server.app) as client:
                response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["llm_status"], "ok")
        self.assertEqual(payload["runtime"]["capabilities"]["summary_local"]["state"], "unavailable")
        self.assertEqual(payload["runtime"]["capabilities"]["chat_local"]["state"], "unavailable")

    def test_readiness_endpoint_degrades_when_asr_is_idle(self):
        fake_asr = make_fake_asr(initialized=False)
        fake_preview_asr = make_fake_asr(initialized=True)
        fake_asr.initialization_state = lambda: "idle"
        fake_asr.initialization_error = lambda: None

        with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr), patch.object(server, "get_llm_status", return_value="ok"):
            with TestClient(server.app) as client:
                response = client.get("/api/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["asr"]["state"], "idle")
        self.assertEqual(payload["runtime"]["capabilities"]["transcription_realtime"]["state"], "unavailable")

    def test_preview_failure_keeps_runtime_transcription_ready_and_degrades_readiness(self):
        fake_final_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=False)
        fake_final_asr.initialization_state = lambda: "ready"
        fake_final_asr.initialization_error = lambda: None
        fake_preview_asr.initialization_state = lambda: "failed"
        fake_preview_asr.initialization_error = lambda: "preview failed"
        fake_runtime_snapshot = make_runtime_snapshot("preview-failed")
        captured_states: list[tuple[str, str | None, str]] = []

        def build_runtime_snapshot_side_effect(llm_config, asr_config, server_config, *, asr_state, asr_error, llm_status):
            captured_states.append((asr_state, asr_error, llm_status))
            return fake_runtime_snapshot

        with patch.object(server, "asr_service", fake_final_asr), \
             patch.object(server, "preview_asr_service", fake_preview_asr), \
             patch.object(server, "build_runtime_snapshot", side_effect=build_runtime_snapshot_side_effect), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "cached_llm_status", "ok"):
            with TestClient(server.app) as client:
                health_response = client.get("/api/health")
                readiness_response = client.get("/api/readiness")

        self.assertEqual([state for state, _, _ in captured_states], ["ready", "ready", "ready"])

        self.assertEqual(health_response.status_code, 200)
        health_payload = health_response.json()
        self.assertFalse(health_payload["preview_enabled"])
        self.assertEqual(health_payload["runtime"]["capabilities"]["transcription_realtime"]["state"], "ready")

        self.assertEqual(readiness_response.status_code, 200)
        readiness_payload = readiness_response.json()
        self.assertEqual(readiness_payload["status"], "degraded")
        self.assertEqual(readiness_payload["runtime"]["capabilities"]["transcription_realtime"]["state"], "ready")

    def test_startup_event_runs_asr_initialize(self):
        fake_asr = make_fake_asr(initialized=False)
        fake_preview_asr = make_fake_asr(initialized=True)
        fake_runtime_snapshot = make_runtime_snapshot("startup")
        events: list[str] = []

        async def initialize_side_effect():
            events.append("initialize")

        def refresh_side_effect(*args, **kwargs):
            events.append("refresh")
            return fake_runtime_snapshot

        fake_asr.initialize = AsyncMock(side_effect=initialize_side_effect)

        with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr), patch.object(server, "get_llm_status", return_value="ok"), patch.object(server, "refresh_runtime_snapshot", side_effect=refresh_side_effect, create=True):
            with TestClient(server.app):
                pass

        fake_asr.initialize.assert_awaited_once()
        self.assertEqual(events, ["initialize", "refresh"])

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

    def test_websocket_ready_payload_includes_runtime_snapshot(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)
        fake_transcriber = make_fake_realtime_transcriber()
        startup_runtime_snapshot = make_runtime_snapshot("startup")
        websocket_runtime_snapshot = make_runtime_snapshot("websocket")
        stale_runtime_snapshot = make_runtime_snapshot("stale")
        original_runtime_snapshot = server.runtime_snapshot
        original_cached_llm_status = server.cached_llm_status
        llm_status_calls: list[str] = []

        def refresh_side_effect(*args, **kwargs):
            llm_status_calls.append(kwargs.get("llm_status"))
            if len(llm_status_calls) == 1:
                return startup_runtime_snapshot
            return websocket_runtime_snapshot

        try:
            with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr), patch.object(
                server,
                "get_llm_status",
                side_effect=["startup-ok", AssertionError("websocket should not probe llm")],
            ), patch.object(
                server,
                "refresh_runtime_snapshot",
                side_effect=refresh_side_effect,
                create=True,
            ) as refresh_runtime_snapshot, patch.object(
                server,
                "build_realtime_transcriber",
                return_value=fake_transcriber,
                create=True,
            ):
                with TestClient(server.app) as client:
                    server.runtime_snapshot = stale_runtime_snapshot
                    server.cached_llm_status = "startup-ok"
                    with client.websocket_connect("/ws/meeting") as websocket:
                        ready = websocket.receive_json()

                        self.assertEqual(ready["type"], "ready")
                        self.assertEqual(ready["asr_model"], str(server.asr_config.model_path))
                        self.assertEqual(ready["final_asr_model"], str(server.asr_config.model_path))
                        self.assertEqual(ready["preview_asr_model"], str(server.preview_asr_config.model_path))
                        self.assertTrue(ready["preview_enabled"])
                        self.assertEqual(ready["final_asr_state"], "ready")
                        self.assertEqual(ready["preview_asr_state"], "ready")
                        self.assertEqual(ready["runtime"], websocket_runtime_snapshot.to_dict())
                        self.assertEqual(refresh_runtime_snapshot.call_count, 2)
                        self.assertEqual(llm_status_calls, ["startup-ok", "startup-ok"])
        finally:
            server.runtime_snapshot = original_runtime_snapshot
            server.cached_llm_status = original_cached_llm_status

    def test_shutdown_event_shuts_down_preview_and_final_asr_services(self):
        fake_final_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)
        fake_runtime_snapshot = make_runtime_snapshot("shutdown")

        with patch.object(server, "asr_service", fake_final_asr), \
             patch.object(server, "preview_asr_service", fake_preview_asr), \
             patch.object(server, "get_llm_status", return_value="ok"), \
             patch.object(server, "refresh_runtime_snapshot", return_value=fake_runtime_snapshot, create=True):
            with TestClient(server.app):
                pass

        fake_preview_asr.shutdown.assert_awaited_once()
        fake_final_asr.shutdown.assert_awaited_once()

    def test_meeting_history_endpoints_support_detail_export_edit_and_delete(self):
        fake_asr = make_fake_asr(initialized=True)
        fake_preview_asr = make_fake_asr(initialized=True)

        with TemporaryDirectory() as tmpdir:
            store, recording_path = self._build_store_with_meeting(tmpdir)

            with patch.object(server, "asr_service", fake_asr), patch.object(server, "preview_asr_service", fake_preview_asr), patch.object(server, "meeting_store", store), patch.object(server.session_manager, "store", store):
                with TestClient(server.app) as client:
                    response = client.get("/api/meetings")
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertEqual(len(payload["meetings"]), 1)
                    self.assertEqual(payload["meetings"][0]["session_id"], "meeting-001")

                    response = client.get("/api/meetings/meeting-001")
                    self.assertEqual(response.status_code, 200)
                    meeting = response.json()
                    self.assertEqual(meeting["summary"], "会议确定周五上线。")
                    self.assertEqual(len(meeting["transcript"]), 1)

                    response = client.get("/api/meetings/meeting-001/export.md")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("attachment; filename=\"meeting-meeting-001.md\"", response.headers["content-disposition"])
                    self.assertIn("## Summary", response.text)

                    response = client.get("/api/meetings/meeting-001/export.json")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("attachment; filename=\"meeting-meeting-001.json\"", response.headers["content-disposition"])
                    self.assertEqual(response.json()["session_id"], "meeting-001")

                    response = client.get("/api/meetings/meeting-001/recording")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, recording_path.read_bytes())

                    response = client.patch(
                        "/api/meetings/meeting-001/transcript/seg-1",
                        json={"speaker": "主持人", "text": "我们决定在周五上线，并在周四回归。"},
                    )
                    self.assertEqual(response.status_code, 200)
                    updated = response.json()
                    self.assertEqual(updated["segment"]["speaker"], "主持人")
                    self.assertEqual(updated["meeting"]["transcript"][0]["text"], "我们决定在周五上线，并在周四回归。")

                    response = client.delete("/api/meetings/meeting-001")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "deleted")
                    self.assertFalse(recording_path.exists())

                    response = client.get("/api/meetings/meeting-001")
                    self.assertEqual(response.status_code, 404)

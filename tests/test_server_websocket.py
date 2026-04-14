import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import server
from asr import ASRResult
from session import MeetingSession


def make_pcm(*parts, sample_rate: int = 16000) -> bytes:
    chunks = []
    for duration_sec, amplitude in parts:
        sample_count = int(duration_sec * sample_rate)
        sample_bytes = int(amplitude).to_bytes(2, byteorder="little", signed=True)
        chunks.append(sample_bytes * sample_count)

    return b"".join(chunks)


def receive_until_type(websocket, expected_type: str):
    while True:
        message = websocket.receive_json()
        if message["type"] == expected_type:
            return message


def make_fake_asr_service(*, initialized: bool, wait_ready_side_effect=None, transcribe_side_effect=None):
    fake_asr = type("FakeASRService", (), {})()
    fake_asr._initialized = initialized
    fake_asr.initialize = AsyncMock()
    fake_asr.wait_ready = AsyncMock(side_effect=wait_ready_side_effect)
    if isinstance(transcribe_side_effect, list):
        fake_asr.transcribe_wav = AsyncMock(side_effect=transcribe_side_effect)
    elif transcribe_side_effect is None:
        fake_asr.transcribe_wav = AsyncMock()
    else:
        fake_asr.transcribe_wav = AsyncMock(return_value=transcribe_side_effect)
    return fake_asr


def make_realtime_event(
    *,
    event_type: str,
    text: str,
    segment_id: int,
    revision: int,
    is_final: bool,
    cut_reason: str,
    start_time: float = 0.0,
    end_time: float = 0.6,
    processing_time: float = 0.01,
):
    return server.RealtimeTranscriptEvent(
        event_type=event_type,
        text=text,
        start_time=start_time,
        end_time=end_time,
        processing_time=processing_time,
        segment_id=segment_id,
        revision=revision,
        is_final=is_final,
        cut_reason=cut_reason,
    )


class FakeRealtimeTranscriber:
    def __init__(self, *, push_results=None, flush_results=None, flush_callback=None):
        self.push_results = list(push_results or [])
        self.flush_results = list(flush_results or [])
        self.flush_reasons = []
        self.flush_callback = flush_callback

    async def push_pcm(self, pcm_chunk):
        if self.push_results:
            result = self.push_results.pop(0)
            return result
        return []

    async def flush(self, reason: str = "flush"):
        self.flush_reasons.append(reason)
        if self.flush_callback is not None:
            return await self.flush_callback(reason)
        if self.flush_results:
            return self.flush_results.pop(0)
        return []


class FakePromptAwareRealtimeTranscriber:
    def __init__(self, *, asr_service, session_state):
        self.asr_service = asr_service
        self.session_state = session_state
        self.flush_reasons = []

    async def push_pcm(self, pcm_chunk):
        return []

    async def flush(self, reason: str = "flush"):
        self.flush_reasons.append(reason)
        result = await self.asr_service.transcribe_wav(
            (b"", 16000),
            language=self.session_state.get("language"),
            context=self.session_state.get("asr_prompt"),
        )
        return [
            make_realtime_event(
                event_type="final",
                text=result.text,
                segment_id=1,
                revision=1,
                is_final=True,
                cut_reason=reason,
                start_time=0.0,
                end_time=float(getattr(result, "audio_duration", 0.0) or 0.0),
            )
        ]


class ServerWebSocketTests(unittest.TestCase):
    def test_live_preview_upsert_ignores_stale_revisions(self):
        session = MeetingSession("session-preview-cache", server.llm_config, server.meeting_config, store=None)

        newest, newest_accepted = session.upsert_live_preview_segment(
            segment_id=7,
            revision=3,
            speaker="发言人",
            text="最新内容",
            start_time=1.0,
            end_time=2.0,
        )
        stale, stale_accepted = session.upsert_live_preview_segment(
            segment_id=7,
            revision=2,
            speaker="发言人",
            text="旧内容",
            start_time=1.0,
            end_time=2.0,
        )

        self.assertTrue(newest_accepted)
        self.assertFalse(stale_accepted)
        self.assertEqual(newest.text, "最新内容")
        self.assertEqual(stale.text, "最新内容")
        self.assertEqual(session.live_preview_segments[7].revision, 3)
        self.assertEqual(session.live_preview_segments[7].text, "最新内容")

    def test_live_preview_upsert_ignores_duplicate_revision_replays(self):
        session = MeetingSession("session-preview-cache-replay", server.llm_config, server.meeting_config, store=None)

        first, first_accepted = session.upsert_live_preview_segment(
            segment_id=9,
            revision=4,
            speaker="发言人",
            text="第一次预览",
            start_time=2.0,
            end_time=3.0,
        )
        replay, replay_accepted = session.upsert_live_preview_segment(
            segment_id=9,
            revision=4,
            speaker="发言人",
            text="重复回放",
            start_time=2.0,
            end_time=3.0,
        )

        self.assertTrue(first_accepted)
        self.assertFalse(replay_accepted)
        self.assertEqual(first.text, "第一次预览")
        self.assertEqual(replay.text, "第一次预览")
        self.assertEqual(session.live_preview_segments[9].revision, 4)
        self.assertEqual(session.live_preview_segments[9].text, "第一次预览")

    def test_empty_final_clears_live_preview_without_persisting(self):
        fake_transcriber = FakeRealtimeTranscriber(
            push_results=[
                [
                    make_realtime_event(
                        event_type="preview",
                        text="临时预览",
                        segment_id=1,
                        revision=1,
                        is_final=False,
                        cut_reason="preview_tick",
                    )
                ]
            ],
            flush_results=[
                [
                    make_realtime_event(
                        event_type="final",
                        text="",
                        segment_id=1,
                        revision=2,
                        is_final=True,
                        cut_reason="stop",
                    )
                ]
            ],
        )
        session = MeetingSession("session-empty-final", server.llm_config, server.meeting_config, store=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            recording_dir = Path(tmpdir)
            with patch.object(server, "asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server.session_manager, "create_session", return_value=session), \
                 patch.object(server.meeting_store, "complete_session"), \
                 patch.object(server, "RECORDINGS_DIR", recording_dir):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        websocket.receive_json()
                        websocket.send_text(json.dumps({"type": "start"}))
                        receive_until_type(websocket, "status")
                        receive_until_type(websocket, "start_ack")

                        websocket.send_bytes(make_pcm((0.1, 1800)))

                        preview = receive_until_type(websocket, "transcript")
                        self.assertEqual(preview["segment"]["id"], "")
                        self.assertEqual(preview["segment"]["segment_id"], 1)
                        receive_until_type(websocket, "transcribe_done")
                        self.assertIn(1, session.live_preview_segments)

                        websocket.send_text(json.dumps({"type": "stop"}))

                        final_done = receive_until_type(websocket, "transcribe_done")
                        self.assertTrue(final_done["is_final"])
                        self.assertEqual(final_done["segment_id"], 1)
                        closed = receive_until_type(websocket, "stopped")
                        self.assertEqual(closed["type"], "stopped")

        self.assertEqual(session.live_preview_segments, {})
        self.assertEqual(session.transcript, [])

    def test_stale_preview_revision_is_not_broadcast_to_client(self):
        fake_transcriber = FakeRealtimeTranscriber(
            push_results=[
                [
                    make_realtime_event(
                        event_type="preview",
                        text="最新预览",
                        segment_id=5,
                        revision=3,
                        is_final=False,
                        cut_reason="preview_tick",
                    ),
                    make_realtime_event(
                        event_type="preview",
                        text="旧预览",
                        segment_id=5,
                        revision=2,
                        is_final=False,
                        cut_reason="preview_tick",
                    ),
                ]
            ],
            flush_results=[[]],
        )
        session = MeetingSession("session-stale-preview", server.llm_config, server.meeting_config, store=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            recording_dir = Path(tmpdir)
            with patch.object(server, "asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server.session_manager, "create_session", return_value=session), \
                 patch.object(server.meeting_store, "complete_session"), \
                 patch.object(server, "RECORDINGS_DIR", recording_dir):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        websocket.receive_json()
                        websocket.send_text(json.dumps({"type": "start"}))
                        receive_until_type(websocket, "status")
                        receive_until_type(websocket, "start_ack")

                        websocket.send_bytes(make_pcm((0.1, 1800)))
                        websocket.send_text(json.dumps({"type": "stop"}))

                        messages = []
                        while True:
                            message = websocket.receive_json()
                            messages.append(message)
                            if message["type"] == "stopped":
                                break

        transcript_messages = [msg for msg in messages if msg["type"] == "transcript"]
        self.assertEqual(len(transcript_messages), 1)
        self.assertEqual(transcript_messages[0]["segment"]["segment_id"], 5)
        self.assertEqual(transcript_messages[0]["segment"]["revision"], 3)
        self.assertEqual(transcript_messages[0]["segment"]["text"], "最新预览")
        self.assertEqual(session.live_preview_segments[5].revision, 3)
        self.assertEqual(session.live_preview_segments[5].text, "最新预览")

    def test_final_clears_only_its_live_preview_segment(self):
        fake_transcriber = FakeRealtimeTranscriber(
            push_results=[
                [
                    make_realtime_event(
                        event_type="preview",
                        text="第一段预览",
                        segment_id=1,
                        revision=1,
                        is_final=False,
                        cut_reason="preview_tick",
                    ),
                    make_realtime_event(
                        event_type="preview",
                        text="第二段预览",
                        segment_id=2,
                        revision=1,
                        is_final=False,
                        cut_reason="preview_tick",
                    ),
                ]
            ],
            flush_results=[
                [
                    make_realtime_event(
                        event_type="final",
                        text="第一段最终",
                        segment_id=1,
                        revision=2,
                        is_final=True,
                        cut_reason="stop",
                    )
                ]
            ],
        )
        session = MeetingSession("session-live-preview-clear", server.llm_config, server.meeting_config, store=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            recording_dir = Path(tmpdir)
            with patch.object(server, "asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server.session_manager, "create_session", return_value=session), \
                 patch.object(server.meeting_store, "complete_session"), \
                 patch.object(server, "RECORDINGS_DIR", recording_dir):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        websocket.receive_json()
                        websocket.send_text(json.dumps({"type": "start"}))
                        receive_until_type(websocket, "status")
                        receive_until_type(websocket, "start_ack")

                        websocket.send_bytes(make_pcm((0.1, 1800)))

                        preview_one = receive_until_type(websocket, "transcript")
                        self.assertEqual(preview_one["segment"]["segment_id"], 1)
                        self.assertEqual(preview_one["segment"]["id"], "")
                        receive_until_type(websocket, "transcribe_done")

                        preview_two = receive_until_type(websocket, "transcript")
                        self.assertEqual(preview_two["segment"]["segment_id"], 2)
                        self.assertEqual(preview_two["segment"]["id"], "")
                        receive_until_type(websocket, "transcribe_done")

                        self.assertEqual(session.live_preview_segments[1].revision, 1)
                        self.assertEqual(session.live_preview_segments[1].text, "第一段预览")
                        self.assertEqual(session.live_preview_segments[2].text, "第二段预览")

                        websocket.send_text(json.dumps({"type": "stop"}))

                        final = receive_until_type(websocket, "transcript")
                        self.assertEqual(final["segment"]["segment_id"], 1)
                        self.assertNotEqual(final["segment"]["id"], "")
                        self.assertEqual(final["segment"]["cut_reason"], "stop")
                        receive_until_type(websocket, "transcribe_done")
                        receive_until_type(websocket, "stopped")

                        self.assertNotIn(1, session.live_preview_segments)
                        self.assertIn(2, session.live_preview_segments)
                        self.assertEqual(session.live_preview_segments[2].text, "第二段预览")

        self.assertEqual(fake_transcriber.flush_reasons, ["stop"])

    def test_preview_event_does_not_persist_and_final_reuses_segment_id(self):
        fake_transcriber = FakeRealtimeTranscriber(
            push_results=[
                [
                    make_realtime_event(
                        event_type="preview",
                        text="先试一下",
                        segment_id=1,
                        revision=1,
                        is_final=False,
                        cut_reason="preview_tick",
                    )
                ]
            ],
            flush_results=[
                [
                    make_realtime_event(
                        event_type="final",
                        text="先试一下确认版",
                        segment_id=1,
                        revision=2,
                        is_final=True,
                        cut_reason="stop",
                    )
                ]
            ],
        )
        session = MeetingSession("session-preview", server.llm_config, server.meeting_config, store=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            recording_dir = Path(tmpdir)
            with patch.object(server, "asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server.session_manager, "create_session", return_value=session), \
                 patch.object(server.meeting_store, "complete_session"), \
                 patch.object(server, "RECORDINGS_DIR", recording_dir):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        ready = websocket.receive_json()
                        self.assertEqual(ready["type"], "ready")

                        websocket.send_text(json.dumps({"type": "start"}))
                        self.assertEqual(receive_until_type(websocket, "status")["type"], "status")
                        self.assertEqual(receive_until_type(websocket, "start_ack")["type"], "start_ack")

                        websocket.send_bytes(make_pcm((0.1, 1800)))

                        preview = receive_until_type(websocket, "transcript")
                        self.assertEqual(preview["type"], "transcript")
                        self.assertEqual(preview["segment"]["id"], "")
                        self.assertEqual(preview["segment"]["segment_id"], 1)
                        self.assertEqual(preview["segment"]["revision"], 1)
                        self.assertFalse(preview["segment"]["is_final"])
                        self.assertEqual(preview["segment"]["cut_reason"], "preview_tick")
                        self.assertEqual(len(session.transcript), 0)
                        self.assertIn(1, session.live_preview_segments)
                        self.assertEqual(session.live_preview_segments[1].text, "先试一下")

                        preview_done = websocket.receive_json()
                        self.assertEqual(preview_done["type"], "transcribe_done")
                        self.assertEqual(preview_done["phase"], "preview")
                        self.assertEqual(preview_done["segment_id"], 1)
                        self.assertEqual(preview_done["revision"], 1)
                        self.assertFalse(preview_done["is_final"])
                        self.assertEqual(preview_done["cut_reason"], "preview_tick")

                        websocket.send_text(json.dumps({"type": "stop"}))

                        final = receive_until_type(websocket, "transcript")
                        self.assertEqual(final["type"], "transcript")
                        self.assertNotEqual(final["segment"]["id"], "")
                        self.assertEqual(final["segment"]["segment_id"], 1)
                        self.assertEqual(final["segment"]["revision"], 2)
                        self.assertTrue(final["segment"]["is_final"])
                        self.assertEqual(final["segment"]["cut_reason"], "stop")
                        self.assertEqual(len(session.transcript), 1)
                        self.assertEqual(session.transcript[0].text, "先试一下确认版")
                        self.assertEqual(session.live_preview_segments, {})

                        final_done = websocket.receive_json()
                        self.assertEqual(final_done["type"], "transcribe_done")
                        self.assertEqual(final_done["phase"], "final")
                        self.assertEqual(final_done["segment_id"], 1)
                        self.assertEqual(final_done["revision"], 2)
                        self.assertTrue(final_done["is_final"])
                        self.assertEqual(final_done["cut_reason"], "stop")

                        closed = receive_until_type(websocket, "stopped")
                        self.assertEqual(closed["type"], "stopped")

        self.assertEqual(fake_transcriber.flush_reasons, ["stop"])

    def test_final_only_compatibility_still_persists_transcript(self):
        fake_transcriber = FakeRealtimeTranscriber(
            push_results=[[]],
            flush_results=[
                [
                    make_realtime_event(
                        event_type="final",
                        text="纯最终结果",
                        segment_id=1,
                        revision=1,
                        is_final=True,
                        cut_reason="flush",
                    )
                ]
            ],
        )
        session = MeetingSession("session-final-only", server.llm_config, server.meeting_config, store=None)

        with tempfile.TemporaryDirectory() as tmpdir:
            recording_dir = Path(tmpdir)
            with patch.object(server, "asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server.session_manager, "create_session", return_value=session), \
                 patch.object(server.meeting_store, "complete_session"), \
                 patch.object(server, "RECORDINGS_DIR", recording_dir):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        websocket.receive_json()
                        websocket.send_text(json.dumps({"type": "start"}))
                        receive_until_type(websocket, "status")
                        receive_until_type(websocket, "start_ack")

                        websocket.send_bytes(make_pcm((0.1, 1800)))
                        websocket.send_text(json.dumps({"type": "stop"}))

                        final = receive_until_type(websocket, "transcript")
                        self.assertNotEqual(final["segment"]["id"], "")
                        self.assertEqual(final["segment"]["segment_id"], 1)
                        self.assertEqual(final["segment"]["revision"], 1)
                        self.assertTrue(final["segment"]["is_final"])
                        self.assertEqual(final["segment"]["cut_reason"], "flush")
                        self.assertEqual(final["segment"]["text"], "纯最终结果")
                        self.assertEqual(len(session.transcript), 1)
                        self.assertEqual(session.transcript[0].text, "纯最终结果")

                        final_done = websocket.receive_json()
                        self.assertEqual(final_done["type"], "transcribe_done")
                        self.assertEqual(final_done["phase"], "final")
                        self.assertEqual(final_done["segment_id"], 1)
                        self.assertEqual(final_done["revision"], 1)
                        self.assertTrue(final_done["is_final"])
                        self.assertEqual(final_done["cut_reason"], "flush")

                        closed = receive_until_type(websocket, "stopped")
                        self.assertEqual(closed["type"], "stopped")

        self.assertEqual(fake_transcriber.flush_reasons, ["stop"])

    def test_semantic_transcript_emits_on_pause_and_flushes_tail(self):
        fake_transcriber = FakeRealtimeTranscriber(
            push_results=[
                [
                    make_realtime_event(
                        event_type="final",
                        text="今天我们讨论预算",
                        segment_id=1,
                        revision=1,
                        is_final=True,
                        cut_reason="semantic",
                        start_time=0.0,
                        end_time=4.0,
                    )
                ]
            ],
            flush_results=[
                [
                    make_realtime_event(
                        event_type="final",
                        text="最后补一句",
                        segment_id=2,
                        revision=1,
                        is_final=True,
                        cut_reason="flush",
                        start_time=4.0,
                        end_time=5.6,
                    )
                ]
            ],
        )
        fake_asr = make_fake_asr_service(initialized=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            recording_dir = Path(tmpdir)
            with patch.object(server, "asr_service", fake_asr), \
                 patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server.meeting_store, "complete_session"), \
                 patch.object(server, "RECORDINGS_DIR", recording_dir):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        ready = websocket.receive_json()
                        self.assertEqual(ready["type"], "ready")

                        websocket.send_text(json.dumps({"type": "start"}))
                        self.assertEqual(receive_until_type(websocket, "status")["type"], "status")
                        self.assertEqual(receive_until_type(websocket, "start_ack")["type"], "start_ack")

                        websocket.send_bytes(make_pcm((3.6, 1800), (0.6, 0)))
                        transcript = receive_until_type(websocket, "transcript")
                        self.assertEqual(transcript["type"], "transcript")
                        self.assertEqual(transcript["segment"]["text"], "今天我们讨论预算")
                        self.assertEqual(transcript["segment"]["segment_id"], 1)
                        self.assertEqual(transcript["segment"]["revision"], 1)
                        self.assertTrue(transcript["segment"]["is_final"])
                        self.assertEqual(transcript["segment"]["cut_reason"], "semantic")
                        self.assertAlmostEqual(transcript["segment"]["start"], 0.0, places=2)
                        self.assertAlmostEqual(transcript["segment"]["end"], 4.0, places=1)

                        done = websocket.receive_json()
                        self.assertEqual(done["type"], "transcribe_done")
                        self.assertEqual(done["phase"], "final")
                        self.assertEqual(done["segment_id"], 1)
                        self.assertEqual(done["revision"], 1)
                        self.assertTrue(done["is_final"])

                        websocket.send_bytes(make_pcm((1.4, 1800)))
                        websocket.send_text(json.dumps({"type": "stop"}))
                        transcript = receive_until_type(websocket, "transcript")
                        self.assertEqual(transcript["type"], "transcript")
                        self.assertEqual(transcript["segment"]["text"], "最后补一句")
                        self.assertEqual(transcript["segment"]["segment_id"], 2)
                        self.assertEqual(transcript["segment"]["revision"], 1)
                        self.assertTrue(transcript["segment"]["is_final"])
                        self.assertEqual(transcript["segment"]["cut_reason"], "flush")
                        self.assertAlmostEqual(transcript["segment"]["start"], 4.0, places=1)
                        self.assertAlmostEqual(transcript["segment"]["end"], 5.6, places=1)

                        done = websocket.receive_json()
                        self.assertEqual(done["type"], "transcribe_done")
                        self.assertEqual(done["phase"], "final")
                        self.assertEqual(done["segment_id"], 2)
                        self.assertEqual(done["revision"], 1)
                        self.assertTrue(done["is_final"])

                        closed = receive_until_type(websocket, "stopped")
                        self.assertEqual(closed["type"], "stopped")

    def test_stop_flushes_pending_audio_before_stopped_ack(self):
        fake_asr = make_fake_asr_service(
            initialized=True,
            transcribe_side_effect=ASRResult(
                text="tail audio",
                segments=[{"start": 0.0, "end": 0.1, "text": "tail audio"}],
                audio_duration=0.1,
                processing_time=0.01,
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            recording_dir = Path(tmpdir)
            fake_transcriber = FakeRealtimeTranscriber(
                flush_results=[
                    [
                        make_realtime_event(
                            event_type="final",
                            text="tail audio",
                            segment_id=1,
                            revision=1,
                            is_final=True,
                            cut_reason="stop",
                            start_time=0.0,
                            end_time=0.1,
                        )
                    ]
                ]
            )
            with patch.object(server, "asr_service", fake_asr), \
                 patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
                 patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
                 patch.object(server, "RECORDINGS_DIR", recording_dir), \
                 patch.object(server.meeting_store, "complete_session"):
                with TestClient(server.app) as client:
                    with client.websocket_connect("/ws/meeting") as websocket:
                        ready = websocket.receive_json()
                        self.assertEqual(ready["type"], "ready")

                        websocket.send_text(json.dumps({"type": "start"}))
                        self.assertEqual(receive_until_type(websocket, "status")["type"], "status")
                        self.assertEqual(receive_until_type(websocket, "start_ack")["type"], "start_ack")

                        websocket.send_bytes(make_pcm((0.1, 1800)))
                        websocket.send_text(json.dumps({"type": "stop"}))

                        transcript = receive_until_type(websocket, "transcript")
                        self.assertEqual(transcript["type"], "transcript")
                        self.assertEqual(transcript["segment"]["text"], "tail audio")
                        self.assertTrue(transcript["segment"]["is_final"])

                        done = websocket.receive_json()
                        self.assertEqual(done["type"], "transcribe_done")
                        self.assertTrue(done["is_final"])

                        closed = receive_until_type(websocket, "stopped")
                        self.assertEqual(closed["type"], "stopped")
                        self.assertIsNone(closed["recording_error"])
                        self.assertEqual(closed["recording_bytes"], 3200)

                        recording_path = Path(closed["recording_path"])
                        self.assertTrue(recording_path.exists())
                        self.assertEqual(recording_path.parent, recording_dir)

                        with wave.open(str(recording_path), "rb") as wav_file:
                            self.assertEqual(wav_file.getnchannels(), 1)
                            self.assertEqual(wav_file.getframerate(), 16000)
                            self.assertEqual(wav_file.getsampwidth(), 2)
                            self.assertEqual(wav_file.getnframes(), 1600)

        fake_asr.wait_ready.assert_awaited_once_with(timeout=server.asr_config.init_timeout_sec)
        self.assertEqual(fake_transcriber.flush_reasons, ["stop"])

    def test_stop_flush_emits_final_text_from_fake_realtime_transcriber(self):
        fake_asr = make_fake_asr_service(
            initialized=True,
            transcribe_side_effect=ASRResult(
                text="这是完整的一分钟口播内容，不应该只保留开头那一句。",
                segments=[
                    {"start": 0.0, "end": 1.1, "text": "这是"},
                    {"start": 1.1, "end": 1.9, "text": "开头"},
                    {"start": 1.9, "end": 2.8, "text": "那句。"},
                ],
                audio_duration=5.0,
                processing_time=0.02,
                has_timestamps=True,
            ),
        )
        fake_transcriber = FakeRealtimeTranscriber(
            flush_results=[
                [
                    make_realtime_event(
                        event_type="final",
                        text="这是完整的一分钟口播内容，不应该只保留开头那一句。",
                        segment_id=1,
                        revision=1,
                        is_final=True,
                        cut_reason="flush",
                        start_time=0.0,
                        end_time=5.0,
                    )
                ]
            ]
        )

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
             patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
             patch.object(server.meeting_store, "complete_session"):
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/meeting") as websocket:
                    websocket.receive_json()
                    websocket.send_text(json.dumps({"type": "start"}))
                    receive_until_type(websocket, "start_ack")

                    websocket.send_bytes(make_pcm((5.0, 1800)))
                    websocket.send_text(json.dumps({"type": "stop"}))

                    transcript = receive_until_type(websocket, "transcript")
                    self.assertEqual(
                        transcript["segment"]["text"],
                        "这是完整的一分钟口播内容，不应该只保留开头那一句。",
                    )

    def test_start_timeout_returns_asr_specific_error_message(self):
        fake_asr = make_fake_asr_service(
            initialized=False,
            wait_ready_side_effect=RuntimeError("ASR model initialization timed out after 180s"),
        )
        fake_transcriber = FakeRealtimeTranscriber()

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
             patch.object(server, "build_realtime_transcriber", return_value=fake_transcriber, create=True), \
             patch.object(server.meeting_store, "complete_session"):
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/meeting") as websocket:
                    ready = websocket.receive_json()
                    self.assertEqual(ready["type"], "ready")

                    websocket.send_text(json.dumps({"type": "start"}))

                    status = websocket.receive_json()
                    self.assertEqual(status["type"], "status")

                    error = websocket.receive_json()
                    self.assertEqual(error["type"], "error")
                    self.assertIn("转录模型初始化超时", error["message"])

    def test_realtime_start_passes_asr_prompt_to_transcription_context(self):
        fake_asr = make_fake_asr_service(
            initialized=True,
            transcribe_side_effect=ASRResult(
                text="带术语提示的转录",
                segments=[{"start": 0.0, "end": 0.1, "text": "带术语提示的转录"}],
                audio_duration=0.1,
                processing_time=0.01,
            ),
        )
        fake_transcriber = FakePromptAwareRealtimeTranscriber(
            asr_service=fake_asr,
            session_state={"language": None, "asr_prompt": None},
        )

        def build_fake_transcriber(session_state):
            fake_transcriber.session_state = session_state
            return fake_transcriber

        with patch.object(server, "asr_service", fake_asr), \
             patch.object(server, "preview_asr_service", make_fake_asr_service(initialized=True)), \
             patch.object(server, "build_realtime_transcriber", side_effect=build_fake_transcriber, create=True), \
             patch.object(server.meeting_store, "complete_session"):
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws/meeting") as websocket:
                    websocket.receive_json()
                    websocket.send_text(json.dumps({
                        "type": "start",
                        "language": "Chinese",
                        "asr_prompt": "术语：Qwen3-ASR，Codex",
                    }))
                    receive_until_type(websocket, "status")
                    receive_until_type(websocket, "start_ack")

                    websocket.send_bytes(make_pcm((0.1, 1800)))
                    websocket.send_text(json.dumps({"type": "stop"}))

                    receive_until_type(websocket, "transcript")
                    receive_until_type(websocket, "stopped")

        fake_asr.transcribe_wav.assert_awaited_once()
        self.assertEqual(
            fake_asr.transcribe_wav.await_args.kwargs.get("context"),
            "术语：Qwen3-ASR，Codex",
        )
        self.assertEqual(
            fake_asr.transcribe_wav.await_args.kwargs.get("language"),
            "Chinese",
        )

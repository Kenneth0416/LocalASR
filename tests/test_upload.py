"""
Tests for /api/upload endpoint - dual-mode transcription feature.
RED phase: tests written first to define expected behavior.
"""

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from persistence import MeetingStore
import server


class FakeASRResult:
    """Fake ASR transcription result"""
    def __init__(self, text="测试转录文本", segments=None, audio_duration=10.0, processing_time=0.5):
        self.text = text
        self.segments = segments or [
            {"text": "第一段文本", "start": 0.0, "end": 5.0},
            {"text": "第二段文本", "start": 5.0, "end": 10.0},
        ]
        self.audio_duration = audio_duration
        self.processing_time = processing_time


def make_fake_asr_with_result(result=None):
    """Create a fake ASR service that returns a predefined result"""
    fake_asr = type("FakeASRService", (), {})()
    fake_asr._initialized = True
    fake_asr.initialize = AsyncMock()
    fake_asr.shutdown = AsyncMock()
    fake_asr.transcribe_wav = AsyncMock(return_value=result or FakeASRResult())
    return fake_asr


def create_test_wav_bytes(duration_sec=1, sample_rate=16000):
    """Create minimal valid WAV file bytes for testing"""
    import struct
    import wave

    num_samples = int(sample_rate * duration_sec)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        # Write silence (zeros)
        wav.writeframes(bytes(num_samples * 2))
    return buffer.getvalue()


class UploadEndpointTests(unittest.TestCase):
    """Test suite for /api/upload endpoint"""

    def setUp(self):
        """Set up test fixtures"""
        self.fake_asr = make_fake_asr_with_result()
        self.test_wav_bytes = create_test_wav_bytes()

    def test_upload_accepts_valid_wav_file(self):
        """Upload endpoint should accept valid WAV files and return transcription"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("test.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                    data={"language": "Chinese"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("session_id", payload)
        self.assertIn("segments", payload)
        self.assertIsInstance(payload["segments"], list)
        self.assertEqual(len(payload["segments"]), 2)

    def test_upload_rejects_empty_file(self):
        """Upload endpoint should reject empty files"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("empty.wav", io.BytesIO(b""), "audio/wav")},
                )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertIn("Empty file", payload["detail"])

    def test_upload_rejects_invalid_file_type(self):
        """Upload endpoint should reject non-audio files"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("document.txt", io.BytesIO(b"not audio content"), "text/plain")},
                )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertIn("Unsupported file type", payload["detail"])

    def test_upload_handles_mp3_files(self):
        """Upload endpoint should accept MP3 files"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                # Use .mp3 extension to bypass content-type check
                response = client.post(
                    "/api/upload",
                    files={"file": ("recording.mp3", io.BytesIO(self.test_wav_bytes), "audio/mpeg")},
                )

        self.assertEqual(response.status_code, 200)

    def test_upload_creates_meeting_session(self):
        """Upload should create a persisted meeting session"""
        with TemporaryDirectory() as tmpdir:
            store = MeetingStore(Path(tmpdir) / "meetings.sqlite3")

            with patch.object(server, "asr_service", self.fake_asr), \
                 patch.object(server, "meeting_store", store), \
                 patch.object(server.session_manager, "store", store):
                with TestClient(server.app) as client:
                    response = client.post(
                        "/api/upload",
                        files={"file": ("meeting.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                    )

            self.assertEqual(response.status_code, 200)
            session_id = response.json()["session_id"]

            # Verify meeting was persisted
            meeting = store.get_meeting(session_id)
            self.assertIsNotNone(meeting)
            self.assertEqual(meeting["session_id"], session_id)

    def test_upload_response_includes_metadata(self):
        """Upload response should include file metadata"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("conference.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("filename", payload)
        self.assertEqual(payload["filename"], "conference.wav")
        self.assertIn("audio_duration", payload)
        self.assertIn("processing_time", payload)
        self.assertIn("full_text", payload)

    def test_upload_uses_language_parameter(self):
        """Upload should pass language parameter to ASR service"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("test.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                    data={"language": "English"},
                )

        self.assertEqual(response.status_code, 200)
        # Verify ASR was called with language parameter
        self.fake_asr.transcribe_wav.assert_called_once()
        call_kwargs = self.fake_asr.transcribe_wav.call_args
        # transcribe_wav(file_content, language=lang) - check keyword argument
        if call_kwargs.kwargs:
            self.assertEqual(call_kwargs.kwargs.get("language"), "English")
        elif len(call_kwargs.args) >= 2:
            self.assertEqual(call_kwargs.args[1], "English")

    def test_upload_passes_asr_prompt_to_transcribe_wav(self):
        """Upload should pass a one-shot ASR prompt as model context."""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("test.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                    data={"asr_prompt": "术语：Qwen3-ASR，Codex"},
                )

        self.assertEqual(response.status_code, 200)
        call_kwargs = self.fake_asr.transcribe_wav.call_args
        self.assertEqual(call_kwargs.kwargs.get("context"), "术语：Qwen3-ASR，Codex")

    def test_upload_handles_asr_error_gracefully(self):
        """Upload should return 500 if ASR transcription fails"""
        error_asr = make_fake_asr_with_result()
        error_asr.transcribe_wav = AsyncMock(side_effect=Exception("ASR model error"))

        with patch.object(server, "asr_service", error_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("test.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                )

        self.assertEqual(response.status_code, 500)
        payload = response.json()
        self.assertIn("Transcription failed", payload["detail"])

    def test_upload_skips_summary_task_when_transcript_is_short(self):
        """Upload should not queue summary generation for transcripts under 3 segments."""
        short_result = FakeASRResult(
            segments=[
                {"text": "第一段文本", "start": 0.0, "end": 1.0},
                {"text": "第二段文本", "start": 1.0, "end": 2.0},
            ]
        )
        short_asr = make_fake_asr_with_result(short_result)

        with patch.object(server, "asr_service", short_asr), \
             patch.object(server, "regenerate_meeting_summary", AsyncMock()) as summary_mock:
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("short.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary_state"], "unavailable")
        self.assertEqual(payload["summary_message"], "转录段数少于 3 段，暂不生成摘要")
        summary_mock.assert_not_called()

    def test_upload_queues_summary_when_three_or_more_segments_exist(self):
        """Upload should queue summary generation for transcripts with at least 3 segments."""
        queued_result = FakeASRResult(
            segments=[
                {"text": "第一段文本", "start": 0.0, "end": 1.0},
                {"text": "第二段文本", "start": 1.0, "end": 2.0},
                {"text": "第三段文本", "start": 2.0, "end": 3.0},
            ]
        )
        queued_asr = make_fake_asr_with_result(queued_result)

        with patch.object(server, "asr_service", queued_asr), \
             patch.object(server, "regenerate_meeting_summary", AsyncMock()) as summary_mock:
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("queued.wav", io.BytesIO(self.test_wav_bytes), "audio/wav")},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary_state"], "queued")
        self.assertEqual(payload["summary_message"], "摘要已排队生成")
        summary_mock.assert_called_once()


class UploadEndpointFileTypeTests(unittest.TestCase):
    """Test file type validation for upload endpoint"""

    def setUp(self):
        self.fake_asr = make_fake_asr_with_result()

    def test_upload_accepts_m4a_by_extension(self):
        """Upload should accept M4A files by extension"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("audio.m4a", io.BytesIO(b"fake audio"), "audio/x-m4a")},
                )
        # M4A is accepted (not rejected for type, but may fail ASR)
        # The test checks that it's not rejected at upload level
        self.assertIn(response.status_code, [200, 500])

    def test_upload_accepts_ogg_by_extension(self):
        """Upload should accept OGG files by extension"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("audio.ogg", io.BytesIO(b"fake audio"), "audio/ogg")},
                )
        self.assertIn(response.status_code, [200, 500])

    def test_upload_accepts_flac_by_extension(self):
        """Upload should accept FLAC files by extension"""
        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("audio.flac", io.BytesIO(b"fake audio"), "audio/flac")},
                )
        self.assertIn(response.status_code, [200, 500])


class UploadEndpointSizeLimitTests(unittest.TestCase):
    """Test file size validation for upload endpoint"""

    def setUp(self):
        self.fake_asr = make_fake_asr_with_result()

    def test_upload_rejects_oversized_file(self):
        """Upload should reject files larger than 500MB"""
        # Create a file larger than 500MB (501MB)
        oversized_content = b"x" * (501 * 1024 * 1024)

        with patch.object(server, "asr_service", self.fake_asr):
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/upload",
                    files={"file": ("large.wav", io.BytesIO(oversized_content), "audio/wav")},
                )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertIn("too large", payload["detail"])


if __name__ == "__main__":
    unittest.main()

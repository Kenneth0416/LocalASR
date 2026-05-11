import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from asr import ASRResult, ASRService, pcm16le_to_audio_tuple
from config import ASRConfig


class ASRServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_wav_waits_for_model_ready_before_first_chunk(self):
        service = ASRService(ASRConfig())
        expected = ASRResult(
            text="first chunk transcript",
            segments=[{"start": 0.0, "end": 0.1, "text": "first chunk transcript"}],
            audio_duration=0.1,
            processing_time=0.01,
        )

        async def fake_wait_ready(timeout: float = 60.0):
            service._initialized = True
            service._mlx_transcriber = object()

        service.wait_ready = AsyncMock(side_effect=fake_wait_ready)
        service._transcribe_wav_mlx = AsyncMock(return_value=expected)

        result = await service.transcribe_wav(b"fake wav bytes")

        service.wait_ready.assert_awaited_once()
        self.assertIs(result, expected)

    def test_read_audio_source_tuple_preserves_original_scale(self):
        service = ASRService(ASRConfig())
        audio = np.array([0.01, -0.2, 0.5], dtype=np.float32)

        loaded_audio, sample_rate = service._read_audio_source((audio, 16000))

        self.assertEqual(sample_rate, 16000)
        np.testing.assert_allclose(loaded_audio, audio)

    def test_pcm16le_to_audio_tuple_preserves_relative_levels(self):
        pcm = np.array([3276, -16384, 8192], dtype=np.int16).tobytes()

        audio, sample_rate = pcm16le_to_audio_tuple(pcm, sample_rate=16000)

        self.assertEqual(sample_rate, 16000)
        np.testing.assert_allclose(
            audio,
            np.array([3276, -16384, 8192], dtype=np.float32) / 32768.0,
        )

    async def test_transcribe_uses_mlx_transcriber(self):
        service = ASRService(ASRConfig())
        mock_transcriber = Mock()
        mock_transcriber.is_loaded = True
        mock_transcriber.transcribe_audio.return_value = [
            {"start": 0.0, "end": 1.0, "text": "hello"}
        ]
        service._mlx_transcriber = mock_transcriber
        service._initialized = True

        with patch.object(service, "_read_audio_source", return_value=(np.zeros(16000, dtype=np.float32), 16000)):
            result = await service.transcribe(b"fake wav bytes")

        self.assertEqual(result.text, "hello")
        self.assertTrue(result.has_timestamps)

    def test_initialization_state_idle(self):
        service = ASRService(ASRConfig())
        self.assertEqual(service.initialization_state(), "idle")

    def test_initialization_state_ready(self):
        service = ASRService(ASRConfig())
        mock_transcriber = Mock()
        mock_transcriber.is_loaded = True
        service._mlx_transcriber = mock_transcriber
        service._initialized = True
        self.assertEqual(service.initialization_state(), "ready")

import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from asr import ASRResult, ASRService, pcm16le_to_audio_tuple
from config import ASRConfig


class ASRServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_wav_waits_for_model_ready_before_first_chunk(self):
        service = ASRService(ASRConfig(model_path="/tmp/fake-asr-model"))
        expected = ASRResult(
            text="first chunk transcript",
            segments=[{"start": 0.0, "end": 0.1, "text": "first chunk transcript"}],
            audio_duration=0.1,
            processing_time=0.01,
        )

        async def fake_wait_ready(timeout: float = 60.0):
            service._initialized = True
            service._model = object()
            service._processor = object()

        service.wait_ready = AsyncMock(side_effect=fake_wait_ready)
        service._transcribe_wav_sync = Mock(return_value=expected)

        result = await service.transcribe_wav(b"fake wav bytes")

        service.wait_ready.assert_awaited_once()
        service._transcribe_wav_sync.assert_called_once_with(b"fake wav bytes", None, None)
        self.assertIs(result, expected)

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

    def test_transcribe_audio_array_uses_configured_language(self):
        service = ASRService(ASRConfig(model_path="/tmp/fake-asr-model", language="Chinese"))
        service._model = Mock()
        service._model.forced_aligner = None
        service._model.transcribe.return_value = [
            SimpleNamespace(
                text="你好",
                time_stamps=None,
            )
        ]

        service._transcribe_audio_array(
            audio=np.ones(8000, dtype=np.float32),
            sample_rate=16000,
            start_time=0.0,
        )

        service._model.transcribe.assert_called_once()
        self.assertEqual(service._model.transcribe.call_args.kwargs["language"], "Chinese")
        self.assertFalse(service._model.transcribe.call_args.kwargs["return_time_stamps"])

    def test_transcribe_audio_array_passes_context_to_qwen_prompt(self):
        service = ASRService(ASRConfig(model_path="/tmp/fake-asr-model"))
        service._model = Mock()
        service._model.forced_aligner = None
        service._model.transcribe.return_value = [
            SimpleNamespace(
                text="术语识别正确",
                time_stamps=None,
            )
        ]

        service._transcribe_audio_array(
            audio=np.ones(8000, dtype=np.float32),
            sample_rate=16000,
            start_time=0.0,
            context="术语：Qwen3-ASR，Codex",
        )

        self.assertEqual(
            service._model.transcribe.call_args.kwargs["context"],
            "术语：Qwen3-ASR，Codex",
        )

    def test_read_audio_source_tuple_preserves_original_scale(self):
        service = ASRService(ASRConfig(model_path="/tmp/fake-asr-model"))
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

    def test_load_model_sync_disables_low_cpu_mem_usage_on_mps(self):
        service = ASRService(
            ASRConfig(
                model_path="/tmp/fake-asr-model",
                aligner_path="/tmp/fake-aligner-model",
                device="mps",
            )
        )
        fake_model_backend = Mock()
        fake_model_backend.model = Mock()
        fake_model_backend.model.to.return_value = fake_model_backend.model
        fake_model_backend.processor = object()
        fake_aligner = Mock()
        fake_aligner.model = Mock()
        fake_aligner.model.to.return_value = fake_aligner.model

        fake_qwen_asr = ModuleType("qwen_asr")
        fake_qwen_asr.Qwen3ASRModel = SimpleNamespace(
            from_pretrained=Mock(return_value=fake_model_backend)
        )

        with patch.dict("sys.modules", {"qwen_asr": fake_qwen_asr}), \
             patch("asr.os.path.exists", side_effect=lambda path: path in {"/tmp/fake-asr-model", "/tmp/fake-aligner-model"}), \
             patch("asr.load_alignment_model", return_value=fake_aligner) as load_alignment_model:
            service._load_model_sync()

        self.assertEqual(
            fake_qwen_asr.Qwen3ASRModel.from_pretrained.call_args.kwargs["low_cpu_mem_usage"],
            False,
        )
        self.assertEqual(load_alignment_model.call_args.kwargs["low_cpu_mem_usage"], False)

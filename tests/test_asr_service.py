import threading
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from asr import ASRResult, ASRService, pcm16le_to_audio_tuple
from config import ASRConfig


class FakeInputs(dict):
    def to(self, *_args, **_kwargs):
        return self


def install_fake_qwen_utils(parsed_text: str):
    fake_qwen = ModuleType("qwen_asr")
    fake_inference = ModuleType("qwen_asr.inference")
    fake_utils = ModuleType("qwen_asr.inference.utils")
    fake_utils.parse_asr_output = Mock(return_value=(None, parsed_text))
    fake_utils.normalize_language_name = Mock(side_effect=lambda language: language)
    fake_utils.validate_language = Mock()
    return patch.dict(
        "sys.modules",
        {
            "qwen_asr": fake_qwen,
            "qwen_asr.inference": fake_inference,
            "qwen_asr.inference.utils": fake_utils,
        },
    )


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
        service._model.processor = Mock()
        service._model.processor.return_value = FakeInputs({"input_ids": np.array([[1, 2]], dtype=np.int64)})
        service._model.processor.batch_decode.return_value = ["<asr_text>你好世界。"]
        service._model.model = Mock(device="cpu", dtype=np.float32)
        service._model._build_text_prompt = Mock(return_value="<prompt>")
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
        service._run_model_generate = Mock(return_value=SimpleNamespace(sequences=np.array([[1, 2, 3]], dtype=np.int64)))

        with install_fake_qwen_utils("你好世界。"):
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
        service._model.processor = Mock()
        service._model.processor.return_value = FakeInputs({"input_ids": np.array([[1, 2]], dtype=np.int64)})
        service._model.processor.batch_decode.return_value = ["<asr_text>你好"]
        service._model.model = Mock(device="cpu", dtype=np.float32)
        service._model._build_text_prompt = Mock(return_value="<prompt>")
        service._model.forced_aligner = None
        service._run_model_generate = Mock(return_value=SimpleNamespace(sequences=np.array([[1, 2, 3]], dtype=np.int64)))

        with install_fake_qwen_utils("你好"):
            service._transcribe_audio_array(
                audio=np.ones(8000, dtype=np.float32),
                sample_rate=16000,
                start_time=0.0,
            )

        service._model._build_text_prompt.assert_called_once_with(
            context="",
            force_language="Chinese",
        )

    def test_transcribe_audio_array_passes_context_to_qwen_prompt(self):
        service = ASRService(ASRConfig(model_path="/tmp/fake-asr-model"))
        service._model = Mock()
        service._model.processor = Mock()
        service._model.processor.return_value = FakeInputs({"input_ids": np.array([[1, 2]], dtype=np.int64)})
        service._model.processor.batch_decode.return_value = ["<asr_text>术语识别正确"]
        service._model.model = Mock(device="cpu", dtype=np.float32)
        service._model._build_text_prompt = Mock(return_value="<prompt>")
        service._model.forced_aligner = None
        service._run_model_generate = Mock(return_value=SimpleNamespace(sequences=np.array([[1, 2, 3]], dtype=np.int64)))

        with install_fake_qwen_utils("术语识别正确"):
            service._transcribe_audio_array(
                audio=np.ones(8000, dtype=np.float32),
                sample_rate=16000,
                start_time=0.0,
                context="术语：Qwen3-ASR，Codex",
            )

        self.assertEqual(
            service._model._build_text_prompt.call_args.kwargs["context"],
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
             patch("asr_service.os.path.exists", side_effect=lambda path: path in {"/tmp/fake-asr-model", "/tmp/fake-aligner-model"}), \
             patch("asr_service.load_alignment_model", return_value=fake_aligner) as load_alignment_model:
            service._load_model_sync()

        self.assertEqual(
            fake_qwen_asr.Qwen3ASRModel.from_pretrained.call_args.kwargs["low_cpu_mem_usage"],
            False,
        )
        self.assertEqual(load_alignment_model.call_args.kwargs["low_cpu_mem_usage"], False)

    def test_run_model_generate_serializes_concurrent_calls_per_service(self):
        service = ASRService(ASRConfig(model_path="/tmp/fake-asr-model"))
        fake_model = Mock()
        first_entered = threading.Event()
        allow_first_exit = threading.Event()
        second_entered = threading.Event()
        state = {
            "active": 0,
            "max_active": 0,
            "calls": 0,
        }

        def fake_generate(**_kwargs):
            state["calls"] += 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            if state["calls"] == 1:
                first_entered.set()
                allow_first_exit.wait(timeout=1.0)
            else:
                second_entered.set()
            time.sleep(0.01)
            state["active"] -= 1
            return "ok"

        fake_model.generate.side_effect = fake_generate

        def invoke_generate():
            service._run_model_generate(fake_model, {})

        thread_1 = threading.Thread(target=invoke_generate)
        thread_2 = threading.Thread(target=invoke_generate)

        thread_1.start()
        self.assertTrue(first_entered.wait(timeout=1.0))
        thread_2.start()
        time.sleep(0.05)

        self.assertFalse(second_entered.is_set())
        self.assertEqual(state["max_active"], 1)

        allow_first_exit.set()
        thread_1.join(timeout=1.0)
        thread_2.join(timeout=1.0)

        self.assertFalse(thread_1.is_alive())
        self.assertFalse(thread_2.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["max_active"], 1)

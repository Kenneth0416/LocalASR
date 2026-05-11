import unittest
from unittest.mock import AsyncMock

from asr import ASRResult, FinalOnlyASRRouter


class FinalOnlyASRRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_final_does_not_append_previous_segment_text_to_context(self):
        fake_service = type("FakeASRService", (), {})()
        fake_service.transcribe_wav = AsyncMock(
            side_effect=[
                ASRResult(text="第一句定稿。", segments=[], audio_duration=1.0, processing_time=0.01),
                ASRResult(text="第二句定稿。", segments=[], audio_duration=1.0, processing_time=0.01),
            ]
        )

        router = FinalOnlyASRRouter(
            fake_service,
            language_getter=lambda: "Chinese",
            context_getter=lambda: "术语：Codex",
        )

        await router.transcribe_final((b"pcm-1", 16000))
        await router.transcribe_final((b"pcm-2", 16000))

        first_call = fake_service.transcribe_wav.await_args_list[0]
        second_call = fake_service.transcribe_wav.await_args_list[1]

        self.assertEqual(first_call.kwargs["context"], "术语：Codex")
        self.assertEqual(second_call.kwargs["context"], "术语：Codex")

    async def test_transcribe_final_disables_timestamp_extraction_and_preserves_processing_time(self):
        fake_service = type("FakeASRService", (), {})()
        fake_service.transcribe_wav = AsyncMock(
            return_value=ASRResult(
                text="最终定稿。",
                segments=[],
                audio_duration=2.5,
                processing_time=0.42,
            )
        )

        router = FinalOnlyASRRouter(
            fake_service,
            language_getter=lambda: "Chinese",
            context_getter=lambda: None,
        )

        result = await router.transcribe_final((b"pcm", 16000))

        self.assertEqual(result.text, "最终定稿。")
        self.assertEqual(result.duration_sec, 2.5)
        self.assertEqual(result.processing_time, 0.42)
        self.assertFalse(fake_service.transcribe_wav.await_args.kwargs["extract_timestamps"])

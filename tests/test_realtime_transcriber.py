import asyncio
import unittest

import numpy as np

from asr import (
    ASRResult,
    LiteASRResult,
    RealtimeMeetingTranscriber,
    RealtimeTranscriptionConfig,
    SemanticMeetingTranscriber,
    TransformerChunkingConfig,
    WebRTCVADMeetingTranscriber,
    WebRTCVADConfig,
)


def make_pcm(*parts, sample_rate: int = 16000) -> bytes:
    chunks = []
    for duration_sec, amplitude in parts:
        samples = int(duration_sec * sample_rate)
        chunks.append(np.full(samples, amplitude, dtype=np.int16))

    if not chunks:
        return b""

    return np.concatenate(chunks).tobytes()


class FakeRouter:
    def __init__(self, preview_results=None, final_results=None):
        self.preview_results = iter(preview_results or [])
        self.final_results = iter(final_results or [])
        self.preview_calls = []
        self.final_calls = []

    async def transcribe_preview(self, audio_tuple):
        self.preview_calls.append(audio_tuple)
        return next(self.preview_results)

    async def transcribe_preview_streaming(self, audio_tuple):
        # The runtime VAD transcriber prefers streaming preview. For tests, a
        # single-shot preview result is sufficient and avoids duplicating setup.
        yield await self.transcribe_preview(audio_tuple)

    async def transcribe_final(self, audio_tuple):
        self.final_calls.append(audio_tuple)
        return next(self.final_results)


class FakeVAD:
    def __init__(self, decisions):
        self._decisions = iter(decisions)

    def is_speech(self, _pcm_chunk, _sample_rate):
        return next(self._decisions)


class WebRTCVADMeetingTranscriberTests(unittest.IsolatedAsyncioTestCase):
    def _override_preview_timing(self, transcriber, *, preview_interval_sec: float, min_preview_audio_sec: float) -> None:
        # Preview timing knobs were removed from WebRTCVADConfig as part of Task 1,
        # but tests still need deterministic timings for preview emission.
        transcriber._preview_interval_sec = float(preview_interval_sec)
        transcriber._min_preview_audio_sec = float(min_preview_audio_sec)

    async def _push_pcm_and_drain_previews(self, transcriber, pcm_chunk: bytes):
        # Preview ASR runs in a background task. Yielding lets it run while the
        # utterance is still active, so preview text can be used for fallbacks.
        events = []
        events.extend(await transcriber.push_pcm(pcm_chunk))
        await asyncio.sleep(0)
        events.extend(await transcriber.push_pcm(b""))
        return events

    async def test_utterance_starts_at_first_speech_frame(self):
        router = FakeRouter(
            final_results=[LiteASRResult(text="定稿", duration_sec=0.08)],
        )
        decisions = [True] * 4 + [False] * 8
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=4,
                max_utterance_sec=2.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=1.0, min_preview_audio_sec=1.0)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        events.extend(await transcriber.flush())

        finals = [event for event in events if event.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].start_time, 0.0)
        self.assertEqual(finals[0].end_time, 0.16)
        self.assertEqual(finals[0].text, "定稿")

    async def test_emits_preview_revisions_then_final_with_same_segment_id(self):
        router = FakeRouter(
            preview_results=[
                LiteASRResult(text="我们这周先把接口", duration_sec=0.8),
                LiteASRResult(text="我们这周先把接口和回归", duration_sec=1.2),
            ],
            final_results=[
                LiteASRResult(text="我们这周先把接口和回归走完。", duration_sec=1.4),
            ],
        )
        decisions = [True] * 70 + [False] * 12
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=8,
                max_utterance_sec=3.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=0.4, min_preview_audio_sec=0.8)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        events.extend(await transcriber.flush())

        previews = [event for event in events if not event.is_final]
        finals = [event for event in events if event.is_final]

        self.assertEqual([event.segment_id for event in previews], [1, 1])
        self.assertEqual([event.revision for event in previews], [1, 2])
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].segment_id, 1)
        self.assertEqual(finals[0].revision, 3)
        self.assertEqual(finals[0].text, "我们这周先把接口和回归走完。")

    async def test_preview_exception_is_dropped_and_final_still_emits(self):
        class Router:
            def __init__(self):
                self.preview_calls = 0

            async def transcribe_preview(self, _audio_tuple):
                self.preview_calls += 1
                if self.preview_calls == 1:
                    raise RuntimeError("preview model hiccup")
                return LiteASRResult(text="预览恢复", duration_sec=1.0)

            async def transcribe_preview_streaming(self, audio_tuple):
                yield await self.transcribe_preview(audio_tuple)

            async def transcribe_final(self, _audio_tuple):
                return LiteASRResult(text="最终定稿", duration_sec=1.4)

        decisions = [True] * 55 + [False] * 8
        transcriber = WebRTCVADMeetingTranscriber(
            router=Router(),
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=4,
                max_utterance_sec=3.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=0.4, min_preview_audio_sec=0.8)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        events.extend(await transcriber.flush())

        finals = [event for event in events if event.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "最终定稿")
        self.assertEqual(finals[0].segment_id, 1)

    async def test_preview_exceptions_are_rate_limited_by_preview_cadence(self):
        class Router:
            def __init__(self):
                self.preview_calls = 0

            async def transcribe_preview(self, _audio_tuple):
                self.preview_calls += 1
                raise RuntimeError("preview model still down")

            async def transcribe_preview_streaming(self, audio_tuple):
                yield await self.transcribe_preview(audio_tuple)

            async def transcribe_final(self, _audio_tuple):
                return LiteASRResult(text="最终定稿", duration_sec=1.4)

        decisions = [True] * 80 + [False] * 8
        router = Router()
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=4,
                max_utterance_sec=4.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=0.4, min_preview_audio_sec=0.8)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        events.extend(await transcriber.flush())

        finals = [event for event in events if event.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "最终定稿")
        self.assertLessEqual(router.preview_calls, 4)

    async def test_pending_final_does_not_block_next_utterance(self):
        preview = LiteASRResult(text="第一句预览", duration_sec=0.8)
        final_1 = asyncio.Future()
        final_2 = asyncio.Future()
        final_2.set_result(LiteASRResult(text="第二句定稿。", duration_sec=0.9))

        class Router:
            async def transcribe_preview(self, _audio_tuple):
                return preview

            async def transcribe_preview_streaming(self, audio_tuple):
                yield await self.transcribe_preview(audio_tuple)

            async def transcribe_final(self, _audio_tuple):
                if not hasattr(self, "called"):
                    self.called = 1
                    return await final_1
                return await final_2

        decisions = [True] * 50 + [False] * 10 + [True] * 50 + [False] * 10
        transcriber = WebRTCVADMeetingTranscriber(
            router=Router(),
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=8,
                max_utterance_sec=3.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=0.4, min_preview_audio_sec=0.8)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        self.assertEqual([event for event in events if event.is_final], [])

        final_1.set_result(LiteASRResult(text="第一句定稿。", duration_sec=1.5))
        events.extend(await transcriber.flush(reason="flush"))

        finals = [event for event in events if event.is_final]
        self.assertEqual([event.segment_id for event in finals], [1, 2])
        self.assertEqual([event.text for event in finals], ["第一句定稿。", "第二句定稿。"])

    async def test_final_falls_back_to_last_non_empty_preview_text(self):
        router = FakeRouter(
            preview_results=[LiteASRResult(text="还有半句", duration_sec=0.8)],
            final_results=[LiteASRResult(text="", duration_sec=1.0)],
        )
        decisions = [True] * 45 + [False] * 12
        transcriber = WebRTCVADMeetingTranscriber(
            router=router,
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=8,
                max_utterance_sec=3.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=0.4, min_preview_audio_sec=0.8)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        events.extend(await transcriber.flush())

        finals = [event for event in events if event.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "还有半句")
        self.assertEqual(finals[0].cut_reason, "final_fallback")

    async def test_final_exception_falls_back_to_last_non_empty_preview_text(self):
        class Router:
            async def transcribe_preview(self, _audio_tuple):
                return LiteASRResult(text="异常时保留", duration_sec=0.8)

            async def transcribe_preview_streaming(self, audio_tuple):
                yield await self.transcribe_preview(audio_tuple)

            async def transcribe_final(self, _audio_tuple):
                raise RuntimeError("final model failed")

        decisions = [True] * 45 + [False] * 8
        transcriber = WebRTCVADMeetingTranscriber(
            router=Router(),
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=4,
                max_utterance_sec=3.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=0.4, min_preview_audio_sec=0.8)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        events.extend(await transcriber.flush())

        finals = [event for event in events if event.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "异常时保留")
        self.assertEqual(finals[0].cut_reason, "final_fallback")

    async def test_flush_finalizes_single_speech_frame_tail(self):
        class Router:
            async def transcribe_preview(self, _audio_tuple):
                return LiteASRResult(text="单帧尾巴", duration_sec=0.02)

            async def transcribe_preview_streaming(self, audio_tuple):
                yield await self.transcribe_preview(audio_tuple)

            async def transcribe_final(self, _audio_tuple):
                return LiteASRResult(text="单帧尾巴", duration_sec=0.02)

        decisions = [True] + [False] * 6
        transcriber = WebRTCVADMeetingTranscriber(
            router=Router(),
            config=WebRTCVADConfig(
                frame_ms=20,
                enter_speech_frames=2,
                endpoint_silence_frames=4,
                max_utterance_sec=3.0,
            ),
            vad_factory=lambda aggressiveness: FakeVAD(decisions),
        )
        self._override_preview_timing(transcriber, preview_interval_sec=0.4, min_preview_audio_sec=0.8)

        events = []
        for _ in range(len(decisions)):
            events.extend(await self._push_pcm_and_drain_previews(transcriber, make_pcm((0.02, 1800))))
        events.extend(await transcriber.flush(reason="flush"))

        finals = [event for event in events if event.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "单帧尾巴")
        self.assertEqual(finals[0].cut_reason, "flush")


class SemanticMeetingTranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracks_monotonic_timeline_across_overlap_and_flush(self):
        responses = iter([
            ASRResult(
                text="今天我们讨论预算",
                segments=[],
                audio_duration=4.0,
                processing_time=0.02,
            ),
            ASRResult(
                text="最后补一句",
                segments=[],
                audio_duration=1.6,
                processing_time=0.03,
            ),
        ])

        async def fake_transcribe(_wav_bytes):
            return next(responses)

        transcriber = SemanticMeetingTranscriber(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        chunk_events = await transcriber.push_pcm(make_pcm((3.6, 1800), (0.6, 0)))
        tail_events = await transcriber.push_pcm(make_pcm((1.4, 1800)))
        final_events = await transcriber.flush()

        self.assertEqual(len(chunk_events), 1)
        self.assertEqual(chunk_events[0].text, "今天我们讨论预算")
        self.assertEqual(chunk_events[0].cut_reason, "semantic")
        self.assertAlmostEqual(chunk_events[0].start_time, 0.0, places=2)
        self.assertAlmostEqual(chunk_events[0].end_time, 4.0, places=1)

        self.assertEqual(tail_events, [])
        self.assertEqual(len(final_events), 1)
        self.assertEqual(final_events[0].text, "最后补一句")
        self.assertEqual(final_events[0].cut_reason, "flush")
        self.assertAlmostEqual(final_events[0].start_time, 4.0, places=1)
        self.assertAlmostEqual(final_events[0].end_time, 5.6, places=1)

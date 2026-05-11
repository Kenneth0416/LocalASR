import asyncio
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from asr_types import LiteASRResult, RealtimeTranscriptEvent
from config import SileroVADConfig


def make_pcm(*parts, sample_rate=16000):
    chunks = []
    for duration_sec, amplitude in parts:
        samples = int(duration_sec * sample_rate)
        chunks.append(np.full(samples, amplitude, dtype=np.int16))
    return np.concatenate(chunks).tobytes() if chunks else b""


class FakeRouter:
    def __init__(self, final_results=None):
        self.final_results = iter(final_results or [])
        self.final_calls = []

    async def transcribe_final(self, audio_tuple):
        self.final_calls.append(audio_tuple)
        return next(self.final_results)


class FakeSileroModel:
    """Fake Silero VAD model that returns configurable probabilities."""
    def __init__(self, probabilities):
        self._probs = iter(probabilities)

    def reset_states(self):
        pass

    def __call__(self, chunk, sr):
        prob = next(self._probs)
        result = MagicMock()
        result.item.return_value = prob
        return result


class SileroVADTranscriberTests(unittest.IsolatedAsyncioTestCase):
    @patch("vad.load_silero_vad")
    async def test_utterance_starts_after_enter_speech_frames(self, mock_load):
        """Speech must persist for enter_speech_frames before utterance begins."""
        from vad import SileroVADTranscriber

        # 3 speech frames, then silence
        probs = [0.8, 0.9, 0.85, 0.1, 0.1, 0.1, 0.1, 0.1]
        mock_load.return_value = FakeSileroModel(probs)

        router = FakeRouter(
            final_results=[LiteASRResult(text="hello", duration_sec=0.16, processing_time=0.01)]
        )
        transcriber = SileroVADTranscriber(
            router=router,
            config=SileroVADConfig(
                speech_threshold=0.5,
                enter_speech_frames=2,
                endpoint_silence_frames=3,
                max_utterance_sec=5.0,
            ),
        )

        # 512 samples = 32ms at 16kHz
        frame_pcm = make_pcm((0.032, 1800))
        events = []
        for _ in range(8):
            events.extend(await transcriber.push_frame(frame_pcm, sample_count=512))
        events.extend(await transcriber.flush())

        finals = [e for e in events if e.is_final]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, "hello")

    @patch("vad.load_silero_vad")
    async def test_below_threshold_treated_as_silence(self, mock_load):
        """Probabilities below threshold should count as silence."""
        from vad import SileroVADTranscriber

        # All below threshold
        probs = [0.2, 0.1, 0.3, 0.1]
        mock_load.return_value = FakeSileroModel(probs)

        router = FakeRouter()
        transcriber = SileroVADTranscriber(
            router=router,
            config=SileroVADConfig(speech_threshold=0.5, endpoint_silence_frames=2),
        )

        frame_pcm = make_pcm((0.032, 500))
        events = []
        for _ in range(4):
            events.extend(await transcriber.push_frame(frame_pcm, sample_count=512))
        events.extend(await transcriber.flush())

        self.assertEqual(len(router.final_calls), 0)

    @patch("vad.load_silero_vad")
    async def test_pipeline_stats(self, mock_load):
        """pipeline_stats() should return dict with expected keys."""
        from vad import SileroVADTranscriber

        mock_load.return_value = FakeSileroModel([])
        transcriber = SileroVADTranscriber(
            router=FakeRouter(),
            config=SileroVADConfig(),
        )
        stats = transcriber.pipeline_stats()
        self.assertIn("asr_in_flight", stats)
        self.assertIn("asr_queued", stats)
        self.assertIn("active_utterance", stats)


if __name__ == "__main__":
    unittest.main()

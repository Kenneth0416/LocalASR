import unittest

import numpy as np

from asr import ASRResult, TransformerAudioChunker, TransformerChunkingConfig


def make_pcm(*parts, sample_rate: int = 16000) -> bytes:
    chunks = []
    for duration_sec, amplitude in parts:
        samples = int(duration_sec * sample_rate)
        chunks.append(np.full(samples, amplitude, dtype=np.int16))

    if not chunks:
        return b""

    return np.concatenate(chunks).tobytes()


class TransformerAudioChunkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_hard_cut_at_three_seconds(self):
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return ASRResult(
                text="不应该这么早触发",
                segments=[],
                audio_duration=0.0,
                processing_time=0.01,
            )

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        emissions = await chunker.push_pcm(make_pcm((3.2, 1400)))

        self.assertEqual(emissions, [])
        self.assertEqual(calls, 0)

    async def test_emits_after_pause_once_minimum_context_is_reached(self):
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return ASRResult(
                text="这是第一段稳定内容",
                segments=[],
                audio_duration=0.0,
                processing_time=0.02,
            )

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        emissions = await chunker.push_pcm(
            make_pcm((3.6, 1800), (0.6, 0))
        )

        self.assertEqual(calls, 1)
        self.assertEqual([emission.text for emission in emissions], ["这是第一段稳定内容"])

    async def test_overlap_deduplicates_following_chunk_text(self):
        responses = iter([
            "今天讨论预算和风险",
            "预算和风险以及排期",
        ])
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return ASRResult(
                text=next(responses),
                segments=[],
                audio_duration=0.0,
                processing_time=0.03,
            )

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        first_emissions = await chunker.push_pcm(
            make_pcm((7.6, 1800), (0.5, 0), (4.5, 1800))
        )
        final_emissions = await chunker.flush()

        self.assertEqual(calls, 2)
        self.assertEqual([emission.text for emission in first_emissions], ["今天讨论预算和风险"])
        self.assertEqual([emission.text for emission in final_emissions], ["以及排期"])

    async def test_overlap_deduplicates_partial_repeat_with_punctuation_difference(self):
        responses = iter([
            "对不对？哎，你们从。",
            "哎，你们从苏州回来啦？我在济南。",
        ])
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return ASRResult(
                text=next(responses),
                segments=[],
                audio_duration=0.0,
                processing_time=0.03,
            )

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        first_emissions = await chunker.push_pcm(
            make_pcm((7.6, 1800), (0.5, 0), (4.5, 1800))
        )
        final_emissions = await chunker.flush()

        self.assertEqual(calls, 2)
        self.assertEqual([emission.text for emission in first_emissions], ["对不对？哎，你们从。"])
        self.assertEqual([emission.text for emission in final_emissions], ["苏州回来啦？我在济南。"])

    async def test_semantic_commit_keeps_unfinished_suffix_buffered(self):
        responses = iter([
            ASRResult(
                text="哎，你们从",
                segments=[
                    {"start": 0.0, "end": 0.4, "text": "哎"},
                    {"start": 0.4, "end": 0.55, "text": "，"},
                    {"start": 0.55, "end": 0.95, "text": "你们"},
                    {"start": 0.95, "end": 1.35, "text": "从"},
                ],
                audio_duration=8.0,
                processing_time=0.03,
                has_timestamps=True,
            ),
            ASRResult(
                text="哎，你们从苏州回来啦？",
                segments=[
                    {"start": 0.0, "end": 0.4, "text": "哎"},
                    {"start": 0.4, "end": 0.55, "text": "，"},
                    {"start": 0.55, "end": 0.95, "text": "你们"},
                    {"start": 0.95, "end": 1.35, "text": "从"},
                    {"start": 1.35, "end": 1.9, "text": "苏州"},
                    {"start": 1.9, "end": 2.55, "text": "回来啦"},
                    {"start": 2.55, "end": 2.75, "text": "？"},
                ],
                audio_duration=10.5,
                processing_time=0.03,
                has_timestamps=True,
            ),
        ])
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return next(responses)

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        first_emissions = await chunker.push_pcm(
            make_pcm((12.5, 1800))
        )
        final_emissions = await chunker.flush()

        self.assertEqual(calls, 2)
        self.assertEqual(first_emissions, [])
        self.assertEqual([emission.text for emission in final_emissions], ["哎，你们从苏州回来啦？"])

    async def test_final_flush_falls_back_to_full_result_text_when_timestamps_are_partial(self):
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return ASRResult(
                text="这是完整的一分钟口播内容，不应该只保留开头那一句。",
                segments=[
                    {"start": 0.0, "end": 1.1, "text": "这是"},
                    {"start": 1.1, "end": 1.9, "text": "开头"},
                    {"start": 1.9, "end": 2.8, "text": "那句。"},
                ],
                audio_duration=5.0,
                processing_time=0.02,
                has_timestamps=True,
            )

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        first_emissions = await chunker.push_pcm(make_pcm((5.0, 1800)))
        final_emissions = await chunker.flush()

        self.assertEqual(calls, 1)
        self.assertEqual(first_emissions, [])
        self.assertEqual(
            [emission.text for emission in final_emissions],
            ["这是完整的一分钟口播内容，不应该只保留开头那一句。"],
        )

    async def test_semantic_commit_emits_only_completed_sentence_prefix(self):
        responses = iter([
            ASRResult(
                text="对不对？哎，你们从",
                segments=[
                    {"start": 0.0, "end": 0.35, "text": "对"},
                    {"start": 0.35, "end": 0.7, "text": "不"},
                    {"start": 0.7, "end": 1.0, "text": "对"},
                    {"start": 1.0, "end": 1.2, "text": "？"},
                    {"start": 7.0, "end": 7.15, "text": "哎"},
                    {"start": 7.15, "end": 7.25, "text": "，"},
                    {"start": 7.25, "end": 7.55, "text": "你们"},
                    {"start": 7.55, "end": 7.85, "text": "从"},
                ],
                audio_duration=8.0,
                processing_time=0.03,
                has_timestamps=True,
            ),
            ASRResult(
                text="哎，你们从苏州回来啦？我在济南。",
                segments=[
                    {"start": 0.0, "end": 0.35, "text": "哎"},
                    {"start": 0.35, "end": 0.5, "text": "，"},
                    {"start": 0.5, "end": 0.95, "text": "你们"},
                    {"start": 0.95, "end": 1.4, "text": "从"},
                    {"start": 1.4, "end": 1.95, "text": "苏州"},
                    {"start": 1.95, "end": 2.6, "text": "回来啦"},
                    {"start": 2.6, "end": 2.8, "text": "？"},
                    {"start": 3.1, "end": 3.45, "text": "我"},
                    {"start": 3.45, "end": 3.75, "text": "在"},
                    {"start": 3.75, "end": 4.25, "text": "济南"},
                    {"start": 4.25, "end": 4.45, "text": "。"},
                ],
                audio_duration=10.5,
                processing_time=0.03,
                has_timestamps=True,
            ),
        ])
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return next(responses)

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        first_emissions = await chunker.push_pcm(
            make_pcm((7.6, 1800), (0.5, 0), (4.5, 1800))
        )
        final_emissions = await chunker.flush()

        self.assertEqual(calls, 2)
        self.assertEqual([emission.text for emission in first_emissions], ["对不对？"])
        self.assertEqual([emission.text for emission in final_emissions], ["哎，你们从苏州回来啦？我在济南。"])

    async def test_semantic_commit_emits_single_character_after_long_pause(self):
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return ASRResult(
                text="嗯",
                segments=[
                    {"start": 0.0, "end": 0.35, "text": "嗯"},
                ],
                audio_duration=4.2,
                processing_time=0.01,
                has_timestamps=True,
            )

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        emissions = await chunker.push_pcm(
            make_pcm((0.3, 1800), (3.9, 0))
        )

        self.assertEqual(calls, 1)
        self.assertEqual([emission.text for emission in emissions], ["嗯"])

    async def test_semantic_commit_emits_three_char_fragment_after_long_pause(self):
        responses = iter([
            ASRResult(
                text="你们从",
                segments=[
                    {"start": 0.0, "end": 0.35, "text": "你"},
                    {"start": 0.35, "end": 0.7, "text": "们"},
                    {"start": 0.7, "end": 1.05, "text": "从"},
                ],
                audio_duration=4.3,
                processing_time=0.01,
                has_timestamps=True,
            ),
            ASRResult(
                text="你们从",
                segments=[
                    {"start": 0.0, "end": 0.35, "text": "你"},
                    {"start": 0.35, "end": 0.7, "text": "们"},
                    {"start": 0.7, "end": 1.05, "text": "从"},
                ],
                audio_duration=4.3,
                processing_time=0.01,
                has_timestamps=True,
            ),
        ])
        calls = 0

        async def fake_transcribe(_wav_bytes):
            nonlocal calls
            calls += 1
            return next(responses)

        chunker = TransformerAudioChunker(
            transcribe_wav=fake_transcribe,
            config=TransformerChunkingConfig(),
        )

        emissions = await chunker.push_pcm(
            make_pcm((1.0, 1800), (3.3, 0))
        )
        final_emissions = await chunker.flush()

        self.assertEqual(calls, 2)
        self.assertEqual([emission.text for emission in emissions], ["你们从"])
        self.assertEqual(final_emissions, [])

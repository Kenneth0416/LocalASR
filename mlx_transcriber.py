"""
MLX Transcriber — Apple Silicon native ASR via mlx-audio.

Uses per-chunk independent token budget (no shared budget across chunks),
120-second energy-boundary chunking, and "".join() for Chinese text merging.

Generation uses mlx_lm sampling utilities to avoid repetition loops:
  - temperature > 0: enables sampling instead of greedy argmax
  - repetition_penalty > 1: penalizes recently generated tokens

Enabled via ASR_UPLOAD_USE_MLX=1 environment variable.
"""

import logging
import time
from typing import Optional

import numpy as np

from text_utils import strip_hallucination

logger = logging.getLogger("meeting.mlx")

DEFAULT_MLX_MODEL = "mlx-community/Qwen3-ASR-1.7B-4bit"


class MLXTranscriber:
    """Lightweight wrapper around mlx-audio Qwen3-ASR for batch transcription."""

    def __init__(
        self,
        model_path: str = "",
        max_new_tokens: int = 2048,
        chunk_sec: float = 120.0,
        search_expand_sec: float = 15.0,
        temperature: float = 0.0,
        repetition_penalty: float = 0.0,
        repetition_context_size: int = 100,
    ):
        self.model_path = model_path or DEFAULT_MLX_MODEL
        self.max_new_tokens = max_new_tokens
        self.chunk_sec = chunk_sec
        self.search_expand_sec = search_expand_sec
        self.temperature = temperature
        self.repetition_penalty = repetition_penalty
        self.repetition_context_size = repetition_context_size
        self._model = None
        self._sr: int = 16000

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self):
        """Load the mlx-audio model. Call from a thread (CPU/GPU bound)."""
        if self._model is not None:
            return

        logger.info("Loading MLX model: %s", self.model_path)
        from mlx_audio.stt.utils import load_model

        t0 = time.time()
        self._model = load_model(self.model_path)
        self._sr = self._model.sample_rate
        logger.info("MLX model loaded in %.1fs", time.time() - t0)

    def transcribe(
        self,
        audio_path: str,
        language: Optional[str] = None,
    ) -> list[dict]:
        """
        Transcribe a WAV file using per-chunk independent token budget.

        Returns:
            list of {"start": float, "end": float, "text": str}
        """
        import mlx.core as mx
        from mlx_audio.audio_io import read as audio_read

        raw_audio, sr = audio_read(audio_path, always_2d=True)
        if sr != self._sr:
            from mlx_audio.audio_utils import resample_audio
            raw_audio = resample_audio(raw_audio, sr, self._sr)
        audio_np = mx.array(raw_audio, dtype=mx.float32).mean(axis=1)
        audio_np = np.array(audio_np)

        return self.transcribe_audio(audio_np, self._sr, language)

    def transcribe_audio(
        self,
        audio_np: np.ndarray,
        sr: int,
        language: Optional[str] = None,
    ) -> list[dict]:
        """
        Transcribe a numpy audio array directly.

        Returns:
            list of {"start": float, "end": float, "text": str}
        """
        if self._model is None:
            self.load()

        import mlx.core as mx
        from mlx_lm.sample_utils import make_logits_processors, make_sampler

        # Ensure mono float32 at correct sample rate
        if sr != self._sr:
            from mlx_audio.audio_utils import resample_audio
            audio_2d = audio_np.reshape(-1, 1) if audio_np.ndim == 1 else audio_np
            audio_2d = resample_audio(audio_2d, sr, self._sr)
            audio_np = np.array(mx.array(audio_2d, dtype=mx.float32).mean(axis=1))
        elif audio_np.ndim > 1:
            audio_np = np.array(mx.array(audio_np, dtype=mx.float32).mean(axis=1))

        # Build sampler and logits processors once
        sampler = make_sampler(self.temperature)
        logits_processors = None
        if self.repetition_penalty > 1.0:
            logits_processors = make_logits_processors(
                repetition_penalty=self.repetition_penalty,
                repetition_context_size=self.repetition_context_size,
            )
            logger.info(
                "MLX sampling: temperature=%.2f, repetition_penalty=%.2f, context=%d",
                self.temperature,
                self.repetition_penalty,
                self.repetition_context_size,
            )
        elif self.temperature > 0.0:
            logger.info("MLX sampling: temperature=%.2f", self.temperature)
        else:
            logger.info("MLX sampling: greedy (temperature=0)")

        # Split into chunks at energy minima
        chunks = self._split_energy_chunks(audio_np)
        logger.info(
            "MLX: %.1fs audio → %d chunks (~%.0fs each)",
            len(audio_np) / self._sr,
            len(chunks),
            self.chunk_sec,
        )

        # Transcribe each chunk independently
        segments: list[dict] = []
        wall_start = time.time()

        for i, (chunk_audio, offset_sec) in enumerate(chunks):
            chunk_dur = len(chunk_audio) / self._sr

            text, _prompt_toks, gen_toks = self._model._generate_single_chunk(
                chunk_audio,
                max_tokens=self.max_new_tokens,
                sampler=sampler,
                logits_processors=logits_processors,
                language=language,
                prefill_step_size=2048,
                verbose=False,
            )

            text = text.strip()
            # Strip MLX model's language prefix (e.g. "language Cantonese<asr_text>")
            if "<asr_text>" in text:
                text = text.split("<asr_text>", 1)[-1]
            text = strip_hallucination(text)
            if text:
                segments.append(
                    {
                        "start": offset_sec,
                        "end": offset_sec + chunk_dur,
                        "text": text,
                    }
                )

            mx.clear_cache()

            if (i + 1) % 10 == 0 or i == len(chunks) - 1:
                logger.debug(
                    "MLX chunk %d/%d: %.1fmin-%.1fmin, %d chars, %d tokens",
                    i + 1,
                    len(chunks),
                    offset_sec / 60,
                    (offset_sec + chunk_dur) / 60,
                    len(text),
                    gen_toks,
                )

        total_chars = sum(len(s["text"]) for s in segments)
        elapsed = time.time() - wall_start
        audio_dur = len(audio_np) / self._sr
        logger.info(
            "MLX done: %d chars, %.1fs (%.1f min), RTF=%.3f",
            total_chars,
            elapsed,
            elapsed / 60,
            elapsed / audio_dur if audio_dur > 0 else 0,
        )

        return segments

    def _split_energy_chunks(
        self,
        audio: np.ndarray,
    ) -> list[tuple[np.ndarray, float]]:
        """Split audio at energy minima into ~chunk_sec chunks."""
        sr = self._sr
        total_sec = len(audio) / sr

        if total_sec <= self.chunk_sec:
            return [(audio, 0.0)]

        chunk_samples = int(self.chunk_sec * sr)
        expand = int(self.search_expand_sec * sr)
        win = max(4, int(0.05 * sr))  # 50ms window

        chunks: list[tuple[np.ndarray, float]] = []
        start = 0

        while start < len(audio):
            cut = start + chunk_samples
            if cut >= len(audio):
                chunks.append((audio[start:], start / sr))
                break

            left = max(start, cut - expand)
            right = min(len(audio), cut + expand)
            seg = audio[left:right]

            if right - left <= win:
                cut_sample = cut
            else:
                energy = np.convolve(seg ** 2, np.ones(win) / win, mode="valid")
                best = int(np.argmin(energy))
                cut_sample = left + best + win // 2

            chunks.append((audio[start:cut_sample], start / sr))
            start = cut_sample

        return chunks

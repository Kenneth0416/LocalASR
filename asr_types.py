"""
ASR shared types - dataclasses used across all modules.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np


def pcm16le_to_audio_tuple(pcm_data: bytes, sample_rate: int) -> tuple[np.ndarray, int]:
    """Convert mono 16-bit PCM bytes into the waveform tuple accepted by Qwen3-ASR."""
    if not pcm_data:
        return np.zeros((0,), dtype=np.float32), sample_rate
    pcm = np.frombuffer(pcm_data, dtype="<i2").astype(np.float32)
    return pcm / 32768.0, sample_rate


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass
class ASRResult:
    """ASR Transcription Result"""
    text: str
    segments: list[dict]
    audio_duration: float
    processing_time: float
    has_timestamps: bool = False


@dataclass(frozen=True)
class LiteASRResult:
    text: str
    duration_sec: float
    processing_time: float = 0.0
    language: str | None = None
    confidence: float | None = None


# ── Realtime transcript events ────────────────────────────────────────────────


@dataclass(frozen=True)
class RealtimeTranscriptEvent:
    """A finalized realtime transcript emitted by the VAD flow."""
    event_type: str
    text: str
    start_time: float
    end_time: float
    processing_time: float
    segment_id: int
    revision: int
    is_final: bool
    capture_start_time: float = 0.0
    capture_duration: float = 0.0
    cut_reason: str = "endpoint"


@dataclass(frozen=True)
class RealtimeTranscriptionRequest:
    """Immutable audio snapshot queued for async transcription."""
    event_type: str
    segment_id: int
    revision: int
    is_final: bool
    audio_pcm: bytes
    start_time: float
    end_time: float
    cut_reason: str = "preview"


# ── Chunker types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChunkedASREmission:
    """A finalized transcript emission produced by the streaming chunker."""
    text: str
    audio_duration: float
    processing_time: float


@dataclass(frozen=True)
class SemanticCommit:
    """A time-aligned prefix that is safe to commit to the transcript."""
    text: str
    committed_bytes: int
    audio_duration: float
    should_wait: bool = False


# ── VAD state types ──────────────────────────────────────────────────────────


@dataclass
class UtteranceState:
    """Tracks in-progress speech utterance within the VAD state machine."""
    segment_id: int
    revision: int
    start_sample: int
    capture_start_sample: int
    end_sample: int | None
    last_speech_sample: int
    pcm_buffer: bytearray = field(default_factory=bytearray)
    sealed: bool = False
    final_task: Optional["asyncio.Task"] = None


@dataclass(frozen=True)
class BufferedAudioFrame:
    """A fixed-size frame tracked by the realtime VAD state machine."""
    pcm: bytes
    sample_count: int
    is_speech: bool
    start_sample: int
    end_sample: int


# ── Config types ──────────────────────────────────────────────────────────────


@dataclass
class RealtimeTranscriptionConfig:
    """
    MVP meeting transcription strategy.

    This mode deliberately avoids VAD, preview revisions, and overlap logic.
    Audio is buffered into fixed-length chunks and every completed chunk is
    transcribed as a final segment. Any tail audio is transcribed on flush.
    """
    frame_ms: float = 20.0
    chunk_sec: float = 4.0
    min_flush_sec: float = 0.1
    pre_roll_sec: float = 0.4
    min_segment_sec: float = 0.25
    min_speech_sec: float = 0.12
    enter_speech_sec: float = 0.04
    endpoint_silence_sec: float = 0.65
    max_segment_sec: float = 10.0
    max_segment_grace_sec: float = 0.4
    overlap_sec: float = 0.4
    silence_threshold: float = 0.005
    speech_peak_multiplier: float = 2.5
    dedupe_tail_chars: int = 120
    min_dedupe_overlap_chars: int = 4

    def __post_init__(self):
        for name, _ in [
            ("frame_ms", 0), ("chunk_sec", 0), ("min_segment_sec", 0),
            ("min_speech_sec", 0), ("enter_speech_sec", 0),
            ("endpoint_silence_sec", 0), ("silence_threshold", 0),
            ("speech_peak_multiplier", 0), ("dedupe_tail_chars", 0),
            ("min_dedupe_overlap_chars", 0),
        ]:
            v = getattr(self, name)
            if v <= 0:
                raise ValueError(f"{name} must be > 0, got {v}")
        for name, _ in [("min_flush_sec", 0), ("pre_roll_sec", 0), ("max_segment_grace_sec", 0)]:
            v = getattr(self, name)
            if v < 0:
                raise ValueError(f"{name} must be >= 0, got {v}")


@dataclass
class TransformerChunkingConfig:
    """
    Chunking strategy tuned for transformer-style ASR models.

    Instead of hard-cutting every fixed interval, the chunker:
    - waits for a meaningful amount of context,
    - prefers cutting on low-energy boundaries,
    - keeps an overlap tail for the next decode to preserve context.
    """
    min_chunk_sec: float = 4.0
    preferred_chunk_sec: float = 8.0
    max_chunk_sec: float = 12.0
    overlap_sec: float = 1.5
    endpoint_silence_sec: float = 0.45
    boundary_search_sec: float = 1.25
    energy_window_ms: float = 120.0
    silence_threshold: float = 0.012
    semantic_pause_sec: float = 0.45
    semantic_soft_pause_sec: float = 0.6
    semantic_short_pause_sec: float = 1.2
    semantic_right_guard_sec: float = 0.35
    semantic_force_commit_sec: float = 18.0
    semantic_min_chars: int = 6
    semantic_short_utterance_chars: int = 3
    dedupe_tail_chars: int = 120
    min_dedupe_overlap_chars: int = 4

    def __post_init__(self):
        if self.min_chunk_sec <= 0:
            raise ValueError("min_chunk_sec must be > 0")
        if self.preferred_chunk_sec < self.min_chunk_sec:
            raise ValueError("preferred_chunk_sec must be >= min_chunk_sec")
        if self.max_chunk_sec < self.preferred_chunk_sec:
            raise ValueError("max_chunk_sec must be >= preferred_chunk_sec")
        if self.overlap_sec < 0:
            raise ValueError("overlap_sec must be >= 0")
        if self.endpoint_silence_sec <= 0:
            raise ValueError("endpoint_silence_sec must be > 0")
        if self.boundary_search_sec <= 0:
            raise ValueError("boundary_search_sec must be > 0")
        if self.energy_window_ms <= 0:
            raise ValueError("energy_window_ms must be > 0")
        if self.semantic_pause_sec <= 0:
            raise ValueError("semantic_pause_sec must be > 0")
        if self.semantic_soft_pause_sec < self.semantic_pause_sec:
            raise ValueError("semantic_soft_pause_sec must be >= semantic_pause_sec")
        if self.semantic_short_pause_sec < self.semantic_soft_pause_sec:
            raise ValueError("semantic_short_pause_sec must be >= semantic_soft_pause_sec")
        if self.semantic_right_guard_sec < 0:
            raise ValueError("semantic_right_guard_sec must be >= 0")
        if self.semantic_force_commit_sec < self.max_chunk_sec:
            raise ValueError("semantic_force_commit_sec must be >= max_chunk_sec")
        if self.semantic_min_chars <= 0:
            raise ValueError("semantic_min_chars must be > 0")
        if self.semantic_short_utterance_chars <= 0:
            raise ValueError("semantic_short_utterance_chars must be > 0")
        if self.dedupe_tail_chars <= 0:
            raise ValueError("dedupe_tail_chars must be > 0")
        if self.min_dedupe_overlap_chars <= 0:
            raise ValueError("min_dedupe_overlap_chars must be > 0")


# ── Router interface ──────────────────────────────────────────────────────────


class BaseASRRouter:
    """Interface that realtime ASR routers must implement."""

    async def transcribe_final(self, audio_tuple) -> LiteASRResult:
        """Transcribe for final committed transcript."""
        raise NotImplementedError

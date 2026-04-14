"""
ASR Service - Non-blocking Qwen3-ASR transcription.
Runs in background thread to not block the event loop.
"""

import asyncio
import logging
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Awaitable, Callable, Optional

from utils.audio_transcoder import transcode_to_wav_pcm, TranscodingError

import numpy as np
import torch

from alignment import load_alignment_model
from config import ASRConfig, WebRTCVADConfig

logger = logging.getLogger("meeting.asr")

# Global executor for CPU-bound ASR tasks
_asr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr-worker")


def pcm16le_to_audio_tuple(pcm_data: bytes, sample_rate: int) -> tuple[np.ndarray, int]:
    """Convert mono 16-bit PCM bytes into the waveform tuple accepted by Qwen3-ASR."""
    if not pcm_data:
        return np.zeros((0,), dtype=np.float32), sample_rate

    pcm = np.frombuffer(pcm_data, dtype="<i2").astype(np.float32)
    return pcm / 32768.0, sample_rate


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
    language: str | None = None
    confidence: float | None = None


@dataclass
class UtteranceState:
    segment_id: int
    revision: int
    start_sample: int
    end_sample: int | None
    last_speech_sample: int
    pcm_buffer: bytearray = field(default_factory=bytearray)
    first_preview_emitted: bool = False
    last_preview_sample: int = 0
    sealed: bool = False
    final_task: asyncio.Task | None = None
    preview_task: asyncio.Task | None = None
    last_non_empty_preview_text: str = ""


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


@dataclass(frozen=True)
class RealtimeTranscriptEvent:
    """A realtime transcript update emitted by the streaming flow."""

    event_type: str  # "preview" or "final"
    text: str
    start_time: float
    end_time: float
    processing_time: float
    segment_id: int
    revision: int
    is_final: bool
    cut_reason: str = "preview"


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


class FinalOnlyASRRouter:
    """ASR router that only performs final transcription (no preview).
    Used when preview ASR is disabled via DISABLE_PREVIEW_ASR env var.
    """

    def __init__(
        self,
        final_service: "ASRService",
        *,
        language_getter: Callable[[], str | None],
        context_getter: Callable[[], str | None],
    ):
        self._final_service = final_service
        self._language_getter = language_getter
        self._context_getter = context_getter

    async def transcribe_preview(self, audio_tuple) -> LiteASRResult:
        """Return empty preview result (preview is disabled)."""
        return LiteASRResult(text="", duration_sec=0.0)

    async def transcribe_final(self, audio_tuple) -> LiteASRResult:
        result = await self._final_service.transcribe_wav(
            audio_tuple,
            language=self._language_getter(),
            context=self._context_getter(),
        )
        return LiteASRResult(
            text=(result.text or "").strip(),
            duration_sec=float(getattr(result, "audio_duration", 0.0) or 0.0),
        )


class PreviewFinalASRRouter:
    def __init__(
        self,
        preview_service: "ASRService",
        final_service: "ASRService",
        *,
        language_getter: Callable[[], str | None],
        context_getter: Callable[[], str | None],
    ):
        self._preview_service = preview_service
        self._final_service = final_service
        self._language_getter = language_getter
        self._context_getter = context_getter

    async def transcribe_preview(self, audio_tuple) -> LiteASRResult:
        result = await self._preview_service.transcribe_wav(
            audio_tuple,
            language=self._language_getter(),
            context=self._context_getter(),
        )
        return LiteASRResult(
            text=(result.text or "").strip(),
            duration_sec=float(getattr(result, "audio_duration", 0.0) or 0.0),
        )

    async def transcribe_final(self, audio_tuple) -> LiteASRResult:
        result = await self._final_service.transcribe_wav(
            audio_tuple,
            language=self._language_getter(),
            context=self._context_getter(),
        )
        return LiteASRResult(
            text=(result.text or "").strip(),
            duration_sec=float(getattr(result, "audio_duration", 0.0) or 0.0),
        )


class WebRTCVADMeetingTranscriber:
    def __init__(
        self,
        router,
        sample_rate: int = 16000,
        config: Optional["WebRTCVADConfig"] = None,
        vad_factory=None,
    ):
        if vad_factory is None:
            import webrtcvad

            vad_factory = webrtcvad.Vad

        self.router = router
        self.sample_rate = sample_rate
        self.config = config or WebRTCVADConfig()
        self._bytes_per_sample = 2
        self._frame_samples = int(self.sample_rate * (self.config.frame_ms / 1000.0))
        self._frame_bytes = self._frame_samples * self._bytes_per_sample
        self._vad = vad_factory(self.config.vad_aggressiveness)
        self._sample_cursor = 0
        self._next_segment_id = 1
        self._next_final_to_emit = 1
        self._active: UtteranceState | None = None
        self._pending_finals: dict[int, RealtimeTranscriptEvent | None] = {}
        self._final_tasks: dict[int, asyncio.Task] = {}
        self._speech_run = 0
        self._silence_run = 0
        self._speech_buffer = bytearray()
        self._speech_start_sample = 0

    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for offset in range(0, len(pcm_chunk), self._frame_bytes):
            frame = pcm_chunk[offset:offset + self._frame_bytes]
            if len(frame) != self._frame_bytes:
                break
            events.extend(await self.push_frame(frame, sample_count=self._frame_samples))
        return events

    async def push_frame(
        self,
        pcm_chunk: bytes,
        sample_count: int | None = None,
    ) -> list[RealtimeTranscriptEvent]:
        sample_count = sample_count or self._frame_samples
        events: list[RealtimeTranscriptEvent] = []
        is_speech = self._vad.is_speech(pcm_chunk, self.sample_rate)
        frame_start = self._sample_cursor
        frame_end = frame_start + sample_count
        self._sample_cursor = frame_end
        created_now = False

        if is_speech:
            self._speech_run += 1
            self._silence_run = 0
            if self._active is None and self._speech_run == 1:
                self._speech_start_sample = frame_start
                self._speech_buffer.clear()
            if self._active is None:
                self._speech_buffer.extend(pcm_chunk)
        else:
            self._speech_run = 0
            self._silence_run += 1

        if self._active is None and is_speech and self._speech_run >= self.config.enter_speech_frames:
            self._active = UtteranceState(
                segment_id=self._next_segment_id,
                revision=0,
                start_sample=self._speech_start_sample,
                end_sample=None,
                last_speech_sample=frame_end,
                pcm_buffer=bytearray(self._speech_buffer),
            )
            self._next_segment_id += 1
            self._speech_buffer.clear()
            created_now = True

        if self._active is not None:
            if not created_now:
                self._active.pcm_buffer.extend(pcm_chunk)
            if is_speech:
                self._active.last_speech_sample = frame_end

            preview_event = await self._maybe_emit_preview()
            if preview_event is not None:
                events.append(preview_event)

            utterance_duration_sec = len(self._active.pcm_buffer) / (self.sample_rate * self._bytes_per_sample)
            if self._silence_run >= self.config.endpoint_silence_frames:
                self._seal_active("endpoint")
            elif utterance_duration_sec >= self.config.max_utterance_sec:
                self._seal_active("max_duration")

        events.extend(await self._drain_finished_tasks())
        return events

    async def flush(self, reason: str = "flush") -> list[RealtimeTranscriptEvent]:
        if self._active is None and self._speech_buffer:
            self._active = UtteranceState(
                segment_id=self._next_segment_id,
                revision=0,
                start_sample=self._speech_start_sample,
                end_sample=None,
                last_speech_sample=self._sample_cursor,
                pcm_buffer=bytearray(self._speech_buffer),
            )
            self._next_segment_id += 1
            self._speech_buffer.clear()
        if self._active is not None:
            self._seal_active(reason)
        if self._final_tasks:
            await asyncio.gather(*self._final_tasks.values())
        return await self._drain_finished_tasks()

    async def _maybe_emit_preview(self) -> RealtimeTranscriptEvent | None:
        state = self._active
        if state is None or state.sealed:
            return None
        if state.preview_task is not None and not state.preview_task.done():
            return None

        current_samples = len(state.pcm_buffer) // self._bytes_per_sample
        current_sec = current_samples / self.sample_rate
        grown_samples = current_samples - state.last_preview_sample
        if current_sec < self.config.min_preview_audio_sec:
            return None
        if (grown_samples / self.sample_rate) < self.config.preview_interval_sec:
            return None

        snapshot = bytes(state.pcm_buffer)
        start_time = state.start_sample / self.sample_rate
        end_time = (state.start_sample + current_samples) / self.sample_rate
        try:
            result = await self.router.transcribe_preview(
                pcm16le_to_audio_tuple(snapshot, self.sample_rate)
            )
        except Exception:
            state.last_preview_sample = current_samples
            return None
        if not result.text:
            state.last_preview_sample = current_samples
            return None

        state.revision += 1
        state.first_preview_emitted = True
        state.last_preview_sample = current_samples
        state.last_non_empty_preview_text = result.text
        return RealtimeTranscriptEvent(
            event_type="preview",
            text=result.text,
            start_time=start_time,
            end_time=end_time,
            processing_time=0.0,
            segment_id=state.segment_id,
            revision=state.revision,
            is_final=False,
            cut_reason="preview_tick",
        )

    def _seal_active(self, reason: str) -> None:
        state = self._active
        if state is None or state.sealed:
            return
        state.sealed = True
        state.end_sample = state.start_sample + (len(state.pcm_buffer) // self._bytes_per_sample)
        snapshot = bytes(state.pcm_buffer)
        segment_id = state.segment_id
        revision = state.revision + 1
        start_time = state.start_sample / self.sample_rate
        end_time = state.end_sample / self.sample_rate
        fallback_text = state.last_non_empty_preview_text

        async def run_final():
            try:
                result = await self.router.transcribe_final(
                    pcm16le_to_audio_tuple(snapshot, self.sample_rate)
                )
                text = (result.text or "").strip() or fallback_text
                cut_reason = reason if (result.text or "").strip() else ("final_fallback" if fallback_text else reason)
            except Exception:
                if not fallback_text:
                    return segment_id, None
                text = fallback_text
                cut_reason = "final_fallback"

            if not text:
                return segment_id, None

            return segment_id, RealtimeTranscriptEvent(
                event_type="final",
                text=text,
                start_time=start_time,
                end_time=end_time,
                processing_time=0.0,
                segment_id=segment_id,
                revision=revision,
                is_final=True,
                cut_reason=cut_reason,
            )

        task = asyncio.create_task(run_final(), name=f"final-{segment_id}")
        self._final_tasks[segment_id] = task
        self._active = None

    async def _drain_finished_tasks(self) -> list[RealtimeTranscriptEvent]:
        for segment_id, task in list(self._final_tasks.items()):
            if not task.done():
                continue
            _, result = await task
            self._pending_finals[segment_id] = result
            del self._final_tasks[segment_id]

        return self._emit_ready_finals_in_order()

    def _emit_ready_finals_in_order(self) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        while self._next_final_to_emit in self._pending_finals:
            result = self._pending_finals.pop(self._next_final_to_emit)
            if result is not None:
                events.append(result)
            self._next_final_to_emit += 1
        return events


@dataclass(frozen=True)
class BufferedAudioFrame:
    """A fixed-size frame tracked by the realtime VAD state machine."""

    pcm: bytes
    sample_count: int
    is_speech: bool
    start_sample: int
    end_sample: int


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
        if self.frame_ms <= 0:
            raise ValueError("frame_ms must be > 0")
        if self.chunk_sec <= 0:
            raise ValueError("chunk_sec must be > 0")
        if self.min_flush_sec < 0:
            raise ValueError("min_flush_sec must be >= 0")
        if self.pre_roll_sec < 0:
            raise ValueError("pre_roll_sec must be >= 0")
        if self.min_segment_sec <= 0:
            raise ValueError("min_segment_sec must be > 0")
        if self.min_speech_sec <= 0:
            raise ValueError("min_speech_sec must be > 0")
        if self.enter_speech_sec <= 0:
            raise ValueError("enter_speech_sec must be > 0")
        if self.endpoint_silence_sec <= 0:
            raise ValueError("endpoint_silence_sec must be > 0")
        if self.max_segment_grace_sec < 0:
            raise ValueError("max_segment_grace_sec must be >= 0")
        if self.overlap_sec < 0:
            raise ValueError("overlap_sec must be >= 0")
        if self.silence_threshold <= 0:
            raise ValueError("silence_threshold must be > 0")
        if self.speech_peak_multiplier <= 0:
            raise ValueError("speech_peak_multiplier must be > 0")
        if self.dedupe_tail_chars <= 0:
            raise ValueError("dedupe_tail_chars must be > 0")
        if self.min_dedupe_overlap_chars <= 0:
            raise ValueError("min_dedupe_overlap_chars must be > 0")


class RealtimeMeetingTranscriber:
    """
    Fixed-chunk MVP transcriber.

    The class keeps the old public interface so the rest of the server can
    remain stable, but internally it only emits final transcript segments.
    """

    def __init__(
        self,
        transcribe_wav: Callable[[bytes], Awaitable[ASRResult]],
        sample_rate: int = 16000,
        config: Optional[RealtimeTranscriptionConfig] = None,
    ):
        self._transcribe_wav = transcribe_wav
        self.sample_rate = sample_rate
        self.config = config or RealtimeTranscriptionConfig()

        self._bytes_per_second = self.sample_rate * 2
        self._chunk_bytes = self._align_pcm_bytes(int(self.config.chunk_sec * self._bytes_per_second))
        self._min_flush_bytes = self._align_pcm_bytes(int(self.config.min_flush_sec * self._bytes_per_second))
        self._pending_pcm = bytearray()
        self._buffer_start_bytes = 0
        self._state = "idle"
        self._next_segment_id = 1

    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        """Append PCM bytes and emit any completed final chunks."""
        requests = self.push_pcm_requests(pcm_chunk)
        return await self._transcribe_requests(requests)

    async def flush(self) -> list[RealtimeTranscriptEvent]:
        """Finalize any remaining buffered chunk when the client stops."""
        requests = self.flush_requests(reason="flush")
        return await self._transcribe_requests(requests)

    def push_pcm_requests(self, pcm_chunk: bytes) -> list[RealtimeTranscriptionRequest]:
        """Convert arbitrary PCM chunks into completed fixed-size chunk requests."""
        if not pcm_chunk:
            return []

        aligned_pcm = pcm_chunk[:self._align_pcm_bytes(len(pcm_chunk))]
        if not aligned_pcm:
            return []

        self._pending_pcm.extend(aligned_pcm)
        return self._drain_ready_requests(final=False)

    def push_frame(
        self,
        pcm_chunk: bytes,
        sample_count: Optional[int] = None,
    ) -> list[RealtimeTranscriptionRequest]:
        """Compatibility wrapper for frame-by-frame audio ingestion."""
        del sample_count
        return self.push_pcm_requests(pcm_chunk)

    def flush_requests(self, reason: str = "flush") -> list[RealtimeTranscriptionRequest]:
        """Finalize pending buffered audio without blocking on ASR."""
        return self._drain_ready_requests(final=True, reason=reason)

    async def _transcribe_requests(
        self,
        requests: list[RealtimeTranscriptionRequest],
    ) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for request in requests:
            event = await self.transcribe_request(request)
            if event is None:
                continue
            events.append(event)

        return events

    async def transcribe_request(
        self,
        request: RealtimeTranscriptionRequest,
    ) -> Optional[RealtimeTranscriptEvent]:
        """Run ASR for a previously captured immutable snapshot."""
        if not request.audio_pcm:
            return None

        result = await self._transcribe_wav(
            pcm16le_to_audio_tuple(request.audio_pcm, sample_rate=self.sample_rate)
        )
        text = (result.text or "").strip()
        if not text:
            return None

        return RealtimeTranscriptEvent(
            event_type=request.event_type,
            text=text,
            start_time=request.start_time,
            end_time=request.end_time,
            processing_time=result.processing_time,
            segment_id=request.segment_id,
            revision=request.revision,
            is_final=request.is_final,
            cut_reason=request.cut_reason,
        )

    def _drain_ready_requests(
        self,
        final: bool,
        reason: str = "chunk",
    ) -> list[RealtimeTranscriptionRequest]:
        requests: list[RealtimeTranscriptionRequest] = []
        self._state = "recording" if self._pending_pcm else "idle"

        while len(self._pending_pcm) >= self._chunk_bytes > 0:
            chunk_pcm = bytes(self._pending_pcm[:self._chunk_bytes])
            del self._pending_pcm[:self._chunk_bytes]
            requests.append(
                self._build_request(
                    audio_pcm=chunk_pcm,
                    cut_reason="chunk",
                )
            )

        if final and self._pending_pcm:
            if len(self._pending_pcm) >= self._min_flush_bytes:
                chunk_pcm = bytes(self._pending_pcm)
                self._pending_pcm.clear()
                requests.append(
                    self._build_request(
                        audio_pcm=chunk_pcm,
                        cut_reason=reason,
                    )
                )
            else:
                self._buffer_start_bytes += len(self._pending_pcm)
                self._pending_pcm.clear()

        if not self._pending_pcm:
            self._state = "idle"

        return requests

    def _build_request(
        self,
        audio_pcm: bytes,
        cut_reason: str,
    ) -> RealtimeTranscriptionRequest:
        start_bytes = self._buffer_start_bytes
        end_bytes = start_bytes + len(audio_pcm)
        segment_id = self._next_segment_id
        self._next_segment_id += 1
        self._buffer_start_bytes = end_bytes

        return RealtimeTranscriptionRequest(
            event_type="final",
            segment_id=segment_id,
            revision=1,
            is_final=True,
            audio_pcm=audio_pcm,
            start_time=start_bytes / self._bytes_per_second,
            end_time=end_bytes / self._bytes_per_second,
            cut_reason=cut_reason,
        )

    @staticmethod
    def _align_pcm_bytes(value: int) -> int:
        return max(0, value - (value % 2))


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


class TransformerAudioChunker:
    """
    Streaming PCM chunker for transformer ASR backends.

    The policy is deliberately different from a fixed 3-second cut:
    - accumulate longer context for the decoder,
    - emit early when a pause appears after the minimum context,
    - otherwise force a cut near a low-energy point around the preferred window,
    - keep a short overlap so the next decode still sees the boundary context.
    """

    def __init__(
        self,
        transcribe_wav: Callable[[bytes], Awaitable[ASRResult]],
        sample_rate: int = 16000,
        config: Optional[TransformerChunkingConfig] = None,
    ):
        self._transcribe_wav = transcribe_wav
        self.sample_rate = sample_rate
        self.config = config or TransformerChunkingConfig()

        self._bytes_per_second = self.sample_rate * 2
        self._buffer = bytearray()
        self._counted_prefix_bytes = 0
        self._recent_text_tail = ""
        self._semantic_mode = False

    async def push_pcm(self, pcm_chunk: bytes) -> list[ChunkedASREmission]:
        """Append PCM bytes and emit any ready transcript chunks."""
        if pcm_chunk:
            self._buffer.extend(pcm_chunk)
        return await self._drain_ready_chunks(final=False)

    async def flush(self) -> list[ChunkedASREmission]:
        """Flush all remaining buffered audio."""
        return await self._drain_ready_chunks(final=True)

    async def _drain_ready_chunks(self, final: bool) -> list[ChunkedASREmission]:
        emissions: list[ChunkedASREmission] = []

        while True:
            boundary = self._choose_boundary(final=final)
            if boundary is None:
                break

            segment_pcm = bytes(self._buffer[:boundary])
            if not segment_pcm:
                break

            result = await self._transcribe_wav(
                pcm16le_to_audio_tuple(segment_pcm, sample_rate=self.sample_rate)
            )
            self._semantic_mode = self._semantic_mode or result.has_timestamps

            semantic_commit = self._semantic_commit_from_result(
                result=result,
                boundary=boundary,
                final=final,
            )
            if semantic_commit is not None:
                if semantic_commit.should_wait:
                    break

                emission_text = self._deduplicate_text((semantic_commit.text or "").strip())
                if emission_text:
                    emissions.append(
                        ChunkedASREmission(
                            text=emission_text,
                            audio_duration=semantic_commit.audio_duration,
                            processing_time=result.processing_time,
                        )
                    )
                    self._remember_text(emission_text)

                committed_bytes = max(0, min(boundary, semantic_commit.committed_bytes))
                if final:
                    self._buffer.clear()
                    self._counted_prefix_bytes = 0
                    break

                if committed_bytes <= 0:
                    break

                self._buffer = bytearray(self._buffer[committed_bytes:])
                self._counted_prefix_bytes = 0
                if not self._buffer:
                    break
                continue

            advanced_bytes = max(0, boundary - self._counted_prefix_bytes)
            emission_text = self._deduplicate_text((result.text or "").strip())
            if emission_text:
                emissions.append(
                    ChunkedASREmission(
                        text=emission_text,
                        audio_duration=advanced_bytes / self._bytes_per_second,
                        processing_time=result.processing_time,
                    )
                )
                self._remember_text(emission_text)

            if final:
                self._buffer.clear()
                self._counted_prefix_bytes = 0
                break

            overlap_bytes = min(
                self._align_pcm_bytes(int(self.config.overlap_sec * self._bytes_per_second)),
                boundary,
            )
            remaining = self._buffer[boundary - overlap_bytes:]
            self._buffer = bytearray(remaining)
            self._counted_prefix_bytes = overlap_bytes

            if not self._buffer:
                break

        return emissions

    def _choose_boundary(self, final: bool) -> Optional[int]:
        if len(self._buffer) < 2:
            return None

        total_duration = len(self._buffer) / self._bytes_per_second
        if final:
            return self._align_pcm_bytes(len(self._buffer))

        if total_duration < self.config.min_chunk_sec:
            return None

        if self._semantic_mode:
            if self._has_trailing_silence():
                return self._align_pcm_bytes(len(self._buffer))

            if total_duration >= self.config.max_chunk_sec:
                cap_bytes = self._align_pcm_bytes(
                    int(self.config.semantic_force_commit_sec * self._bytes_per_second)
                )
                return self._align_pcm_bytes(min(len(self._buffer), cap_bytes))

            return None

        if self._has_trailing_silence():
            boundary = self._find_low_energy_boundary(
                start_sec=max(self.config.min_chunk_sec, total_duration - self.config.boundary_search_sec),
                end_sec=total_duration,
            )
            if boundary is not None:
                return boundary

        if total_duration >= self.config.max_chunk_sec:
            start_sec = max(
                self.config.min_chunk_sec,
                self.config.preferred_chunk_sec - self.config.boundary_search_sec,
            )
            end_sec = min(
                total_duration,
                self.config.preferred_chunk_sec + self.config.boundary_search_sec,
            )
            boundary = self._find_low_energy_boundary(start_sec=start_sec, end_sec=end_sec)
            if boundary is not None:
                return boundary

            fallback = int(self.config.preferred_chunk_sec * self._bytes_per_second)
            return self._align_pcm_bytes(min(fallback, len(self._buffer)))

        return None

    def _has_trailing_silence(self) -> bool:
        tail_samples = int(self.config.endpoint_silence_sec * self.sample_rate)
        audio = self._buffer_to_audio()
        if audio.size < tail_samples:
            return False

        tail = audio[-tail_samples:]
        return float(np.mean(np.abs(tail))) <= self.config.silence_threshold

    def _find_low_energy_boundary(self, start_sec: float, end_sec: float) -> Optional[int]:
        audio = self._buffer_to_audio()
        if audio.size == 0:
            return None

        left = max(0, int(start_sec * self.sample_rate))
        right = min(audio.shape[0], int(end_sec * self.sample_rate))
        if right <= left:
            return None

        win = max(4, int((self.config.energy_window_ms / 1000.0) * self.sample_rate))
        if (right - left) <= win:
            return self._align_pcm_bytes(right * 2)

        seg_abs = np.abs(audio[left:right])
        window_sums = np.convolve(seg_abs, np.ones(win, dtype=np.float32), mode="valid")
        min_pos = int(np.argmin(window_sums))
        local = seg_abs[min_pos:min_pos + win]
        inner = int(np.argmin(local))

        boundary_sample = left + min_pos + inner
        boundary_sample = max(boundary_sample, 1)
        return self._align_pcm_bytes(boundary_sample * 2)

    def _buffer_to_audio(self) -> np.ndarray:
        if not self._buffer:
            return np.zeros((0,), dtype=np.float32)
        pcm = np.frombuffer(bytes(self._buffer), dtype="<i2")
        return pcm.astype(np.float32) / 32768.0

    def _semantic_commit_from_result(
        self,
        result: ASRResult,
        boundary: int,
        final: bool,
    ) -> Optional[SemanticCommit]:
        if not result.has_timestamps:
            return None

        decode_duration = result.audio_duration if result.audio_duration > 0 else boundary / self._bytes_per_second
        segments = self._normalize_timed_segments(result.segments, decode_duration)
        if not segments:
            if final:
                return SemanticCommit(
                    text=(result.text or "").strip(),
                    committed_bytes=boundary,
                    audio_duration=decode_duration,
                )
            return SemanticCommit(text="", committed_bytes=0, audio_duration=0.0, should_wait=True)

        search_limit = decode_duration if final else max(0.0, decode_duration - self.config.semantic_right_guard_sec)
        latest_candidate_idx: Optional[int] = None
        last_idx_before_limit: Optional[int] = None
        visible_chars = 0

        for index, segment in enumerate(segments):
            token_end = min(decode_duration, max(0.0, float(segment["end"])))
            if token_end > search_limit + 1e-3:
                break

            last_idx_before_limit = index
            token_text = str(segment["text"] or "")
            stripped_text = token_text.strip()
            if stripped_text:
                visible_chars += len(stripped_text)

            next_start = decode_duration
            if index + 1 < len(segments):
                next_start = min(
                    decode_duration,
                    max(token_end, float(segments[index + 1]["start"])),
                )
            gap_sec = max(0.0, next_start - token_end)

            if self._is_strong_semantic_boundary(stripped_text):
                latest_candidate_idx = index
            elif self._is_soft_semantic_boundary(stripped_text) and gap_sec >= self.config.semantic_soft_pause_sec:
                latest_candidate_idx = index
            elif gap_sec >= self.config.semantic_pause_sec and visible_chars >= self.config.semantic_min_chars:
                latest_candidate_idx = index
            elif (
                gap_sec >= self.config.semantic_short_pause_sec
                and 0 < visible_chars <= self.config.semantic_short_utterance_chars
            ):
                latest_candidate_idx = index

        if latest_candidate_idx is None:
            if final:
                latest_candidate_idx = len(segments) - 1
            elif (
                decode_duration >= self.config.semantic_force_commit_sec
                and last_idx_before_limit is not None
                and visible_chars >= self.config.semantic_min_chars
            ):
                latest_candidate_idx = last_idx_before_limit
            else:
                return SemanticCommit(text="", committed_bytes=0, audio_duration=0.0, should_wait=True)

        commit_text = "".join(str(segment["text"] or "") for segment in segments[:latest_candidate_idx + 1]).strip()
        commit_sec = min(decode_duration, max(0.0, float(segments[latest_candidate_idx]["end"])))
        commit_bytes = min(
            boundary,
            self._align_pcm_bytes(int(round(commit_sec * self._bytes_per_second))),
        )

        if final:
            full_text = (result.text or "").strip()
            return SemanticCommit(
                text=full_text or commit_text,
                committed_bytes=boundary,
                audio_duration=decode_duration,
            )

        if commit_bytes <= 0:
            return SemanticCommit(text="", committed_bytes=0, audio_duration=0.0, should_wait=True)

        return SemanticCommit(
            text=commit_text,
            committed_bytes=commit_bytes,
            audio_duration=commit_bytes / self._bytes_per_second,
        )

    @staticmethod
    def _normalize_timed_segments(segments: list[dict], decode_duration: float) -> list[dict]:
        normalized: list[dict] = []
        for segment in segments or []:
            if not isinstance(segment, dict):
                continue

            text = str(segment.get("text", "") or "")
            try:
                start = float(segment.get("start", 0.0))
                end = float(segment.get("end", 0.0))
            except (TypeError, ValueError):
                continue

            start = max(0.0, min(start, decode_duration))
            end = max(start, min(end, decode_duration))
            if end <= start and text.strip():
                continue

            normalized.append({
                "start": start,
                "end": end,
                "text": text,
            })

        normalized.sort(key=lambda item: (item["end"], item["start"]))
        return normalized

    @staticmethod
    def _is_strong_semantic_boundary(text: str) -> bool:
        return text.endswith(("。", "！", "？", "!", "?", "…"))

    @staticmethod
    def _is_soft_semantic_boundary(text: str) -> bool:
        return text.endswith(("，", ",", "、", "；", ";", "：", ":"))

    def _deduplicate_text(self, text: str) -> str:
        if not text:
            return ""

        tail = self._recent_text_tail[-self.config.dedupe_tail_chars:]
        overlap_chars = self._find_normalized_overlap_chars(tail, text)
        if overlap_chars > 0:
            trim_index = self._normalized_prefix_trim_index(text, overlap_chars)
            text = text[trim_index:]

        return text.lstrip()

    def _remember_text(self, text: str):
        self._recent_text_tail = (self._recent_text_tail + text)[-self.config.dedupe_tail_chars * 2:]

    def _find_normalized_overlap_chars(self, previous_text: str, current_text: str) -> int:
        prev_norm, _ = self._normalize_with_positions(previous_text)
        curr_norm, _ = self._normalize_with_positions(current_text)
        if not prev_norm or not curr_norm:
            return 0

        max_overlap = min(len(prev_norm), len(curr_norm))
        min_overlap = min(self.config.min_dedupe_overlap_chars, len(curr_norm))
        for size in range(max_overlap, min_overlap - 1, -1):
            if prev_norm[-size:] == curr_norm[:size]:
                return size

        return 0

    @staticmethod
    def _normalized_prefix_trim_index(text: str, overlap_chars: int) -> int:
        _, positions = TransformerAudioChunker._normalize_with_positions(text)
        if overlap_chars <= 0 or overlap_chars > len(positions):
            return 0
        return positions[overlap_chars - 1]

    @staticmethod
    def _normalize_with_positions(text: str) -> tuple[str, list[int]]:
        normalized_chars: list[str] = []
        positions: list[int] = []
        for index, char in enumerate(text):
            if char.isalnum():
                normalized_chars.append(char.lower())
                positions.append(index + 1)
                continue

            if "\u4e00" <= char <= "\u9fff":
                normalized_chars.append(char)
                positions.append(index + 1)

        return "".join(normalized_chars), positions

    @staticmethod
    def _align_pcm_bytes(value: int) -> int:
        return max(0, value - (value % 2))


class SemanticMeetingTranscriber:
    """
    Adapter that exposes semantic chunking through realtime transcript events.

    `TransformerAudioChunker` decides when text is safe to commit. This wrapper
    assigns stable segment ids and maintains a monotonic timeline so the rest of
    the websocket protocol can stay unchanged.
    """

    def __init__(
        self,
        transcribe_wav: Callable[[bytes], Awaitable[ASRResult]],
        sample_rate: int = 16000,
        config: Optional[TransformerChunkingConfig] = None,
    ):
        self._chunker = TransformerAudioChunker(
            transcribe_wav=transcribe_wav,
            sample_rate=sample_rate,
            config=config,
        )
        self._next_segment_id = 1
        self._timeline_sec = 0.0

    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        emissions = await self._chunker.push_pcm(pcm_chunk)
        return self._build_events(emissions, cut_reason="semantic")

    async def flush(self) -> list[RealtimeTranscriptEvent]:
        emissions = await self._chunker.flush()
        return self._build_events(emissions, cut_reason="flush")

    def _build_events(
        self,
        emissions: list[ChunkedASREmission],
        cut_reason: str,
    ) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []

        for emission in emissions:
            text = (emission.text or "").strip()
            if not text:
                continue

            start_time = self._timeline_sec
            duration = max(0.0, float(emission.audio_duration))
            end_time = start_time + duration
            self._timeline_sec = end_time

            events.append(
                RealtimeTranscriptEvent(
                    event_type="final",
                    text=text,
                    start_time=start_time,
                    end_time=end_time,
                    processing_time=emission.processing_time,
                    segment_id=self._next_segment_id,
                    revision=1,
                    is_final=True,
                    cut_reason=cut_reason,
                )
            )
            self._next_segment_id += 1

        return events


class ASRServiceError(RuntimeError):
    """Raised when the ASR pipeline cannot transcribe audio."""


class ASRService:
    """
    Qwen3-ASR Service for real-time transcription.
    Non-blocking design - uses thread pool for inference.
    """

    def __init__(self, config: ASRConfig):
        self.config = config
        self._model = None
        self._processor = None
        self._model_lock = threading.Lock()
        self._initialized = False
        self._init_task: Optional[asyncio.Task] = None
        self._init_error: Optional[BaseException] = None

    async def initialize(self):
        """Initialize ASR model in background"""
        if self._initialized:
            return

        if self._init_task is not None and not self._init_task.done():
            return

        self._init_error = None

        async def _load_model():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_asr_executor, self._load_model_sync)

        self._init_task = asyncio.create_task(_load_model(), name="asr-initialize")
        self._init_task.add_done_callback(self._handle_init_task_done)

    async def wait_ready(self, timeout: Optional[float] = None):
        """Wait for model to be ready"""
        if timeout is None:
            timeout = self.config.init_timeout_sec

        if self._init_task is None or (self._init_task.done() and not self._initialized):
            await self.initialize()

        try:
            await asyncio.wait_for(self._init_task, timeout=timeout)
            self._initialized = self._model is not None and self._processor is not None
            if not self._initialized:
                raise ASRServiceError("ASR model did not finish initialization")
            logger.info("ASR model ready")
        except asyncio.TimeoutError:
            logger.error("ASR model initialization timed out after %.1fs", timeout)
            raise ASRServiceError(f"ASR model initialization timed out after {timeout:.0f}s")
        except Exception as e:
            self._initialized = False
            logger.error(f"ASR model initialization failed: {e}")
            raise ASRServiceError(f"ASR model initialization failed: {e}") from e

    def initialization_state(self) -> str:
        """Return the coarse-grained initialization state for diagnostics."""
        if self._initialized and self._model is not None and self._processor is not None:
            return "ready"
        if self._init_task is not None and not self._init_task.done():
            return "loading"
        if self._init_error is not None:
            return "failed"
        return "idle"

    def initialization_error(self) -> str | None:
        """Return the latest initialization error message, if any."""
        return str(self._init_error) if self._init_error is not None else None

    async def shutdown(self):
        """Cancel background initialization if the process is shutting down."""
        task = self._init_task
        if task is None or task.done():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _handle_init_task_done(self, task: asyncio.Task):
        if task.cancelled():
            return

        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return

        if exc is None:
            return

        self._initialized = False
        self._init_error = exc
        logger.error("ASR background initialization failed: %s", exc)

    def _load_model_sync(self):
        """Synchronous model loading - runs in thread pool"""
        with self._model_lock:
            if self._model is not None:
                return

            device = self.config.device or "auto"
            try:
                from qwen_asr import Qwen3ASRModel

                model_path = self.config.model_path

                # Check if model exists
                if not os.path.exists(model_path):
                    raise FileNotFoundError(f"ASR model not found at {model_path}")

                logger.info(f"Loading ASR model from {model_path}...")

                # Determine device
                if self.config.device == "auto":
                    if torch.cuda.is_available():
                        device = "cuda"
                    elif torch.backends.mps.is_available():
                        device = "mps"
                    else:
                        device = "cpu"
                else:
                    device = self.config.device

                logger.info(f"Using device: {device}")

                if device == "cuda":
                    dtype = torch.bfloat16
                elif device == "mps":
                    dtype = torch.float16
                else:
                    dtype = torch.float32

                load_kwargs = {
                    "dtype": dtype,
                    "max_inference_batch_size": self.config.max_inference_batch_size,
                    "max_new_tokens": self.config.max_new_tokens,
                }
                if device in ("cpu", "mps"):
                    # Sharded checkpoints can remain on meta tensors when loaded
                    # with the default low-memory path, which breaks the later
                    # `.to(device)` move used on non-CUDA backends.
                    load_kwargs["low_cpu_mem_usage"] = False
                forced_aligner_path = None
                if self.config.aligner_path:
                    if os.path.exists(self.config.aligner_path):
                        forced_aligner_path = self.config.aligner_path
                    else:
                        logger.warning(
                            "Forced aligner not found at %s; semantic timestamp commit disabled",
                            self.config.aligner_path,
                        )

                # CUDA can be dispatched directly. MPS is more reliable when moved after load.
                if device == "cuda":
                    load_kwargs["device_map"] = "cuda:0"
                    attn_impl = self._resolve_attention_implementation()
                    if attn_impl:
                        load_kwargs["attn_implementation"] = attn_impl

                logger.info(
                    "ASR runtime config: language=%s batch=%s max_new_tokens=%s attn=%s",
                    self.config.language or "auto",
                    load_kwargs["max_inference_batch_size"],
                    load_kwargs["max_new_tokens"],
                    load_kwargs.get("attn_implementation", "default"),
                )

                self._model = Qwen3ASRModel.from_pretrained(
                    model_path,
                    **load_kwargs,
                )
                self._processor = getattr(self._model, "processor", None)

                if forced_aligner_path is not None:
                    forced_aligner_kwargs = {
                        "dtype": dtype,
                    }
                    if device in ("cpu", "mps"):
                        forced_aligner_kwargs["low_cpu_mem_usage"] = False
                    if device == "cuda":
                        forced_aligner_kwargs["device_map"] = "cuda:0"
                        attn_impl = load_kwargs.get("attn_implementation")
                        if attn_impl:
                            forced_aligner_kwargs["attn_implementation"] = attn_impl

                    self._model.forced_aligner = load_alignment_model(
                        self.config.aligner_backend,
                        forced_aligner_path,
                        **forced_aligner_kwargs,
                    )
                else:
                    self._model.forced_aligner = None

                if device in ("cpu", "mps"):
                    self._model.model = self._model.model.to(device)
                    if self._model.forced_aligner is not None:
                        self._model.forced_aligner.model = self._model.forced_aligner.model.to(device)
                        self._model.forced_aligner.device = torch.device(device)

                self._model.model.eval()
                if self._model.forced_aligner is not None:
                    self._model.forced_aligner.model.eval()
                    logger.info(
                        "Alignment backend %s loaded from %s",
                        self.config.aligner_backend,
                        forced_aligner_path,
                    )
                logger.info("ASR model loaded successfully")

            except ImportError as e:
                logger.error(f"Missing dependencies: {e}")
                raise ASRServiceError(
                    f"Missing dependencies: {e}. Run: pip install -r requirements.txt"
                ) from e
            except Exception as e:
                error_message = str(e)
                if device == "mps" and "meta tensor" in error_message.lower():
                    error_message = (
                        f"{error_message}. "
                        "Current MPS runtime is unstable for this model; prefer Python 3.11/3.12 and retry on CPU or CUDA."
                    )
                logger.error(f"ASR model load error: {error_message}")
                raise ASRServiceError(error_message) from e

    def _resolve_attention_implementation(self) -> str:
        """
        Match the official CUDA recommendation when flash-attn is available.

        `auto` enables flash attention on CUDA only when the dependency exists.
        """
        mode = self.config.attn_implementation.strip().lower()
        if not mode or mode == "none":
            return ""
        if mode != "auto":
            return self.config.attn_implementation

        try:
            import flash_attn  # noqa: F401
        except ImportError:
            return ""

        return "flash_attention_2"

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> ASRResult:
        """
        Transcribe audio data.
        Non-blocking - runs in thread pool.
        """
        if not self._initialized or self._model is None or self._processor is None:
            await self.wait_ready()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _asr_executor,
            self._transcribe_sync,
            audio_data,
            sample_rate
        )

    def _transcribe_sync(self, audio_data: bytes, sample_rate: int) -> ASRResult:
        """Synchronous transcription - runs in thread pool"""
        import time
        start_time = time.time()

        try:
            audio, sr = self._read_audio_source(audio_data)
            return self._transcribe_audio_array(audio, sr, start_time=start_time)

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            raise ASRServiceError(f"Transcription failed: {e}") from e

    async def transcribe_wav(
        self,
        wav_source,
        language: str | None = None,
        context: str | None = None,
    ) -> ASRResult:
        """Transcribe a WAV file or WAV bytes"""
        if not self._initialized or self._model is None or self._processor is None:
            await self.wait_ready()

        loop = asyncio.get_running_loop()
        call = partial(
            self._transcribe_wav_sync,
            wav_source,
            language,
            context,
        )
        return await loop.run_in_executor(
            _asr_executor,
            call,
        )

    def _transcribe_wav_sync(
        self,
        wav_source,
        language: str | None = None,
        context: str | None = None,
    ) -> ASRResult:
        """Synchronous WAV transcription - accepts file path or bytes"""
        import time
        start_time = time.time()

        try:
            audio, sr = self._read_audio_source(wav_source)
            return self._transcribe_audio_array(
                audio,
                sr,
                start_time=start_time,
                language=language,
                context=context,
            )

        except Exception as e:
            logger.error(f"WAV transcription error: {e}")
            raise ASRServiceError(f"WAV transcription failed: {e}") from e

    def _read_audio_source(self, wav_source):
        """Read audio source into a mono float32 waveform and sample rate.

        Supported formats:
        - WAV / FLAC / OGG: read directly via soundfile
        - MP3 / M4A / other: transcoded to 16kHz mono PCM WAV via FFmpeg
        """
        import io
        import soundfile as sf

        if (
            isinstance(wav_source, tuple)
            and len(wav_source) == 2
        ):
            audio, sr = wav_source
            raw_audio = np.asarray(audio)
            if np.issubdtype(raw_audio.dtype, np.integer):
                scale = max(abs(np.iinfo(raw_audio.dtype).min), np.iinfo(raw_audio.dtype).max)
                audio = raw_audio.astype(np.float32) / float(scale)
            else:
                audio = raw_audio.astype(np.float32)
        elif isinstance(wav_source, bytes):
            audio, sr = self._read_audio_or_transcode_bytes(wav_source)
        elif isinstance(wav_source, str):
            audio, sr = self._read_audio_or_transcode_path(wav_source)
        else:
            audio, sr = sf.read(wav_source, dtype="float32")

        if len(audio.shape) > 1:
            audio = audio.mean(axis=1)

        return audio, sr

    def _read_audio_or_transcode_bytes(self, source: bytes):
        """Try soundfile first; fall back to FFmpeg for unsupported formats."""
        import io
        import soundfile as sf

        try:
            audio, sr = sf.read(io.BytesIO(source), dtype="float32")
            return audio, sr
        except Exception:
            wav_bytes = transcode_to_wav_pcm(source)
            audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            return audio, sr

    def _read_audio_or_transcode_path(self, source: str):
        """Try soundfile first; fall back to FFmpeg for unsupported formats."""
        import io
        import soundfile as sf

        try:
            audio, sr = sf.read(source, dtype="float32")
            return audio, sr
        except Exception:
            with open(source, "rb") as f:
                content = f.read()
            wav_bytes = transcode_to_wav_pcm(content, filename=source)
            audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            return audio, sr

    def _transcribe_audio_array(
        self,
        audio: np.ndarray,
        sample_rate: int,
        start_time: float,
        language: str | None = None,
        context: str | None = None,
    ) -> ASRResult:
        """Run Qwen3-ASR on a waveform array."""
        import time

        use_timestamps = getattr(self._model, "forced_aligner", None) is not None
        effective_language = (language if language is not None else self.config.language) or None
        effective_context = (context or "").strip()
        results = self._model.transcribe(
            audio=(audio, sample_rate),
            context=effective_context,
            language=effective_language,
            return_time_stamps=use_timestamps,
        )
        if not results:
            raise ASRServiceError("Empty transcription result")

        first = results[0]
        text = (first.text or "").strip()
        duration = len(audio) / sample_rate if sample_rate else 0.0
        time_stamps = getattr(first, "time_stamps", None)
        segments = self._extract_segments_from_timestamps(time_stamps, text=text, duration=duration)
        has_timestamps = bool(use_timestamps and segments and not self._is_fallback_segment(segments, text, duration))

        return ASRResult(
            text=text,
            segments=segments,
            audio_duration=duration,
            processing_time=time.time() - start_time,
            has_timestamps=has_timestamps,
        )

    @staticmethod
    def _is_cjk_char(ch: str) -> bool:
        return "\u4e00" <= ch <= "\u9fff"

    @staticmethod
    def _extract_segments_from_timestamps(time_stamps, text: str, duration: float) -> list[dict]:
        """Return grouped user-facing transcript segments from forced-aligner timestamps.

        The forced aligner emits low-level items, typically one per word for
        space-delimited languages or one per character for CJK text. This helper
        merges those items into transcript segments that match what users should
        see, using punctuation boundaries, pause gaps, and a maximum segment
        duration cap.

        Text reconstruction keeps CJK compact by joining directly, while
        non-CJK text inserts spaces between consecutive items so word spacing
        remains correct in the final transcript segments.
        """
        items = getattr(time_stamps, "items", None)
        if items is None and isinstance(time_stamps, list):
            items = time_stamps

        raw_items: list[tuple[float, float, str]] = []
        for item in items or []:
            if isinstance(item, dict):
                item_text = str(item.get("text", "") or "")
                start = item.get("start_time", item.get("start", 0.0))
                end = item.get("end_time", item.get("end", 0.0))
            else:
                item_text = str(getattr(item, "text", "") or "")
                start = getattr(item, "start_time", getattr(item, "start", 0.0))
                end = getattr(item, "end_time", getattr(item, "end", 0.0))

            try:
                start_sec = max(0.0, min(float(start), duration))
                end_sec = max(start_sec, min(float(end), duration))
            except (TypeError, ValueError):
                continue

            if end_sec <= start_sec:
                continue

            raw_items.append((start_sec, end_sec, item_text))

        if not raw_items:
            return [{
                "start": 0.0,
                "end": duration,
                "text": text,
            }]

        # Merge consecutive items into segments based on timestamps
        segments: list[dict] = []
        seg_start = raw_items[0][0]
        seg_end = raw_items[0][1]
        seg_texts: list[str] = [raw_items[0][2]]

        SEMANTIC_PAUSE_SEC = 0.3
        SEMANTIC_SOFT_PAUSE_SEC = 0.5
        MAX_SEG_SEC = 10.0

        def is_strong_boundary(t: str) -> bool:
            return t.strip().endswith(("。", "！", "？", "!", "?", ".", "…", "）", ")"))

        def is_soft_boundary(t: str) -> bool:
            return t.strip().endswith(("，", ",", "、", "；", ";", "：", ":"))

        def build_segment_text(texts: list[str]) -> str:
            """Join items with appropriate spacing.

            CJK items (Chinese chars): join directly without spaces.
            Non-CJK items (English words): join with spaces.
            """
            if not texts:
                return ""
            parts: list[str] = []
            for t in texts:
                if parts:
                    # If previous part ends with non-CJK and current starts with non-CJK,
                    # insert a space (English word boundary)
                    prev = parts[-1]
                    if not ASRService._is_cjk_char(prev[-1]) and not ASRService._is_cjk_char(t[0]):
                        parts.append(" ")
                parts.append(t)
            return "".join(parts)

        for i in range(1, len(raw_items)):
            prev_end = raw_items[i - 1][1]
            curr_start = raw_items[i][0]
            gap = curr_start - prev_end
            prev_text = raw_items[i - 1][2]
            curr_text = raw_items[i][2]
            seg_duration = curr_start - seg_start

            start_new = False

            if is_strong_boundary(prev_text):
                start_new = True
            elif is_soft_boundary(prev_text) and gap >= SEMANTIC_SOFT_PAUSE_SEC:
                start_new = True
            elif gap >= SEMANTIC_PAUSE_SEC:
                start_new = True
            elif seg_duration >= MAX_SEG_SEC:
                start_new = True

            if start_new:
                seg_text = build_segment_text(seg_texts)
                if seg_text.strip():
                    segments.append({
                        "start": seg_start,
                        "end": seg_end,
                        "text": seg_text,
                    })
                seg_start = raw_items[i][0]
                seg_end = raw_items[i][1]
                seg_texts = [curr_text]
            else:
                seg_end = raw_items[i][1]
                seg_texts.append(curr_text)

        # Finalize last segment
        seg_text = build_segment_text(seg_texts)
        if seg_text.strip():
            segments.append({
                "start": seg_start,
                "end": seg_end,
                "text": seg_text,
            })

        if segments:
            return segments

        return [{
            "start": 0.0,
            "end": duration,
            "text": text,
        }]

    @staticmethod
    def _is_fallback_segment(segments: list[dict], text: str, duration: float) -> bool:
        if len(segments) != 1:
            return False

        segment = segments[0]
        return (
            segment.get("text", "") == text
            and abs(float(segment.get("start", 0.0))) < 1e-6
            and abs(float(segment.get("end", 0.0)) - duration) < 1e-3
        )


def create_wav_from_pcm(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Create WAV file bytes from PCM data"""
    import io
    import struct
    import wave

    # Convert bytes to array
    num_samples = len(pcm_data) // 2  # 16-bit = 2 bytes per sample
    samples = struct.unpack(f"<{num_samples}h", pcm_data)

    # Normalize to float
    float_samples = np.array(samples, dtype=np.float32) / 32768.0

    # Create WAV in memory
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        # Convert back to 16-bit
        int_samples = np.clip(float_samples * 32767, -32768, 32767).astype(np.int16)
        wf.writeframes(int_samples.tobytes())

    return buffer.getvalue()

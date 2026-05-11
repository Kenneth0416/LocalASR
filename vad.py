"""
VAD-based realtime transcriber using WebRTC Voice Activity Detection.

WebRTCVADMeetingTranscriber manages a state machine that:
  1. Detects speech via WebRTC VAD frame-by-frame
  2. Accumulates audio into utterances
  3. Commits one finalized transcript when speech ends
"""

import asyncio
import logging
from collections import deque
from typing import Optional

import numpy as np

from asr_types import (
    BufferedAudioFrame,
    LiteASRResult,
    RealtimeTranscriptEvent,
    UtteranceState,
    pcm16le_to_audio_tuple,
)

from config import WebRTCVADConfig

logger = logging.getLogger("meeting.vad")

class WebRTCVADMeetingTranscriber:
    """WebRTC VAD-driven realtime meeting transcriber."""

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
        self._active: Optional[UtteranceState] = None
        self._pending_finals: dict[int, Optional[RealtimeTranscriptEvent]] = {}
        self._final_tasks: dict[int, asyncio.Task] = {}
        self._sealed_work: deque[tuple] = deque()
        self._sealed_work_maxlen = 20  # Drop oldest segments if transcription falls behind
        self._on_final_done = None
        self._speech_run = 0
        self._silence_run = 0
        self._speech_buffer = bytearray()
        self._speech_start_sample = 0
        self._capture_start_sample = 0
        self._pre_roll_samples = max(0, int(round(self.config.pre_roll_sec * self.sample_rate)))
        self._recent_frames: deque[BufferedAudioFrame] = deque()
        self._recent_samples = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for offset in range(0, len(pcm_chunk), self._frame_bytes):
            frame = pcm_chunk[offset:offset + self._frame_bytes]
            if len(frame) != self._frame_bytes:
                break
            events.extend(await self.push_frame(frame, sample_count=self._frame_samples))
        events.extend(await self._drain_finished_tasks())
        return events

    async def push_frame(
        self,
        pcm_chunk: bytes,
        sample_count: Optional[int] = None,
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
                self._capture_start_sample, pre_roll_pcm = self._consume_pre_roll()
                self._speech_buffer.extend(pre_roll_pcm)
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
                capture_start_sample=self._capture_start_sample,
                end_sample=None,
                last_speech_sample=frame_end,
                pcm_buffer=bytearray(self._speech_buffer),
            )
            self._next_segment_id += 1
            self._speech_buffer.clear()
            self._clear_recent_frames()
            created_now = True

        if self._active is not None:
            if not created_now:
                self._active.pcm_buffer.extend(pcm_chunk)
            if is_speech:
                self._active.last_speech_sample = frame_end

            utterance_duration_sec = len(self._active.pcm_buffer) / (self.sample_rate * self._bytes_per_sample)
            if self._silence_run >= self.config.endpoint_silence_frames:
                self._seal_active("endpoint")
            elif utterance_duration_sec >= self.config.max_utterance_sec:
                self._seal_active("max_duration")

        events.extend(await self._drain_finished_tasks())
        if self._active is None:
            self._remember_recent_frame(
                BufferedAudioFrame(
                    pcm=pcm_chunk,
                    sample_count=sample_count,
                    is_speech=is_speech,
                    start_sample=frame_start,
                    end_sample=frame_end,
                )
            )
        return events

    async def flush(self, reason: str = "flush") -> list[RealtimeTranscriptEvent]:
        if self._active is None and self._speech_buffer:
            # Defensive check: if speech_buffer is unreasonably large, truncate it
            buffer_sec = len(self._speech_buffer) / (self.sample_rate * self._bytes_per_sample)
            max_expected = self.config.max_utterance_sec + self.config.pre_roll_sec + 1.0
            if buffer_sec > max_expected:
                logger.warning(
                    "flush: speech_buffer too large (%.1fs), truncating to %.1fs",
                    buffer_sec,
                    max_expected,
                )
                max_bytes = int(max_expected * self.sample_rate * self._bytes_per_sample)
                self._speech_buffer = self._speech_buffer[-max_bytes:]
            self._active = UtteranceState(
                segment_id=self._next_segment_id,
                revision=0,
                start_sample=self._speech_start_sample,
                capture_start_sample=self._capture_start_sample,
                end_sample=None,
                last_speech_sample=self._sample_cursor,
                pcm_buffer=bytearray(self._speech_buffer),
            )
            self._next_segment_id += 1
            self._speech_buffer.clear()
        if self._active is not None:
            self._seal_active(reason)
        all_events: list[RealtimeTranscriptEvent] = []
        while self._sealed_work or self._final_tasks:
            self._maybe_start_next_final()
            if self._final_tasks:
                await asyncio.gather(*self._final_tasks.values())
            all_events.extend(await self._drain_finished_tasks())
        return all_events

    # ── Final emission ─────────────────────────────────────────────────────────

    def _seal_active(self, reason: str) -> None:
        state = self._active
        if state is None or state.sealed:
            return
        state.sealed = True
        state.end_sample = max(state.start_sample, state.last_speech_sample)
        snapshot = bytes(state.pcm_buffer)
        segment_id = state.segment_id
        revision = state.revision + 1
        start_time = state.start_sample / self.sample_rate
        end_time = state.end_sample / self.sample_rate
        capture_start_time = state.capture_start_sample / self.sample_rate
        capture_duration = len(snapshot) / (self.sample_rate * self._bytes_per_sample)
        # Defensive cap: capture_duration should not massively exceed max_utterance_sec + pre_roll
        max_expected = self.config.max_utterance_sec + self.config.pre_roll_sec + 1.0
        if capture_duration > max_expected:
            logger.warning(
                "Segment %d capture_duration %.1fs exceeds max_expected %.1fs, capping (reason=%s)",
                segment_id,
                capture_duration,
                max_expected,
                reason,
            )
            capture_duration = max_expected

        # Drop oldest stale segments if transcription falls behind
        if len(self._sealed_work) >= self._sealed_work_maxlen:
            dropped = self._sealed_work.popleft()
            logger.warning(
                "Dropping stale utterance segment %d due to backlog (%d queued)",
                dropped[1], len(self._sealed_work) + 1,
            )

        self._sealed_work.append(
            (
                snapshot,
                segment_id,
                revision,
                start_time,
                end_time,
                capture_start_time,
                capture_duration,
                reason,
            )
        )
        self._active = None
        self._maybe_start_next_final()

    def _maybe_start_next_final(self) -> None:
        """Start the next ASR task only when no task is already in flight."""
        if self._final_tasks or not self._sealed_work:
            return
        (
            snapshot,
            segment_id,
            revision,
            start_time,
            end_time,
            capture_start_time,
            capture_duration,
            reason,
        ) = (
            self._sealed_work.popleft()
        )

        # Skip segments that are too short to produce meaningful transcription
        audio_sec = len(snapshot) / (self.sample_rate * self._bytes_per_sample)
        if audio_sec < self.config.min_final_audio_sec:
            logger.debug(
                "Segment %d short (%.3fs < min_final_audio_sec=%.3fs), sending to ASR anyway",
                segment_id,
                audio_sec,
                self.config.min_final_audio_sec,
            )

        async def run_final():
            try:
                result = await self.router.transcribe_final(
                    pcm16le_to_audio_tuple(snapshot, self.sample_rate)
                )
                text = (result.text or "").strip()
                if not text:
                    logger.warning(
                        "Segment %d produced empty transcription (duration=%.1fs)",
                        segment_id,
                        len(snapshot) / (self.sample_rate * self._bytes_per_sample),
                    )
                    return segment_id, None
            except Exception:
                logger.warning(
                    "Segment %d transcription failed",
                    segment_id,
                    exc_info=True,
                )
                return segment_id, None

            return segment_id, RealtimeTranscriptEvent(
                event_type="final",
                text=text,
                start_time=start_time,
                end_time=end_time,
                processing_time=float(getattr(result, "processing_time", 0.0) or 0.0),
                segment_id=segment_id,
                revision=revision,
                is_final=True,
                capture_start_time=capture_start_time,
                capture_duration=capture_duration,
                cut_reason=reason,
            )

        task = asyncio.create_task(run_final(), name=f"final-{segment_id}")
        self._final_tasks[segment_id] = task
        if self._on_final_done:
            notify = self._on_final_done
            task.add_done_callback(lambda _t: notify())

    # ── Task draining ──────────────────────────────────────────────────────────

    async def _drain_finished_tasks(self) -> list[RealtimeTranscriptEvent]:
        for segment_id, task in list(self._final_tasks.items()):
            if not task.done():
                continue
            _, result = await task
            self._pending_finals[segment_id] = result
            del self._final_tasks[segment_id]

        self._maybe_start_next_final()
        return self._emit_ready_finals_in_order()

    def _consume_pre_roll(self) -> tuple[int, bytes]:
        if self._pre_roll_samples <= 0 or not self._recent_frames:
            return self._speech_start_sample, b""

        selected: list[BufferedAudioFrame] = []
        sample_budget = self._pre_roll_samples
        for frame in reversed(self._recent_frames):
            if sample_budget <= 0:
                break
            selected.append(frame)
            sample_budget -= frame.sample_count
        selected.reverse()
        if not selected:
            return self._speech_start_sample, b""
        return selected[0].start_sample, b"".join(frame.pcm for frame in selected)

    def _remember_recent_frame(self, frame: BufferedAudioFrame) -> None:
        if self._pre_roll_samples <= 0:
            return
        self._recent_frames.append(frame)
        self._recent_samples += frame.sample_count
        while self._recent_frames and self._recent_samples - self._recent_frames[0].sample_count >= self._pre_roll_samples:
            dropped = self._recent_frames.popleft()
            self._recent_samples -= dropped.sample_count

    def _clear_recent_frames(self) -> None:
        self._recent_frames.clear()
        self._recent_samples = 0

    def _emit_ready_finals_in_order(self) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        while self._next_final_to_emit in self._pending_finals:
            result = self._pending_finals.pop(self._next_final_to_emit)
            if result is not None:
                events.append(result)
            self._next_final_to_emit += 1
        return events

    def pipeline_stats(self) -> dict:
        """Return live pipeline state for observability."""
        active_ms = 0
        if self._active is not None:
            active_ms = int(
                len(self._active.pcm_buffer) / (self.sample_rate * self._bytes_per_sample) * 1000
            )
        speech_buf_ms = int(
            len(self._speech_buffer) / (self.sample_rate * self._bytes_per_sample) * 1000
        )
        return {
            "asr_in_flight": len(self._final_tasks),
            "asr_queued": len(self._sealed_work),
            "asr_pending_emit": len(self._pending_finals),
            "active_utterance": self._active is not None,
            "active_utterance_ms": active_ms,
            "speech_buffer_ms": speech_buf_ms,
        }


# ── Silero VAD transcriber ─────────────────────────────────────────────────────

try:
    from silero_vad import load_silero_vad
    _SILERO_AVAILABLE = True
except ImportError:
    load_silero_vad = None  # type: ignore[assignment]
    _SILERO_AVAILABLE = False


class SileroVADTranscriber:
    """Silero VAD-driven realtime meeting transcriber.

    Same state machine as WebRTCVADMeetingTranscriber but uses Silero's
    neural-network VAD (ONNX) instead of WebRTC's GMM-based VAD.
    Frame size is 512 samples (32ms at 16kHz).
    """

    def __init__(
        self,
        router,
        sample_rate: int = 16000,
        config: Optional["SileroVADConfig"] = None,
        vad_model=None,
    ):
        if vad_model is None:
            if load_silero_vad is None:
                raise RuntimeError(
                    "silero-vad not installed. Install with: pip install silero-vad onnxruntime"
                )
            vad_model = load_silero_vad(onnx=True)

        from config import SileroVADConfig as _SileroCfg
        self.router = router
        self.sample_rate = sample_rate
        self.config = config or _SileroCfg()
        self._model = vad_model
        self._frame_samples = 512  # Silero VAD works with 512 samples at 16kHz
        self._frame_bytes = self._frame_samples * 2
        self._bytes_per_sample = 2

        # State machine (mirrors WebRTCVADMeetingTranscriber)
        self._sample_cursor = 0
        self._next_segment_id = 1
        self._next_final_to_emit = 1
        self._active: Optional[UtteranceState] = None
        self._pending_finals: dict[int, Optional[RealtimeTranscriptEvent]] = {}
        self._final_tasks: dict[int, asyncio.Task] = {}
        self._sealed_work: deque[tuple] = deque()
        self._sealed_work_maxlen = 20
        self._on_final_done = None
        self._speech_run = 0
        self._silence_run = 0
        self._speech_buffer = bytearray()
        self._speech_start_sample = 0
        self._capture_start_sample = 0
        self._pre_roll_samples = max(0, int(round(self.config.pre_roll_sec * self.sample_rate)))
        self._recent_frames: deque[BufferedAudioFrame] = deque()
        self._recent_samples = 0

    def _is_speech(self, pcm_chunk: bytes) -> bool:
        """Run Silero VAD on a PCM16 chunk, return True if speech probability > threshold."""
        import torch
        samples = np.frombuffer(pcm_chunk, dtype="<i2").astype(np.float32) / 32768.0
        tensor = torch.from_numpy(samples)
        prob = self._model(tensor, self.sample_rate).item()
        return prob > self.config.speech_threshold

    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for offset in range(0, len(pcm_chunk), self._frame_bytes):
            frame = pcm_chunk[offset:offset + self._frame_bytes]
            if len(frame) != self._frame_bytes:
                break
            events.extend(await self.push_frame(frame, sample_count=self._frame_samples))
        events.extend(await self._drain_finished_tasks())
        return events

    async def push_frame(
        self,
        pcm_chunk: bytes,
        sample_count: Optional[int] = None,
    ) -> list[RealtimeTranscriptEvent]:
        sample_count = sample_count or self._frame_samples
        events: list[RealtimeTranscriptEvent] = []
        is_speech = self._is_speech(pcm_chunk)
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
                self._capture_start_sample, pre_roll_pcm = self._consume_pre_roll()
                self._speech_buffer.extend(pre_roll_pcm)
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
                capture_start_sample=self._capture_start_sample,
                end_sample=None,
                last_speech_sample=frame_end,
                pcm_buffer=bytearray(self._speech_buffer),
            )
            self._next_segment_id += 1
            self._speech_buffer.clear()
            self._clear_recent_frames()
            created_now = True

        if self._active is not None:
            if not created_now:
                self._active.pcm_buffer.extend(pcm_chunk)
            if is_speech:
                self._active.last_speech_sample = frame_end

            utterance_duration_sec = len(self._active.pcm_buffer) / (self.sample_rate * self._bytes_per_sample)
            if self._silence_run >= self.config.endpoint_silence_frames:
                self._seal_active("endpoint")
            elif utterance_duration_sec >= self.config.max_utterance_sec:
                self._seal_active("max_duration")

        events.extend(await self._drain_finished_tasks())
        if self._active is None:
            self._remember_recent_frame(
                BufferedAudioFrame(
                    pcm=pcm_chunk,
                    sample_count=sample_count,
                    is_speech=is_speech,
                    start_sample=frame_start,
                    end_sample=frame_end,
                )
            )
        return events

    async def flush(self, reason: str = "flush") -> list[RealtimeTranscriptEvent]:
        if self._active is None and self._speech_buffer:
            buffer_sec = len(self._speech_buffer) / (self.sample_rate * self._bytes_per_sample)
            max_expected = self.config.max_utterance_sec + self.config.pre_roll_sec + 1.0
            if buffer_sec > max_expected:
                logger.warning("flush: speech_buffer too large (%.1fs), truncating", buffer_sec)
                max_bytes = int(max_expected * self.sample_rate * self._bytes_per_sample)
                self._speech_buffer = self._speech_buffer[-max_bytes:]
            self._active = UtteranceState(
                segment_id=self._next_segment_id,
                revision=0,
                start_sample=self._speech_start_sample,
                capture_start_sample=self._capture_start_sample,
                end_sample=None,
                last_speech_sample=self._sample_cursor,
                pcm_buffer=bytearray(self._speech_buffer),
            )
            self._next_segment_id += 1
            self._speech_buffer.clear()
        if self._active is not None:
            self._seal_active(reason)
        all_events: list[RealtimeTranscriptEvent] = []
        while self._sealed_work or self._final_tasks:
            self._maybe_start_next_final()
            if self._final_tasks:
                await asyncio.gather(*self._final_tasks.values())
            all_events.extend(await self._drain_finished_tasks())
        return all_events

    def _seal_active(self, reason: str) -> None:
        state = self._active
        if state is None or state.sealed:
            return
        state.sealed = True
        state.end_sample = max(state.start_sample, state.last_speech_sample)
        snapshot = bytes(state.pcm_buffer)
        segment_id = state.segment_id
        revision = state.revision + 1
        start_time = state.start_sample / self.sample_rate
        end_time = state.end_sample / self.sample_rate
        capture_start_time = state.capture_start_sample / self.sample_rate
        capture_duration = len(snapshot) / (self.sample_rate * self._bytes_per_sample)
        max_expected = self.config.max_utterance_sec + self.config.pre_roll_sec + 1.0
        if capture_duration > max_expected:
            capture_duration = max_expected

        if len(self._sealed_work) >= self._sealed_work_maxlen:
            dropped = self._sealed_work.popleft()
            logger.warning("Dropping stale utterance segment %d", dropped[1])

        self._sealed_work.append((
            snapshot, segment_id, revision, start_time, end_time,
            capture_start_time, capture_duration, reason,
        ))
        self._active = None
        self._maybe_start_next_final()

    def _maybe_start_next_final(self) -> None:
        if self._final_tasks or not self._sealed_work:
            return
        (snapshot, segment_id, revision, start_time, end_time,
         capture_start_time, capture_duration, reason) = self._sealed_work.popleft()

        audio_sec = len(snapshot) / (self.sample_rate * self._bytes_per_sample)
        if audio_sec < self.config.min_final_audio_sec:
            logger.debug("Segment %d short (%.3fs), sending anyway", segment_id, audio_sec)

        async def run_final():
            try:
                result = await self.router.transcribe_final(
                    pcm16le_to_audio_tuple(snapshot, self.sample_rate)
                )
                text = (result.text or "").strip()
                if not text:
                    return segment_id, None
            except Exception:
                logger.warning("Segment %d transcription failed", segment_id, exc_info=True)
                return segment_id, None

            return segment_id, RealtimeTranscriptEvent(
                event_type="final",
                text=text,
                start_time=start_time,
                end_time=end_time,
                processing_time=float(getattr(result, "processing_time", 0.0) or 0.0),
                segment_id=segment_id,
                revision=revision,
                is_final=True,
                capture_start_time=capture_start_time,
                capture_duration=capture_duration,
                cut_reason=reason,
            )

        task = asyncio.create_task(run_final(), name=f"final-{segment_id}")
        self._final_tasks[segment_id] = task
        if self._on_final_done:
            notify = self._on_final_done
            task.add_done_callback(lambda _t: notify())

    async def _drain_finished_tasks(self) -> list[RealtimeTranscriptEvent]:
        for segment_id, task in list(self._final_tasks.items()):
            if not task.done():
                continue
            _, result = await task
            self._pending_finals[segment_id] = result
            del self._final_tasks[segment_id]
        self._maybe_start_next_final()
        return self._emit_ready_finals_in_order()

    def _consume_pre_roll(self) -> tuple[int, bytes]:
        if self._pre_roll_samples <= 0 or not self._recent_frames:
            return self._speech_start_sample, b""
        selected: list[BufferedAudioFrame] = []
        sample_budget = self._pre_roll_samples
        for frame in reversed(self._recent_frames):
            if sample_budget <= 0:
                break
            selected.append(frame)
            sample_budget -= frame.sample_count
        selected.reverse()
        if not selected:
            return self._speech_start_sample, b""
        return selected[0].start_sample, b"".join(frame.pcm for frame in selected)

    def _remember_recent_frame(self, frame: BufferedAudioFrame) -> None:
        if self._pre_roll_samples <= 0:
            return
        self._recent_frames.append(frame)
        self._recent_samples += frame.sample_count
        while self._recent_frames and self._recent_samples - self._recent_frames[0].sample_count >= self._pre_roll_samples:
            dropped = self._recent_frames.popleft()
            self._recent_samples -= dropped.sample_count

    def _clear_recent_frames(self) -> None:
        self._recent_frames.clear()
        self._recent_samples = 0

    def _emit_ready_finals_in_order(self) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        while self._next_final_to_emit in self._pending_finals:
            result = self._pending_finals.pop(self._next_final_to_emit)
            if result is not None:
                events.append(result)
            self._next_final_to_emit += 1
        return events

    def pipeline_stats(self) -> dict:
        active_ms = 0
        if self._active is not None:
            active_ms = int(
                len(self._active.pcm_buffer) / (self.sample_rate * self._bytes_per_sample) * 1000
            )
        speech_buf_ms = int(
            len(self._speech_buffer) / (self.sample_rate * self._bytes_per_sample) * 1000
        )
        return {
            "asr_in_flight": len(self._final_tasks),
            "asr_queued": len(self._sealed_work),
            "asr_pending_emit": len(self._pending_finals),
            "active_utterance": self._active is not None,
            "active_utterance_ms": active_ms,
            "speech_buffer_ms": speech_buf_ms,
        }

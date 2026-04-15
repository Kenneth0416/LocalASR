"""
VAD-based realtime transcriber using WebRTC Voice Activity Detection.

WebRTCVADMeetingTranscriber manages a state machine that:
  1. Detects speech via WebRTC VAD frame-by-frame
  2. Accumulates audio into utterances
  3. Emits preview transcripts for early display
  4. Commits finalized transcripts when speech ends
"""

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from asr_types import (
    LiteASRResult,
    RealtimeTranscriptEvent,
    UtteranceState,
    pcm16le_to_audio_tuple,
)

from config import WebRTCVADConfig


DEFAULT_PREVIEW_INTERVAL_SEC = 1.2
DEFAULT_MIN_PREVIEW_AUDIO_SEC = 0.8


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
        # Preview timing knobs were removed from WebRTCVADConfig as part of the
        # single-lane ASR rollback, but preview emission behavior stays the same.
        self._preview_interval_sec = float(DEFAULT_PREVIEW_INTERVAL_SEC)
        self._min_preview_audio_sec = float(DEFAULT_MIN_PREVIEW_AUDIO_SEC)
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
        self._pending_preview_events: list[RealtimeTranscriptEvent] = []
        self._speech_run = 0
        self._silence_run = 0
        self._speech_buffer = bytearray()
        self._speech_start_sample = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    async def push_pcm(self, pcm_chunk: bytes) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for offset in range(0, len(pcm_chunk), self._frame_bytes):
            frame = pcm_chunk[offset:offset + self._frame_bytes]
            if len(frame) != self._frame_bytes:
                break
            events.extend(await self.push_frame(frame, sample_count=self._frame_samples))
        # Drain completed final tasks so finalized transcripts arrive as soon as
        # the ASR task completes (not only at flush time).
        events.extend(await self._drain_finished_tasks())
        # Drain completed preview events that arrived while processing frames.
        # This is the non-blocking path: preview ASR ran in the background
        # and its results land here without stalling the audio loop.
        events.extend(self._drain_preview_events())
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
        events = await self._drain_finished_tasks()
        # Flush any in-flight preview results before the session ends.
        events.extend(self._drain_preview_events())
        return events

    # ── Preview emission ────────────────────────────────────────────────────────

    async def _maybe_emit_preview(self) -> Optional[RealtimeTranscriptEvent]:
        state = self._active
        if state is None or state.sealed:
            return None
        if state.preview_task is not None and not state.preview_task.done():
            return None

        current_samples = len(state.pcm_buffer) // self._bytes_per_sample
        current_sec = current_samples / self.sample_rate
        grown_samples = current_samples - state.last_preview_sample
        if current_sec < self._min_preview_audio_sec:
            return None
        if (grown_samples / self.sample_rate) < self._preview_interval_sec:
            return None

        snapshot = bytes(state.pcm_buffer)
        start_time = state.start_sample / self.sample_rate
        end_time = (state.start_sample + current_samples) / self.sample_rate

        # Increment revision and pass it to the background task so all
        # streaming tokens for this preview cycle share the same revision.
        state.revision += 1
        preview_revision = state.revision
        state.first_preview_emitted = True

        # Fire-and-forget: spawn preview task and return immediately.
        # The audio_worker loop will drain _pending_preview_events separately.
        state.preview_task = asyncio.create_task(
            self._run_preview_transcription(
                snapshot, start_time, end_time, state.segment_id, preview_revision
            ),
            name=f"preview-{state.segment_id}",
        )
        state.last_preview_sample = current_samples
        # No immediate event returned — streaming tokens arrive via drain
        # with the correct revision, avoiding the silent-drop bug that
        # occurred when a preview_pending placeholder (empty text) was
        # emitted first and stole the revision number.
        return None

    async def _run_preview_transcription(
        self,
        snapshot: bytes,
        start_time: float,
        end_time: float,
        segment_id: int,
        preview_revision: int,
    ) -> None:
        """Background task — runs preview ASR without blocking the audio loop.

        Uses streaming when available to emit token-by-token partial results,
        giving a typewriter effect for preview text.
        """
        audio_tuple = pcm16le_to_audio_tuple(snapshot, self.sample_rate)
        try:
            async for partial_result in self.router.transcribe_preview_streaming(audio_tuple):
                if not partial_result.text:
                    continue
                # Track latest text for fallback if final transcription fails
                if self._active is not None and self._active.segment_id == segment_id:
                    self._active.last_non_empty_preview_text = partial_result.text
                self._pending_preview_events.append(
                    RealtimeTranscriptEvent(
                        event_type="preview",
                        text=partial_result.text,
                        start_time=start_time,
                        end_time=end_time,
                        processing_time=0.0,
                        segment_id=segment_id,
                        revision=preview_revision,
                        is_final=False,
                        cut_reason="preview_streaming",
                    )
                )
        except Exception:
            return

    # ── Final emission ─────────────────────────────────────────────────────────

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
                cut_reason = reason if (result.text or "").strip() else (
                    "final_fallback" if fallback_text else reason
                )
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

    # ── Task draining ──────────────────────────────────────────────────────────

    async def _drain_finished_tasks(self) -> list[RealtimeTranscriptEvent]:
        for segment_id, task in list(self._final_tasks.items()):
            if not task.done():
                continue
            _, result = await task
            self._pending_finals[segment_id] = result
            del self._final_tasks[segment_id]

        return self._emit_ready_finals_in_order()

    def _drain_preview_events(self) -> list[RealtimeTranscriptEvent]:
        """Return and clear all completed preview events from the background tasks."""
        events = self._pending_preview_events
        self._pending_preview_events = []
        return events

    def _emit_ready_finals_in_order(self) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        while self._next_final_to_emit in self._pending_finals:
            result = self._pending_finals.pop(self._next_final_to_emit)
            if result is not None:
                events.append(result)
            self._next_final_to_emit += 1
        return events

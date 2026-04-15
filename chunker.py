"""
Audio chunkers and realtime transcriber adapters.

Contains:
- RealtimeMeetingTranscriber: fixed-chunk MVP (legacy, kept for test compatibility)
- TransformerChunkingConfig: configuration for transformer-style chunking
- TransformerAudioChunker: semantic-chunking strategy for transformer ASR
- SemanticMeetingTranscriber: adapter exposing TransformerAudioChunker via realtime events
"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import numpy as np

from asr_types import (
    ASRResult,
    ChunkedASREmission,
    RealtimeTranscriptEvent,
    RealtimeTranscriptionConfig,
    RealtimeTranscriptionRequest,
    SemanticCommit,
    TransformerChunkingConfig,
    pcm16le_to_audio_tuple,
)


# ── Fixed-chunk MVP transcriber (legacy, for test compatibility) ───────────────


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
        pcm_bytes: bytes,
        sample_count: int,
        is_speech: bool,
        timestamp: float,
    ) -> list[RealtimeTranscriptionRequest]:
        """Convert a single PCM frame into a transcription request."""
        if not is_speech or not pcm_bytes:
            return []

        self._pending_pcm.extend(pcm_bytes)
        return self._drain_ready_requests(final=False)

    def flush_requests(self, reason: str = "flush") -> list[RealtimeTranscriptionRequest]:
        """Return a request for all remaining buffered audio."""
        if len(self._pending_pcm) < self._min_flush_bytes:
            self._pending_pcm.clear()
            return []

        pcm = bytes(self._pending_pcm)
        start_time = self._buffer_start_bytes / self._bytes_per_second
        end_time = (self._buffer_start_bytes + len(pcm)) / self._bytes_per_second
        self._pending_pcm.clear()
        self._buffer_start_bytes += len(pcm)
        return [RealtimeTranscriptionRequest(
            event_type="final",
            segment_id=self._next_segment_id,
            revision=1,
            is_final=True,
            audio_pcm=pcm,
            start_time=start_time,
            end_time=end_time,
            cut_reason=reason,
        )]

    def _drain_ready_requests(self, final: bool) -> list[RealtimeTranscriptionRequest]:
        requests: list[RealtimeTranscriptionRequest] = []
        while len(self._pending_pcm) >= self._chunk_bytes:
            chunk_pcm = bytes(self._pending_pcm[:self._chunk_bytes])
            del self._pending_pcm[:self._chunk_bytes]
            start_time = self._buffer_start_bytes / self._bytes_per_second
            end_time = (self._buffer_start_bytes + len(chunk_pcm)) / self._bytes_per_second
            self._buffer_start_bytes += len(chunk_pcm)
            requests.append(RealtimeTranscriptionRequest(
                event_type="final",
                segment_id=self._next_segment_id,
                revision=1,
                is_final=True,
                audio_pcm=chunk_pcm,
                start_time=start_time,
                end_time=end_time,
                cut_reason="fixed_chunk",
            ))
            self._next_segment_id += 1
        return requests

    async def _transcribe_requests(
        self,
        requests: list[RealtimeTranscriptionRequest],
    ) -> list[RealtimeTranscriptEvent]:
        events: list[RealtimeTranscriptEvent] = []
        for request in requests:
            result = await self._transcribe_wav(request.audio_pcm)
            text = (result.text or "").strip()
            events.append(RealtimeTranscriptEvent(
                event_type="final",
                text=text,
                start_time=request.start_time,
                end_time=request.end_time,
                processing_time=result.processing_time,
                segment_id=request.segment_id,
                revision=1,
                is_final=True,
                cut_reason=request.cut_reason,
            ))
        return events

    async def _transcribe_wav(self, pcm_data: bytes) -> ASRResult:
        return await self._transcribe_wav(
            pcm16le_to_audio_tuple(pcm_data, sample_rate=self.sample_rate)
        )

    @staticmethod
    def _align_pcm_bytes(value: int) -> int:
        return max(0, value - (value % 2))


# ── Transformer audio chunker ──────────────────────────────────────────────────


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
        if pcm_chunk:
            self._buffer.extend(pcm_chunk)
        return await self._drain_ready_chunks(final=False)

    async def flush(self) -> list[ChunkedASREmission]:
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
                    emissions.append(ChunkedASREmission(
                        text=emission_text,
                        audio_duration=semantic_commit.audio_duration,
                        processing_time=result.processing_time,
                    ))
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
                emissions.append(ChunkedASREmission(
                    text=emission_text,
                    audio_duration=advanced_bytes / self._bytes_per_second,
                    processing_time=result.processing_time,
                ))
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

        decode_duration = (
            result.audio_duration if result.audio_duration > 0 else boundary / self._bytes_per_second
        )
        segments = self._normalize_timed_segments(result.segments, decode_duration)
        if not segments:
            if final:
                return SemanticCommit(
                    text=(result.text or "").strip(),
                    committed_bytes=boundary,
                    audio_duration=decode_duration,
                )
            return SemanticCommit(text="", committed_bytes=0, audio_duration=0.0, should_wait=True)

        search_limit = decode_duration if final else max(
            0.0, decode_duration - self.config.semantic_right_guard_sec
        )
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
            normalized.append({"start": start, "end": end, "text": text})
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


# ── Semantic meeting transcriber ───────────────────────────────────────────────


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
            events.append(RealtimeTranscriptEvent(
                event_type="final",
                text=text,
                start_time=start_time,
                end_time=end_time,
                processing_time=emission.processing_time,
                segment_id=self._next_segment_id,
                revision=1,
                is_final=True,
                cut_reason=cut_reason,
            ))
            self._next_segment_id += 1
        return events

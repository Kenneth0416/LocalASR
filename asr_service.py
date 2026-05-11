"""
ASR Service — MLX-only Qwen3-ASR transcription.

All CPU/GPU-bound inference runs in a shared thread pool executor so it never
blocks the asyncio event loop.
"""

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Optional

import numpy as np

from asr_utils import normalize_language_name, parse_asr_output, validate_language
from config import ASRConfig
from text_utils import strip_hallucination

from asr_types import ASRResult

logger = logging.getLogger("meeting.asr")

# Global executor for CPU-bound ASR tasks
_asr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr-worker")


class ASRServiceError(RuntimeError):
    """Raised when the ASR pipeline cannot transcribe audio."""


class ASRService:
    """
    Qwen3-ASR Service for real-time and batch transcription (MLX-only).
    Non-blocking design — uses thread pool for inference.
    """

    def __init__(self, config: ASRConfig):
        self.config = config
        self._initialized = False
        self._init_task: Optional[asyncio.Task] = None
        self._init_error: Optional[BaseException] = None
        self._mlx_transcriber = None

    # ── Initialization ────────────────────────────────────────────────────────

    async def initialize(self):
        """Initialize MLX ASR model in background."""
        if self._initialized:
            return

        if self._init_task is not None and not self._init_task.done():
            return

        self._init_error = None

        async def _load_model():
            loop = asyncio.get_running_loop()
            logger.info("Loading MLX ASR model")
            await loop.run_in_executor(_asr_executor, self._load_mlx_sync)
            self._initialized = True
            logger.info("MLX ASR backend ready")

        self._init_task = asyncio.create_task(_load_model(), name="asr-initialize")
        self._init_task.add_done_callback(self._handle_init_task_done)

    async def wait_ready(self, timeout: Optional[float] = None):
        """Wait for model to be ready."""
        if timeout is None:
            timeout = self.config.init_timeout_sec

        if self._init_task is None or (self._init_task.done() and not self._initialized):
            await self.initialize()

        try:
            await asyncio.wait_for(self._init_task, timeout=timeout)
            mlx_ready = self._mlx_transcriber is not None and self._mlx_transcriber.is_loaded
            self._initialized = mlx_ready
            if not self._initialized:
                raise ASRServiceError("MLX ASR model did not finish initialization")
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
        mlx_ready = self._mlx_transcriber is not None and self._mlx_transcriber.is_loaded
        if self._initialized and mlx_ready:
            return "ready"
        if self._init_task is not None and not self._init_task.done():
            return "loading"
        if self._init_error is not None:
            return "failed"
        return "idle"

    def initialization_error(self) -> Optional[str]:
        """Return the latest initialization error message, if any."""
        return str(self._init_error) if self._init_error is not None else None

    async def shutdown(self):
        """Cancel background initialization if the process is shutting down."""
        task = self._init_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        _asr_executor.shutdown(wait=False)

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

    # ── Model loading ─────────────────────────────────────────────────────────

    def _load_mlx_sync(self):
        """Load MLX backend. Runs in thread pool."""
        from mlx_transcriber import MLXTranscriber

        try:
            self._mlx_transcriber = MLXTranscriber(
                model_path=self.config.mlx_model_path,
                max_new_tokens=self.config.mlx_max_new_tokens,
                chunk_sec=self.config.upload_chunk_sec,
                temperature=self.config.mlx_temperature,
                repetition_penalty=self.config.mlx_repetition_penalty,
                repetition_context_size=self.config.mlx_repetition_context_size,
            )
            self._mlx_transcriber.load()
            logger.info("MLX ASR backend loaded")
        except Exception as e:
            logger.error("MLX backend failed to load: %s", e, exc_info=True)
            self._mlx_transcriber = None
            raise ASRServiceError(f"MLX backend failed: {e}") from e

    # ── Transcription ─────────────────────────────────────────────────────────

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> ASRResult:
        """Transcribe audio data. Non-blocking — runs MLX in thread pool."""
        if not self._initialized or self._mlx_transcriber is None:
            await self.wait_ready()
        return await self._transcribe_wav_mlx(audio_data)

    async def transcribe_wav(
        self,
        wav_source,
        language: Optional[str] = None,
        context: Optional[str] = None,
        extract_timestamps: bool = True,
    ) -> ASRResult:
        """Transcribe a WAV file or WAV bytes."""
        if not self._initialized or self._mlx_transcriber is None:
            await self.wait_ready()
        return await self._transcribe_wav_mlx(wav_source, language)

    async def _transcribe_wav_mlx(
        self,
        wav_source,
        language: Optional[str] = None,
    ) -> ASRResult:
        """MLX transcription path — used by realtime and upload."""
        import time
        wall_start = time.time()
        loop = asyncio.get_running_loop()

        # Read audio
        audio, sr = await loop.run_in_executor(
            _asr_executor, self._read_audio_source, wav_source
        )
        duration = len(audio) / sr if sr else 0.0

        # Run MLX transcription in thread pool
        segments = await loop.run_in_executor(
            _asr_executor,
            self._mlx_transcriber.transcribe_audio,
            audio,
            sr,
            language,
        )

        full_text = "".join(s["text"] for s in segments)
        elapsed = time.time() - wall_start
        logger.info(
            "MLX transcribe_wav: %d chars, %.1fs, RTF=%.3f",
            len(full_text),
            elapsed,
            elapsed / duration if duration > 0 else 0,
        )
        return ASRResult(
            text=full_text,
            segments=segments,
            audio_duration=duration,
            processing_time=elapsed,
            has_timestamps=bool(segments),
        )

    # ── Audio reading ─────────────────────────────────────────────────────────

    def _read_audio_source(self, wav_source):
        """Read audio source into a mono float32 waveform and sample rate.

        Supported formats:
        - WAV / FLAC / OGG: read directly via soundfile
        - MP3 / M4A / other: transcoded to 16kHz mono PCM WAV via FFmpeg
        """
        import soundfile as sf

        if isinstance(wav_source, tuple) and len(wav_source) == 2:
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
        from utils.audio_transcoder import transcode_to_wav_pcm

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
        from utils.audio_transcoder import transcode_to_wav_pcm

        try:
            audio, sr = sf.read(source, dtype="float32")
            return audio, sr
        except Exception:
            with open(source, "rb") as f:
                content = f.read()
            wav_bytes = transcode_to_wav_pcm(content, filename=source)
            audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            return audio, sr

    # ── Batched upload transcription ──────────────────────────────────────────

    async def transcribe_wav_batched(
        self,
        wav_source,
        language: Optional[str] = None,
        context: Optional[str] = None,
        target_chunk_sec: Optional[float] = None,
        batch_size: Optional[int] = None,
        min_audio_sec: Optional[float] = None,
        num_workers: Optional[int] = None,
    ) -> ASRResult:
        """Transcribe long audio using MLX with energy-based chunking.

        Falls back to single-pass MLX transcription for short files.
        """
        if not self._initialized or self._mlx_transcriber is None:
            await self.wait_ready()

        cfg = self.config
        min_audio_sec = min_audio_sec if min_audio_sec is not None else cfg.upload_min_audio_sec

        loop = asyncio.get_running_loop()

        # Read audio
        audio, sr = await loop.run_in_executor(
            _asr_executor, self._read_audio_source, wav_source
        )
        duration = len(audio) / sr if sr else 0.0

        import time
        wall_start = time.time()

        if duration < min_audio_sec:
            # Short file: single-pass MLX
            segments = await loop.run_in_executor(
                _asr_executor,
                self._mlx_transcriber.transcribe_audio,
                audio,
                sr,
                language,
            )
        else:
            # Long file: write temp WAV, use MLX chunked transcription
            import tempfile
            import wave

            tmp_wav = tempfile.mktemp(suffix=".wav")
            try:
                with wave.open(tmp_wav, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    wf.writeframesraw(
                        (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
                    )

                segments = await loop.run_in_executor(
                    _asr_executor,
                    self._mlx_transcriber.transcribe,
                    tmp_wav,
                    language,
                )
            finally:
                if os.path.exists(tmp_wav):
                    os.remove(tmp_wav)

        all_texts = [s["text"] for s in segments]
        full_text = "".join(all_texts)
        elapsed = time.time() - wall_start
        logger.info(
            "MLX upload: %d chars, %.1fs (%.1f min audio)",
            len(full_text),
            elapsed,
            duration / 60,
        )
        return ASRResult(
            text=full_text,
            segments=segments,
            audio_duration=duration,
            processing_time=elapsed,
            has_timestamps=bool(segments),
        )

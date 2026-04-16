"""
ASR Service - Qwen3-ASR model loading and non-blocking transcription.

All CPU-bound inference runs in a shared thread pool executor so it never
blocks the asyncio event loop.
"""

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Optional

import numpy as np
import torch

from alignment import load_alignment_model
from config import ASRConfig

from asr_types import ASRResult

logger = logging.getLogger("meeting.asr")

# Global executor for CPU-bound ASR tasks
_asr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr-worker")

# Serialize model loading to prevent a PyTorch thread-safety bug on Python 3.14:
# concurrent AutoModel.from_pretrained() calls cause the second model's
# parameters to land on the meta device instead of CPU.
_model_load_lock = threading.Lock()


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
        # A single ASRService may serve both preview and final inference during
        # the single-service transition. Serialize model access per service so
        # concurrent generate() calls never race on the same model object.
        self._inference_lock = threading.Lock()
        self._initialized = False
        self._init_task: Optional[asyncio.Task] = None
        self._init_error: Optional[BaseException] = None

    # ── Initialization ────────────────────────────────────────────────────────

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

    def initialization_error(self) -> Optional[str]:
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

    # ── Model loading ──────────────────────────────────────────────────────────

    def _load_model_sync(self):
        """Synchronous model loading - runs in thread pool"""
        from utils.audio_transcoder import transcode_to_wav_pcm

        with self._model_lock:
            if self._model is not None:
                return

        # Serialize across ASRService instances to avoid PyTorch meta-tensor bug
        # when two models load concurrently on Python 3.14.
        with _model_load_lock:
            with self._model_lock:
                if self._model is not None:
                    return

                try:
                    from qwen_asr import Qwen3ASRModel

                    model_path = self.config.model_path
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
                    # On Python 3.13+, AutoModel.from_pretrained() creates meta tensors
                    # by default for memory efficiency. This breaks inference because
                    # parameters have no data. Force disable meta tensor initialization.
                    if device in ("cpu", "mps"):
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

                    self._model = Qwen3ASRModel.from_pretrained(model_path, **load_kwargs)
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
                            "Current MPS runtime is unstable for this Model; prefer Python 3.11/3.12 and retry on CPU or CUDA."
                        )
                    logger.error("ASR model load error: %s", error_message, exc_info=True)
                    raise ASRServiceError(error_message) from e

    def _resolve_attention_implementation(self) -> str:
        """Match the official CUDA recommendation when flash-attn is available."""
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

    # ── Transcription ─────────────────────────────────────────────────────────

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> ASRResult:
        """Transcribe audio data. Non-blocking - runs in thread pool."""
        if not self._initialized or self._model is None or self._processor is None:
            await self.wait_ready()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _asr_executor,
            self._transcribe_sync,
            audio_data,
            sample_rate,
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
        language: Optional[str] = None,
        context: Optional[str] = None,
    ) -> ASRResult:
        """Transcribe a WAV file or WAV bytes"""
        if not self._initialized or self._model is None or self._processor is None:
            await self.wait_ready()
        loop = asyncio.get_running_loop()
        call = partial(self._transcribe_wav_sync, wav_source, language, context)
        return await loop.run_in_executor(_asr_executor, call)

    def _transcribe_wav_sync(
        self,
        wav_source,
        language: Optional[str] = None,
        context: Optional[str] = None,
    ) -> ASRResult:
        """Synchronous WAV transcription - accepts file path or bytes"""
        import time
        start_time = time.time()
        try:
            audio, sr = self._read_audio_source(wav_source)
            return self._transcribe_audio_array(
                audio, sr, start_time=start_time, language=language, context=context
            )
        except Exception as e:
            logger.error(f"WAV transcription error: {e}")
            raise ASRServiceError(f"WAV transcription failed: {e}") from e

    # ── Streaming transcription ───────────────────────────────────────────────

    def _run_model_generate(self, hf_model, inputs, *, streamer=None):
        kwargs = {
            **inputs,
            "max_new_tokens": self.config.max_new_tokens,
            "do_sample": False,
            "repetition_penalty": 1.1,
        }
        if streamer is not None:
            kwargs["streamer"] = streamer

        with self._inference_lock:
            with torch.no_grad():
                return hf_model.generate(**kwargs)

    def _run_model_transcribe(
        self,
        qwen_model,
        *,
        audio: np.ndarray,
        sample_rate: int,
        context: Optional[str],
        language: Optional[str],
    ):
        with self._inference_lock:
            return qwen_model.transcribe(
                audio=(audio, sample_rate),
                context=(context or "").strip(),
                language=language,
                return_time_stamps=True,
            )

    async def transcribe_wav_streaming(
        self,
        wav_source,
        language: Optional[str] = None,
        context: Optional[str] = None,
    ) -> AsyncIterator[ASRResult]:
        """Async generator yielding partial ASRResult as decoder tokens arrive.

        Uses HuggingFace TextIteratorStreamer to get token-by-token output from
        the underlying model.generate() call, giving a typewriter effect for
        preview transcriptions.
        """
        if not self._initialized or self._model is None or self._processor is None:
            await self.wait_ready()

        loop = asyncio.get_running_loop()

        # Prepare model inputs in thread pool (fast: audio read + processor)
        inputs, force_language, duration = await loop.run_in_executor(
            _asr_executor,
            self._prepare_streaming_inputs,
            wav_source,
            language,
            context,
        )

        from transformers import TextIteratorStreamer

        tokenizer = self._model.processor.tokenizer
        streamer = TextIteratorStreamer(
            tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )

        # Run model.generate() in thread pool — it writes tokens into streamer
        hf_model = self._model.model

        def _run_generate():
            self._run_model_generate(hf_model, inputs, streamer=streamer)

        gen_future = loop.run_in_executor(_asr_executor, _run_generate)

        # Read from streamer token-by-token and yield partial results
        from qwen_asr.inference.utils import parse_asr_output

        it = iter(streamer)
        partial_raw = ""
        _ASR_TEXT_TAG = "<asr_text>"
        # When language is forced, the prompt already includes the tag prefix,
        # so generated tokens are pure text — no need to wait for tag.
        tag_found = force_language is not None

        try:
            while True:
                try:
                    new_text = await loop.run_in_executor(None, next, it)
                except StopIteration:
                    break

                partial_raw += new_text

                if not tag_found:
                    if _ASR_TEXT_TAG in partial_raw:
                        tag_found = True
                    else:
                        continue

                _, parsed_text = parse_asr_output(partial_raw, user_language=force_language)
                if parsed_text:
                    yield ASRResult(
                        text=parsed_text,
                        segments=[],
                        audio_duration=duration,
                        processing_time=0.0,
                        has_timestamps=False,
                    )
        finally:
            await gen_future

    def _prepare_streaming_inputs(self, wav_source, language, context):
        """Build processor inputs for streaming generate. Runs in thread pool."""
        audio, sr = self._read_audio_source(wav_source)

        qwen_model = self._model
        processor = qwen_model.processor

        effective_language = (language if language is not None else self.config.language) or None
        force_language = None
        if effective_language:
            from qwen_asr.inference.utils import normalize_language_name, validate_language
            try:
                ln = normalize_language_name(effective_language)
                validate_language(ln)
                force_language = ln
            except (ValueError, KeyError):
                logger.warning(
                    "Unrecognized language '%s', falling back to auto-detection",
                    effective_language,
                )
                force_language = None

        text_prompt = qwen_model._build_text_prompt(
            context=(context or "").strip(),
            force_language=force_language,
        )

        wav_array = audio.astype(np.float32)
        inputs = processor(
            text=[text_prompt],
            audio=[wav_array],
            return_tensors="pt",
            padding=True,
        )
        inputs = inputs.to(qwen_model.model.device).to(qwen_model.model.dtype)

        duration = len(audio) / sr if sr else 0.0
        return inputs, force_language, duration

    # ── Audio reading ─────────────────────────────────────────────────────────

    def _read_audio_source(self, wav_source):
        """Read audio source into a mono float32 waveform and sample rate.

        Supported formats:
        - WAV / FLAC / OGG: read directly via soundfile
        - MP3 / M4A / other: transcoded to 16kHz mono PCM WAV via FFmpeg
        """
        import io
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

    def _transcribe_audio_array(
        self,
        audio: np.ndarray,
        sample_rate: int,
        start_time: float,
        language: Optional[str] = None,
        context: Optional[str] = None,
    ) -> ASRResult:
        """Run Qwen3-ASR on a waveform array using direct model.generate()."""
        import time

        qwen_model = self._model
        processor = qwen_model.processor

        effective_language = (language if language is not None else self.config.language) or None
        force_language = None
        if effective_language:
            from qwen_asr.inference.utils import normalize_language_name, validate_language
            try:
                ln = normalize_language_name(effective_language)
                validate_language(ln)
                force_language = ln
            except (ValueError, KeyError):
                logger.warning(
                    "Unrecognized language '%s', falling back to auto-detection",
                    effective_language,
                )
                force_language = None

        text_prompt = qwen_model._build_text_prompt(
            context=(context or "").strip(),
            force_language=force_language,
        )
        wav_array = audio.astype(np.float32)
        inputs = processor(
            text=[text_prompt],
            audio=[wav_array],
            return_tensors="pt",
            padding=True,
        )
        inputs = inputs.to(qwen_model.model.device).to(qwen_model.model.dtype)

        output_ids = self._run_model_generate(qwen_model.model, inputs)

        # Decode only the generated tokens (skip the prompt)
        generated = output_ids.sequences[:, inputs["input_ids"].shape[1]:]
        decoded = processor.batch_decode(
            generated,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )

        from qwen_asr.inference.utils import parse_asr_output
        _, text = parse_asr_output(decoded[0], user_language=force_language)
        text = text.strip()

        duration = len(audio) / sample_rate if sample_rate else 0.0

        # Extract timestamps if forced aligner is available
        use_timestamps = getattr(qwen_model, "forced_aligner", None) is not None
        segments = []
        has_timestamps = False
        if use_timestamps and text:
            try:
                results_fallback = self._run_model_transcribe(
                    qwen_model,
                    audio=audio,
                    sample_rate=sample_rate,
                    context=context,
                    language=effective_language,
                )
                if results_fallback:
                    time_stamps = getattr(results_fallback[0], "time_stamps", None)
                    segments = self._extract_segments_from_timestamps(time_stamps, text=text, duration=duration)
                    has_timestamps = bool(
                        segments and not self._is_fallback_segment(segments, text, duration)
                    )
            except Exception:
                pass

        return ASRResult(
            text=text,
            segments=segments,
            audio_duration=duration,
            processing_time=time.time() - start_time,
            has_timestamps=has_timestamps,
        )

    # ── Timestamp helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _is_cjk_char(ch: str) -> bool:
        return "\u4e00" <= ch <= "\u9fff"

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

    @staticmethod
    def _extract_segments_from_timestamps(time_stamps, text: str, duration: float) -> list[dict]:
        """Return grouped user-facing transcript segments from forced-aligner timestamps.

        The forced aligner emits low-level items, typically one per word for
        space-delimited languages or one per character for CJK text. This helper
        merges those items into transcript segments using punctuation boundaries,
        pause gaps, and a maximum segment duration cap.

        Text reconstruction keeps CJK compact by joining directly, while
        non-CJK text inserts spaces between consecutive items.
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
            return [{"start": 0.0, "end": duration, "text": text}]

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
            if not texts:
                return ""
            parts: list[str] = []
            for t in texts:
                if parts:
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
                    segments.append({"start": seg_start, "end": seg_end, "text": seg_text})
                seg_start = raw_items[i][0]
                seg_end = raw_items[i][1]
                seg_texts = [curr_text]
            else:
                seg_end = raw_items[i][1]
                seg_texts.append(curr_text)

        seg_text = build_segment_text(seg_texts)
        if seg_text.strip():
            segments.append({"start": seg_start, "end": seg_end, "text": seg_text})

        if segments:
            return segments

        return [{"start": 0.0, "end": duration, "text": text}]

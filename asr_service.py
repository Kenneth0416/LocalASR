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


def _get_spawn_context():
    """Return a multiprocessing context using 'spawn' (safe for MPS/CUDA)."""
    import multiprocessing
    return multiprocessing.get_context("spawn")


def _upload_worker_fn(args):
    """
    Module-level worker function for parallel upload transcription.

    Runs inside a spawned subprocess: loads its own model, transcribes the
    assigned chunks, and returns a list of {start, end, text} dicts.

    args tuple:
        audio       : np.ndarray  - full float32 waveform
        sr          : int         - sample rate (16000)
        assignments : list of (start_sample, end_sample, start_sec, end_sec)
        model_path  : str
        device      : str
        language    : str
        context     : str
        max_new_tokens : int
        worker_id   : int
    """
    import asyncio
    import os
    import sys
    import time

    (audio, sr, assignments, model_path, device,
     language, context, max_new_tokens, worker_id) = args

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from config import ASRConfig
    # Import ASRService from the same module (avoid circular via sys.modules)
    from asr_service import ASRService

    import logging
    logging.basicConfig(level=logging.WARNING)

    async def run() -> list:
        cfg = ASRConfig(
            model_path=model_path,
            device=device,
            max_new_tokens=max_new_tokens,
        )
        svc = ASRService(cfg)
        await svc.initialize()
        await svc.wait_ready()

        results = []
        for start_sample, end_sample, start_sec, end_sec in assignments:
            chunk = audio[start_sample:end_sample]
            t0 = time.time()
            from functools import partial as _partial
            from asr_service import _asr_executor as _exec
            loop = asyncio.get_running_loop()
            call = _partial(
                svc._transcribe_audio_array,
                chunk,
                sr,
                t0,
                language or None,
                context or None,
                False,
            )
            result = await loop.run_in_executor(_exec, call)
            text = (result.text or "").strip()
            if text:
                results.append({"start": start_sec, "end": end_sec, "text": text})

        return results

    return asyncio.run(run())


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
        # MLX backend (optional, for upload transcription)
        self._mlx_transcriber = None

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
            try:
                await loop.run_in_executor(_asr_executor, self._load_model_sync)
            except Exception:
                logger.warning("PyTorch ASR init failed, will try MLX fallback if enabled")

            # Optionally load MLX backend for upload transcription
            if self.config.upload_use_mlx:
                try:
                    await loop.run_in_executor(_asr_executor, self._load_mlx_sync)
                except Exception:
                    logger.error("MLX backend also failed to load")
                    raise

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

                device = self.config.device  # default, used in error handler
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
                    self._initialized = True

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

    def _load_mlx_sync(self):
        """Load MLX backend for upload transcription. Runs in thread pool."""
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
            logger.info("MLX backend ready for upload transcription")
        except Exception as e:
            logger.error("MLX backend failed to load: %s", e, exc_info=True)
            self._mlx_transcriber = None
            raise ASRServiceError(f"MLX backend failed: {e}") from e

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
        # MLX fast path
        if self._mlx_transcriber is not None and self._mlx_transcriber.is_loaded:
            return await self._transcribe_wav_mlx(audio_data)

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
        extract_timestamps: bool = True,
    ) -> ASRResult:
        """Transcribe a WAV file or WAV bytes"""
        # MLX fast path (no context support; extract_timestamps always False)
        if self._mlx_transcriber is not None and self._mlx_transcriber.is_loaded:
            return await self._transcribe_wav_mlx(wav_source, language)

        if not self._initialized or self._model is None or self._processor is None:
            await self.wait_ready()
        loop = asyncio.get_running_loop()
        call = partial(self._transcribe_wav_sync, wav_source, language, context, extract_timestamps)
        return await loop.run_in_executor(_asr_executor, call)

    def _transcribe_wav_sync(
        self,
        wav_source,
        language: Optional[str] = None,
        context: Optional[str] = None,
        extract_timestamps: bool = True,
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
                extract_timestamps=extract_timestamps,
            )
        except Exception as e:
            logger.error(f"WAV transcription error: {e}")
            raise ASRServiceError(f"WAV transcription failed: {e}") from e

    async def _transcribe_wav_mlx(
        self,
        wav_source,
        language: Optional[str] = None,
    ) -> ASRResult:
        """MLX fast path for transcribe_wav — used by realtime and upload."""
        import time
        wall_start = time.time()
        loop = asyncio.get_running_loop()

        # Read audio (same pipeline as PyTorch path)
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
        extract_timestamps: bool = True,
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
        use_timestamps = extract_timestamps and getattr(qwen_model, "forced_aligner", None) is not None
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

    # ── Batched upload transcription ─────────────────────────────────────────

    async def transcribe_wav_batched(
        self,
        wav_source,
        language: Optional[str] = None,
        context: Optional[str] = None,
        target_chunk_sec: Optional[float] = None,
        batch_size: Optional[int] = None,
        min_audio_sec: Optional[float] = None,
        num_workers: Optional[int] = None,
    ) -> "ASRResult":
        """Transcribe long audio using VAD-based ~2-min chunking.

        Splits the audio at silence boundaries into chunks of roughly
        ``target_chunk_sec`` (default 120 s / 2 min), then:
          - ``num_workers=1``: runs chunks sequentially in this process.
          - ``num_workers>1``: spawns worker processes that each load their own
            model instance and run their assigned chunks in parallel.

        Falls back to single-pass ``transcribe_wav`` for short files.
        """
        cfg = self.config

        # MLX fast path does not need PyTorch model
        use_mlx = cfg.upload_use_mlx and self._mlx_transcriber is not None
        if not use_mlx and (not self._initialized or self._model is None or self._processor is None):
            await self.wait_ready()
        target_chunk_sec = target_chunk_sec if target_chunk_sec is not None else cfg.upload_chunk_sec
        batch_size = batch_size if batch_size is not None else cfg.upload_batch_size
        min_audio_sec = min_audio_sec if min_audio_sec is not None else cfg.upload_min_audio_sec
        num_workers = num_workers if num_workers is not None else cfg.upload_workers
        # Per-chunk token budget — must cover ~120s of dense CJK speech (≥400 chars)
        max_new_tokens = cfg.upload_max_new_tokens

        loop = asyncio.get_running_loop()

        # Read audio and decide path ──────────────────────────────────────────
        audio, sr = await loop.run_in_executor(
            _asr_executor, self._read_audio_source, wav_source
        )
        duration = len(audio) / sr if sr else 0.0

        if duration < min_audio_sec:
            # Short file: reuse existing single-pass path
            import time
            call = partial(
                self._transcribe_audio_array,
                audio,
                sr,
                time.time(),
                language,
                context,
                True,
            )
            return await loop.run_in_executor(_asr_executor, call)

        # ── MLX fast path ─────────────────────────────────────────────────────
        if self._mlx_transcriber is not None and self._mlx_transcriber.is_loaded:
            import time
            wall_start = time.time()

            # Write temporary WAV for MLX audio reader
            import tempfile, wave
            tmp_wav = tempfile.mktemp(suffix=".wav")
            try:
                with wave.open(tmp_wav, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    wf.writeframesraw((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())

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
            logger.info("MLX upload: %d chars, %.1fs", len(full_text), elapsed)
            return ASRResult(
                text=full_text,
                segments=segments,
                audio_duration=duration,
                processing_time=elapsed,
                has_timestamps=bool(segments),
            )

        # ── PyTorch path ──────────────────────────────────────────────────────
        import time
        wall_start = time.time()

        chunks: list[tuple[float, float, np.ndarray]] = await loop.run_in_executor(
            _asr_executor,
            self._vad_split_to_chunks,
            audio,
            sr,
            target_chunk_sec,
        )
        logger.info(
            "Upload VAD split: %.1fs audio → %d chunks (target %.0fs each, workers=%d)",
            duration,
            len(chunks),
            target_chunk_sec,
            num_workers,
        )

        # Choose serial or parallel path ──────────────────────────────────────
        if num_workers > 1:
            all_segments = await self._transcribe_chunks_parallel(
                chunks, audio, sr, language, context, max_new_tokens, num_workers, loop
            )
        else:
            all_segments = await self._transcribe_chunks_serial(
                chunks, batch_size, language, context, max_new_tokens, loop
            )

        all_texts = [s["text"] for s in all_segments if s.get("text")]
        full_text = " ".join(all_texts)
        return ASRResult(
            text=full_text,
            segments=all_segments,
            audio_duration=duration,
            processing_time=time.time() - wall_start,
            has_timestamps=bool(all_segments),
        )

    async def _transcribe_chunks_serial(
        self,
        chunks: "list[tuple[float, float, np.ndarray]]",
        batch_size: int,
        language: Optional[str],
        context: Optional[str],
        max_new_tokens: int,
        loop,
    ) -> "list[dict]":
        """Sequential chunk inference in the current process."""
        all_segments: list[dict] = []

        for batch_start in range(0, len(chunks), batch_size):
            batch = chunks[batch_start : batch_start + batch_size]
            batch_audios = [c[2] for c in batch]

            inputs, force_language = await loop.run_in_executor(
                _asr_executor,
                self._prepare_batch_inputs,
                batch_audios,
                language,
                context,
            )
            output_ids = await loop.run_in_executor(
                _asr_executor,
                self._run_batch_generate_tokens,
                inputs,
                max_new_tokens,
            )
            batch_texts = await loop.run_in_executor(
                _asr_executor,
                self._decode_batch_output,
                output_ids,
                inputs,
                force_language,
            )

            for text, (chunk_start, chunk_end, _) in zip(batch_texts, batch):
                text = text.strip()
                if text:
                    all_segments.append(
                        {"start": chunk_start, "end": chunk_end, "text": text}
                    )

            logger.debug(
                "Serial batch %d-%d done (%d chunks)",
                batch_start,
                batch_start + len(batch) - 1,
                len(batch),
            )

        return all_segments

    async def _transcribe_chunks_parallel(
        self,
        chunks: "list[tuple[float, float, np.ndarray]]",
        audio: np.ndarray,
        sr: int,
        language: Optional[str],
        context: Optional[str],
        max_new_tokens: int,
        num_workers: int,
        loop,
    ) -> "list[dict]":
        """Distribute chunks across ``num_workers`` spawned processes.

        Each worker process loads its own model instance and processes its
        assigned chunks sequentially.  Results are merged back in order.
        """
        import concurrent.futures
        import os

        # Build chunk boundaries (sample indices + time offsets)
        chunk_boundaries: list[tuple[int, int, float, float]] = []
        for start_sec, end_sec, chunk_arr in chunks:
            start_sample = int(start_sec * sr)
            end_sample = start_sample + len(chunk_arr)
            chunk_boundaries.append((start_sample, end_sample, start_sec, end_sec))

        # Split chunk list across workers
        n = len(chunk_boundaries)
        worker_assignments: list[list[tuple]] = [[] for _ in range(num_workers)]
        for i, cb in enumerate(chunk_boundaries):
            worker_assignments[i % num_workers].append(cb)

        # Use upload-specific model if configured (e.g. 0.6B for speed)
        upload_model = (self.config.upload_model_path or "").strip()
        model_path = upload_model if upload_model else self.config.model_path
        device = self.config.device
        if upload_model:
            logger.info("Upload workers using dedicated model: %s", model_path)

        worker_args = [
            (
                audio,
                sr,
                assignment,
                model_path,
                device,
                language or "",
                context or "",
                max_new_tokens,
                worker_id,
            )
            for worker_id, assignment in enumerate(worker_assignments)
            if assignment
        ]

        logger.info(
            "Spawning %d worker processes for %d chunks",
            len(worker_args),
            n,
        )

        # Run workers in a process pool (spawn avoids fork/MPS issues)
        ctx = _get_spawn_context()
        executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=num_workers,
            mp_context=ctx,
        )
        try:
            futures = [
                loop.run_in_executor(executor, _upload_worker_fn, args)
                for args in worker_args
            ]
            worker_results: list[list[dict]] = await asyncio.gather(*futures)
        finally:
            executor.shutdown(wait=False)

        # Merge results preserving chronological order
        all_results: list[dict] = []
        for result_list in worker_results:
            all_results.extend(result_list)
        all_results.sort(key=lambda s: s["start"])
        return all_results

    def _vad_split_to_chunks(
        self,
        audio: np.ndarray,
        sample_rate: int,
        target_chunk_sec: float = 120.0,
    ) -> "list[tuple[float, float, np.ndarray]]":
        """Split audio into ~target_chunk_sec chunks at VAD silence boundaries.

        Returns list of (start_sec, end_sec, audio_array).
        """
        import webrtcvad

        # If audio shorter than one target chunk, return as-is
        if len(audio) / sample_rate <= target_chunk_sec:
            return [(0.0, len(audio) / sample_rate, audio)]

        frame_ms = 20
        frame_samples = int(sample_rate * frame_ms / 1000)
        frame_bytes = frame_samples * 2  # 16-bit PCM

        # Convert float32 → int16 for WebRTC VAD
        audio_i16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        pcm_bytes = audio_i16.tobytes()

        vad = webrtcvad.Vad(2)  # moderate aggressiveness
        total_frames = len(pcm_bytes) // frame_bytes

        # Find frames where there is silence
        silence_frame_indices: list[int] = []
        for i in range(total_frames):
            frame = pcm_bytes[i * frame_bytes : (i + 1) * frame_bytes]
            if len(frame) < frame_bytes:
                break
            if not vad.is_speech(frame, sample_rate):
                silence_frame_indices.append(i)

        silence_set = set(silence_frame_indices)
        target_frames = int(target_chunk_sec * 1000 / frame_ms)
        # Search window: ±30 s around the target boundary
        window_frames = int(30 * 1000 / frame_ms)

        chunks: list[tuple[float, float, np.ndarray]] = []
        start_frame = 0

        while start_frame < total_frames:
            target_frame = start_frame + target_frames

            if target_frame >= total_frames:
                end_frame = total_frames
            else:
                # Find the nearest silence frame within the window
                best: Optional[int] = None
                best_dist = window_frames + 1
                for f in range(
                    max(start_frame, target_frame - window_frames),
                    min(total_frames, target_frame + window_frames + 1),
                ):
                    if f in silence_set:
                        dist = abs(f - target_frame)
                        if dist < best_dist:
                            best_dist = dist
                            best = f
                end_frame = best if best is not None else target_frame

            start_sample = start_frame * frame_samples
            end_sample = min(end_frame * frame_samples, len(audio))
            chunk_audio = audio[start_sample:end_sample]

            if len(chunk_audio) > 0:
                start_sec = start_sample / sample_rate
                end_sec = end_sample / sample_rate
                chunks.append((start_sec, end_sec, chunk_audio))

            start_frame = end_frame

        return chunks

    def _prepare_batch_inputs(
        self,
        audios: "list[np.ndarray]",
        language: Optional[str],
        context: Optional[str],
    ) -> "tuple":
        """Build batched processor inputs for a list of audio arrays.

        Runs in thread pool (CPU-bound).
        Returns (inputs_dict_on_device, force_language).
        """
        qwen_model = self._model
        processor = qwen_model.processor

        effective_language = (language if language is not None else self.config.language) or None
        force_language = None
        if effective_language:
            try:
                from qwen_asr.inference.utils import normalize_language_name, validate_language
                ln = normalize_language_name(effective_language)
                validate_language(ln)
                force_language = ln
            except (ValueError, KeyError):
                logger.warning(
                    "Unrecognized language '%s' in batch prep, falling back to auto",
                    effective_language,
                )

        text_prompt = qwen_model._build_text_prompt(
            context=(context or "").strip(),
            force_language=force_language,
        )

        texts = [text_prompt] * len(audios)
        audio_arrays = [a.astype(np.float32) for a in audios]

        inputs = processor(
            text=texts,
            audio=audio_arrays,
            return_tensors="pt",
            padding=True,
        )
        inputs = inputs.to(qwen_model.model.device).to(qwen_model.model.dtype)
        return inputs, force_language

    def _run_batch_generate(self, inputs) -> "torch.Tensor":
        """Run batched model.generate() under the inference lock."""
        return self._run_batch_generate_tokens(inputs, self.config.max_new_tokens)

    def _run_batch_generate_tokens(self, inputs, max_new_tokens: int) -> "torch.Tensor":
        """Run batched model.generate() with an explicit token budget."""
        with self._inference_lock:
            with torch.no_grad():
                return self._model.model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    repetition_penalty=1.1,
                )

    def _decode_batch_output(
        self,
        output_ids,
        inputs,
        force_language: Optional[str],
    ) -> "list[str]":
        """Decode a batched generate() output into a list of text strings."""
        from qwen_asr.inference.utils import parse_asr_output

        processor = self._model.processor
        prompt_len = inputs["input_ids"].shape[1]
        generated = output_ids.sequences[:, prompt_len:]
        decoded_list = processor.batch_decode(
            generated,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        texts = []
        for raw in decoded_list:
            _, text = parse_asr_output(raw, user_language=force_language)
            texts.append(text.strip())
        return texts

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

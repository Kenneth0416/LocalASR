"""
ASR module - re-exports all types and classes for backward compatibility.

The actual implementation is split across:
- asr_types.py     : shared dataclass types
- asr_service.py    : ASRService (MLX model loading and inference)
- asr_utils.py     : ASR output parsing utilities (vendored from qwen_asr)
- vad.py           : WebRTCVADMeetingTranscriber (VAD-based realtime)
- chunker.py       : TransformerAudioChunker, SemanticMeetingTranscriber, RealtimeMeetingTranscriber
"""

# Re-export shared types
from asr_types import (
    ASRResult,
    BaseASRRouter,
    BufferedAudioFrame,
    ChunkedASREmission,
    LiteASRResult,
    RealtimeTranscriptEvent,
    RealtimeTranscriptionConfig,
    RealtimeTranscriptionRequest,
    SemanticCommit,
    UtteranceState,
    pcm16le_to_audio_tuple,
)

# Re-export routers (defined here to avoid circular imports)
from asr_types import LiteASRResult  # noqa: F401


class FinalOnlyASRRouter(BaseASRRouter):
    """ASR router that only performs final transcription."""

    def __init__(
        self,
        final_service: "ASRService",
        *,
        language_getter: "Callable[[], str | None]",
        context_getter: "Callable[[], str | None]",
    ):
        from asr_service import ASRService

        self._final_service: ASRService = final_service
        self._language_getter = language_getter
        self._context_getter = context_getter

    def _build_context(self) -> str | None:
        """Return the user-supplied static ASR prompt, if any."""
        static = self._context_getter() or ""
        return static if static else None

    async def transcribe_final(self, audio_tuple) -> LiteASRResult:
        context = self._build_context()
        result = await self._final_service.transcribe_wav(
            audio_tuple,
            language=self._language_getter(),
            context=context,
            extract_timestamps=False,
        )
        text = (result.text or "").strip()
        return LiteASRResult(
            text=text,
            duration_sec=float(getattr(result, "audio_duration", 0.0) or 0.0),
            processing_time=float(getattr(result, "processing_time", 0.0) or 0.0),
        )


# Re-export services and transcriber
from asr_service import ASRService, ASRServiceError
from config import WebRTCVADConfig
from vad import WebRTCVADMeetingTranscriber
from vad import SileroVADTranscriber
from config import SileroVADConfig, NoiseSuppressionConfig, AGCConfig
from agc import AGCProcessor
from audio_preprocessor import AudioPreprocessor

# Re-export chunkers
from chunker import (
    RealtimeMeetingTranscriber,
    SemanticMeetingTranscriber,
    TransformerAudioChunker,
    TransformerChunkingConfig,
)

__all__ = [
    # Types
    "ASRResult",
    "BaseASRRouter",
    "BufferedAudioFrame",
    "ChunkedASREmission",
    "LiteASRResult",
    "RealtimeTranscriptEvent",
    "RealtimeTranscriptionConfig",
    "RealtimeTranscriptionRequest",
    "SemanticCommit",
    "UtteranceState",
    # Services
    "ASRService",
    "ASRServiceError",
    # Routers
    "FinalOnlyASRRouter",
    # VAD
    "WebRTCVADMeetingTranscriber",
    "WebRTCVADConfig",
    # VAD (continued)
    "SileroVADTranscriber",
    "SileroVADConfig",
    # Preprocessing
    "NoiseSuppressionConfig",
    "AGCConfig",
    "AGCProcessor",
    "AudioPreprocessor",
    # Chunkers
    "RealtimeMeetingTranscriber",
    "SemanticMeetingTranscriber",
    "TransformerAudioChunker",
    "TransformerChunkingConfig",
    # Utils
    "pcm16le_to_audio_tuple",
]

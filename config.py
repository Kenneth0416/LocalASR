"""
Configuration module for Meeting Realtime Voice.
Supports both Ollama and OpenAI-compatible APIs.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class LLMConfig:
    """LLM Provider Configuration"""

    # Provider type: "ollama" or "openai"
    provider: str = "ollama"
    local_only: bool = True

    # Ollama settings
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:9b"

    # OpenAI-compatible settings
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = "gpt-4o-mini"

    def __post_init__(self):
        if self.provider == "openai" and not self.openai_base_url:
            self.openai_base_url = "https://api.openai.com/v1"

    @property
    def api_key(self) -> str:
        if self.provider == "openai":
            return self.openai_api_key
        return ""  # Ollama doesn't need API key

    @property
    def base_url(self) -> str:
        if self.provider == "openai":
            return self.openai_base_url
        return f"{self.ollama_base_url}/v1"

    @property
    def model_name(self) -> str:
        if self.provider == "openai":
            return self.openai_model
        return self.ollama_model

    def is_openai_compatible(self) -> bool:
        return self.provider == "openai" and bool(self.openai_base_url)

    def is_local_provider(self) -> bool:
        if self.provider == "ollama":
            parsed = urlparse(self.ollama_base_url or "")
            return not parsed.hostname or parsed.hostname in {"127.0.0.1", "localhost", "::1"}

        parsed = urlparse(self.openai_base_url or "")
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


@dataclass
class ASRConfig:
    """ASR Model Configuration"""

    model_path: str = os.path.expanduser("~/whisper-models/Qwen3-ASR-0.6B")
    aligner_path: str = ""
    aligner_backend: str = "qwen3alignment"
    language: str = ""
    device: str = "auto"  # auto, mps, cpu, cuda
    sample_rate: int = 16000
    init_timeout_sec: float = 180.0
    max_inference_batch_size: int = 32
    max_new_tokens: int = 256
    attn_implementation: str = "auto"
    min_chunk_sec: float = 4.0
    preferred_chunk_sec: float = 8.0
    max_chunk_sec: float = 12.0
    overlap_sec: float = 1.5
    endpoint_silence_sec: float = 0.45
    boundary_search_sec: float = 1.25
    semantic_pause_sec: float = 0.45
    semantic_soft_pause_sec: float = 0.6
    semantic_force_commit_sec: float = 18.0

    def __post_init__(self):
        self.model_path = os.path.expanduser(self.model_path)
        self.aligner_path = os.path.expanduser(self.aligner_path) if self.aligner_path else ""
        self.aligner_backend = str(self.aligner_backend or "qwen3alignment").strip().lower()
        language = str(self.language or "").strip()
        self.language = language if language and language.lower() != "auto" else ""
        self.init_timeout_sec = float(self.init_timeout_sec)
        self.max_inference_batch_size = int(self.max_inference_batch_size)
        self.max_new_tokens = int(self.max_new_tokens)
        self.attn_implementation = str(self.attn_implementation or "auto").strip()
        self.min_chunk_sec = float(self.min_chunk_sec)
        self.preferred_chunk_sec = float(self.preferred_chunk_sec)
        self.max_chunk_sec = float(self.max_chunk_sec)
        self.overlap_sec = float(self.overlap_sec)
        self.endpoint_silence_sec = float(self.endpoint_silence_sec)
        self.boundary_search_sec = float(self.boundary_search_sec)
        self.semantic_pause_sec = float(self.semantic_pause_sec)
        self.semantic_soft_pause_sec = float(self.semantic_soft_pause_sec)
        self.semantic_force_commit_sec = float(self.semantic_force_commit_sec)


@dataclass
class WebRTCVADConfig:
    frame_ms: int = 20
    vad_aggressiveness: int = 2
    enter_speech_frames: int = 2
    endpoint_silence_frames: int = 36  # 720ms silence to end utterance (was 18)
    max_utterance_sec: float = 18.0
    pre_roll_sec: float = 0.2
    min_final_audio_sec: float = 0.1
    # NOTE: Preview timing knobs were removed as part of the rollback to a
    # single realtime ASR lane. Some runtime code still emits preview events,
    # so we provide backward-compatible defaults via __getattr__.
    _preview_interval_sec_default: float = field(default=1.2, init=False, repr=False)
    _min_preview_audio_sec_default: float = field(default=0.8, init=False, repr=False)

    def __post_init__(self):
        self.frame_ms = int(self.frame_ms)
        self.vad_aggressiveness = int(self.vad_aggressiveness)
        self.enter_speech_frames = int(self.enter_speech_frames)
        self.endpoint_silence_frames = int(self.endpoint_silence_frames)
        self.max_utterance_sec = float(self.max_utterance_sec)
        self.pre_roll_sec = float(self.pre_roll_sec)
        self.min_final_audio_sec = float(self.min_final_audio_sec)

        if self.frame_ms not in {10, 20, 30}:
            raise ValueError("frame_ms must be one of 10, 20, 30")
        if not 0 <= self.vad_aggressiveness <= 3:
            raise ValueError("vad_aggressiveness must be between 0 and 3")
        if self.enter_speech_frames <= 0:
            raise ValueError("enter_speech_frames must be > 0")
        if self.endpoint_silence_frames <= 0:
            raise ValueError("endpoint_silence_frames must be > 0")
        if self.max_utterance_sec <= 0:
            raise ValueError("max_utterance_sec must be > 0")
        if self.pre_roll_sec < 0:
            raise ValueError("pre_roll_sec must be >= 0")
        if self.min_final_audio_sec < 0:
            raise ValueError("min_final_audio_sec must be >= 0")

    def __getattr__(self, name: str):
        # Backward compatibility for code that still references preview timing.
        if name == "preview_interval_sec":
            return float(self._preview_interval_sec_default)
        if name == "min_preview_audio_sec":
            return float(self._min_preview_audio_sec_default)
        raise AttributeError(name)

@dataclass
class ServerConfig:
    """Server Configuration"""

    host: str = "127.0.0.1"
    port: int = 8800
    cors_origins: list[str] = field(default_factory=lambda: [
        "http://127.0.0.1:8800",
        "http://localhost:8800",
    ])
    cors_allow_credentials: bool = False
    expose_session_api: bool = False
    audio_queue_maxsize: int = 32
    recordings_dir: str = "recordings"
    database_path: str = "data/meeting_realtime_voice.sqlite3"
    history_limit: int = 100

    def __post_init__(self):
        self.recordings_dir = str(_resolve_project_path(self.recordings_dir))
        self.database_path = str(_resolve_project_path(self.database_path))


@dataclass
class MeetingConfig:
    """Meeting Session Configuration"""

    max_context_messages: int = 50
    max_context_chars: int = 50000  # Max characters for context
    summary_interval_turns: int = 30  # Update summary every N turns


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if raw is None:
        return list(default)

    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or list(default)


def _resolve_project_path(path_value: str) -> Path:
    path = Path(os.path.expanduser(path_value))
    if path.is_absolute():
        return path.resolve(strict=False)
    project_root = Path(__file__).resolve().parent
    return (project_root / path).resolve(strict=False)


def _build_final_asr_config() -> ASRConfig:
    return ASRConfig(
        model_path=_env("ASR_MODEL_PATH", _env("FINAL_ASR_MODEL_PATH", os.path.expanduser("~/whisper-models/Qwen3-ASR-1.7B"))),
        aligner_path=_env("ASR_ALIGNER_PATH", _env("FINAL_ASR_ALIGNER_PATH", "")),
        aligner_backend=_env("ASR_ALIGNER_BACKEND", _env("FINAL_ASR_ALIGNER_BACKEND", "qwen3alignment")),
        language=_env("ASR_LANGUAGE", _env("FINAL_ASR_LANGUAGE", "")),
        device=_env("ASR_DEVICE", _env("FINAL_ASR_DEVICE", "auto")),
        init_timeout_sec=float(_env("ASR_INIT_TIMEOUT_SEC", _env("FINAL_ASR_INIT_TIMEOUT_SEC", "240"))),
        max_inference_batch_size=int(_env("ASR_MAX_INFERENCE_BATCH_SIZE", _env("FINAL_ASR_MAX_INFERENCE_BATCH_SIZE", "32"))),
        max_new_tokens=int(_env("ASR_MAX_NEW_TOKENS", _env("FINAL_ASR_MAX_NEW_TOKENS", "256"))),
        attn_implementation=_env("ASR_ATTN_IMPLEMENTATION", _env("FINAL_ASR_ATTN_IMPLEMENTATION", "auto")),
        sample_rate=int(_env("ASR_SAMPLE_RATE", _env("FINAL_ASR_SAMPLE_RATE", "16000"))),
        min_chunk_sec=float(_env("ASR_MIN_CHUNK_SEC", _env("FINAL_ASR_MIN_CHUNK_SEC", "4.0"))),
        preferred_chunk_sec=float(_env("ASR_PREFERRED_CHUNK_SEC", _env("FINAL_ASR_PREFERRED_CHUNK_SEC", "8.0"))),
        max_chunk_sec=float(_env("ASR_MAX_CHUNK_SEC", _env("FINAL_ASR_MAX_CHUNK_SEC", "12.0"))),
        overlap_sec=float(_env("ASR_OVERLAP_SEC", _env("FINAL_ASR_OVERLAP_SEC", "1.5"))),
        endpoint_silence_sec=float(_env("ASR_ENDPOINT_SILENCE_SEC", _env("FINAL_ASR_ENDPOINT_SILENCE_SEC", "0.45"))),
        boundary_search_sec=float(_env("ASR_BOUNDARY_SEARCH_SEC", _env("FINAL_ASR_BOUNDARY_SEARCH_SEC", "1.25"))),
        semantic_pause_sec=float(_env("ASR_SEMANTIC_PAUSE_SEC", _env("FINAL_ASR_SEMANTIC_PAUSE_SEC", "0.45"))),
        semantic_soft_pause_sec=float(_env("ASR_SEMANTIC_SOFT_PAUSE_SEC", _env("FINAL_ASR_SEMANTIC_SOFT_PAUSE_SEC", "0.6"))),
        semantic_force_commit_sec=float(_env("ASR_SEMANTIC_FORCE_COMMIT_SEC", _env("FINAL_ASR_SEMANTIC_FORCE_COMMIT_SEC", "18.0"))),
    )


def _build_asr_config() -> ASRConfig:
    return _build_final_asr_config()


def _build_webrtc_vad_config() -> WebRTCVADConfig:
    return WebRTCVADConfig(
        frame_ms=int(_env("WEBRTC_VAD_FRAME_MS", "20")),
        vad_aggressiveness=int(_env("WEBRTC_VAD_AGGRESSIVENESS", "2")),
        enter_speech_frames=int(_env("WEBRTC_VAD_ENTER_SPEECH_FRAMES", "2")),
        endpoint_silence_frames=int(_env("WEBRTC_VAD_ENDPOINT_SILENCE_FRAMES", "36")),
        max_utterance_sec=float(_env("WEBRTC_VAD_MAX_UTTERANCE_SEC", "18.0")),
        pre_roll_sec=float(_env("WEBRTC_VAD_PRE_ROLL_SEC", "0.2")),
        min_final_audio_sec=float(_env("WEBRTC_VAD_MIN_FINAL_AUDIO_SEC", "0.1")),
    )


def load_config() -> tuple[LLMConfig, ASRConfig, WebRTCVADConfig, ServerConfig, MeetingConfig]:
    """Load all configuration from environment variables."""

    local_only = _env_bool("LOCAL_ONLY_MODE", True)
    openai_base_url = os.getenv("OPENAI_BASE_URL", "")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    provider = "ollama"
    if openai_base_url:
        provider = "openai"
    elif not local_only and openai_api_key:
        provider = "openai"

    llm = LLMConfig(
        provider=provider,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:9b"),
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        local_only=local_only,
    )

    asr = _build_final_asr_config()
    realtime_vad = _build_webrtc_vad_config()

    server = ServerConfig(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8800")),
        cors_origins=_env_list(
            "CORS_ORIGINS",
            ["http://127.0.0.1:8800", "http://localhost:8800"],
        ),
        cors_allow_credentials=_env_bool("CORS_ALLOW_CREDENTIALS", False),
        expose_session_api=_env_bool("EXPOSE_SESSION_API", False),
        audio_queue_maxsize=max(1, int(os.getenv("AUDIO_QUEUE_MAXSIZE", "32"))),
        recordings_dir=os.getenv("MEETING_RECORDINGS_DIR", "recordings"),
        database_path=os.getenv("MEETING_DB_PATH", "data/meeting_realtime_voice.sqlite3"),
        history_limit=max(1, int(os.getenv("MEETING_HISTORY_LIMIT", "100"))),
    )

    meeting = MeetingConfig(
        max_context_messages=int(os.getenv("MAX_CONTEXT_MESSAGES", "50")),
        max_context_chars=int(os.getenv("MAX_CONTEXT_CHARS", "50000")),
        summary_interval_turns=int(os.getenv("SUMMARY_INTERVAL_TURNS", "30")),
    )

    return llm, asr, realtime_vad, server, meeting

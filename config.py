"""
Configuration module for Meeting Realtime Voice.
Supports both llama.cpp server and OpenAI-compatible APIs.
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

    # Provider type: "llamacpp" or "openai"
    provider: str = "llamacpp"
    local_only: bool = True

    # llama.cpp server settings
    llamacpp_base_url: str = "http://localhost:8190"
    llamacpp_model: str = "qwen3.5:9b"

    # OpenAI-compatible settings
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = "gpt-4o-mini"

    # Context window (tokens). 0 = auto-detect from model defaults.
    max_context_tokens: int = 0

    # Well-known model context sizes (fallback when max_context_tokens=0)
    _MODEL_CONTEXT_DEFAULTS: dict = field(default_factory=lambda: {
        "qwen3.5:9b": 32768,
    })

    def __post_init__(self):
        if self.provider == "openai" and not self.openai_base_url:
            self.openai_base_url = "https://api.openai.com/v1"

    @property
    def api_key(self) -> str:
        if self.provider == "openai":
            return self.openai_api_key
        return ""  # llama.cpp server doesn't need API key

    @property
    def base_url(self) -> str:
        if self.provider == "openai":
            return self.openai_base_url
        return self.llamacpp_base_url

    @property
    def model_name(self) -> str:
        if self.provider == "openai":
            return self.openai_model
        return self.llamacpp_model

    def is_openai_compatible(self) -> bool:
        if self.provider == "openai":
            return bool(self.openai_base_url)
        return self.provider == "llamacpp"

    def is_local_provider(self) -> bool:
        if self.provider == "llamacpp":
            parsed = urlparse(self.llamacpp_base_url or "")
            return not parsed.hostname or parsed.hostname in {"127.0.0.1", "localhost", "::1"}

        parsed = urlparse(self.openai_base_url or "")
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}

    @property
    def effective_context_tokens(self) -> int:
        """Return the effective context window in tokens.

        Priority: explicit max_context_tokens > model defaults > 32768 fallback.
        """
        if self.max_context_tokens > 0:
            return self.max_context_tokens
        model = self.model_name
        defaults = self._MODEL_CONTEXT_DEFAULTS
        if model in defaults:
            return defaults[model]
        # Try prefix match (e.g. "qwen3.5:9b-instruct" matches "qwen3.5:9b")
        for key, val in defaults.items():
            if model.startswith(key.rsplit(":", 1)[0]):
                return val
        return 32768


@dataclass
class ASRConfig:
    """ASR Model Configuration (MLX-only)"""

    model_path: str = os.path.expanduser("~/whisper-models/Qwen3-ASR-1.7B")
    language: str = ""
    backend: str = "mlx"  # always mlx
    sample_rate: int = 16000
    init_timeout_sec: float = 180.0
    max_new_tokens: int = 256
    min_chunk_sec: float = 4.0
    preferred_chunk_sec: float = 8.0
    max_chunk_sec: float = 12.0
    overlap_sec: float = 1.5
    endpoint_silence_sec: float = 0.45
    boundary_search_sec: float = 1.25
    semantic_pause_sec: float = 0.45
    semantic_soft_pause_sec: float = 0.6
    semantic_force_commit_sec: float = 18.0
    # Upload (non-realtime) batched transcription
    upload_chunk_sec: float = 120.0     # target VAD chunk length ~2 min
    upload_min_audio_sec: float = 30.0  # below this, use single-pass
    upload_max_new_tokens: int = 600    # per-chunk token budget (120s ≈ 500 chars CJK)
    upload_workers: int = 1             # parallel processes (2 = 2x speedup, needs 2x RAM)
    # MLX backend (Apple Silicon native, ~15x faster than PyTorch MPS)
    mlx_model_path: str = ""            # MLX model path (empty = mlx-community/Qwen3-ASR-1.7B-4bit)
    mlx_max_new_tokens: int = 2048      # per-chunk independent token budget
    mlx_temperature: float = 0.0        # 0=greedy argmax, >0=sampling (0.1-0.3 helps avoid loops)
    mlx_repetition_penalty: float = 1.3 # 0=disabled, 1.1-1.3=penalize repeats
    mlx_repetition_context_size: int = 50   # tokens to consider for repetition penalty

    def __post_init__(self):
        self.model_path = os.path.expanduser(self.model_path)
        language = str(self.language or "").strip()
        self.language = language if language and language.lower() != "auto" else ""
        self.init_timeout_sec = float(self.init_timeout_sec)
        self.max_new_tokens = int(self.max_new_tokens)
        self.min_chunk_sec = float(self.min_chunk_sec)
        self.preferred_chunk_sec = float(self.preferred_chunk_sec)
        self.max_chunk_sec = float(self.max_chunk_sec)
        self.overlap_sec = float(self.overlap_sec)
        self.endpoint_silence_sec = float(self.endpoint_silence_sec)
        self.boundary_search_sec = float(self.boundary_search_sec)
        self.semantic_pause_sec = float(self.semantic_pause_sec)
        self.semantic_soft_pause_sec = float(self.semantic_soft_pause_sec)
        self.semantic_force_commit_sec = float(self.semantic_force_commit_sec)
        self.upload_chunk_sec = float(self.upload_chunk_sec)
        self.upload_min_audio_sec = float(self.upload_min_audio_sec)
        self.upload_max_new_tokens = int(self.upload_max_new_tokens)
        self.upload_workers = int(self.upload_workers)
        self.mlx_model_path = str(self.mlx_model_path or "")
        self.mlx_max_new_tokens = int(self.mlx_max_new_tokens)
        self.mlx_temperature = float(self.mlx_temperature)
        self.mlx_repetition_penalty = float(self.mlx_repetition_penalty)
        self.mlx_repetition_context_size = int(self.mlx_repetition_context_size)


@dataclass
class WebRTCVADConfig:
    frame_ms: int = 20
    vad_aggressiveness: int = 2
    enter_speech_frames: int = 2
    endpoint_silence_frames: int = 36
    max_utterance_sec: float = 18.0
    pre_roll_sec: float = 0.2
    min_final_audio_sec: float = 0.1

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

@dataclass
class NoiseSuppressionConfig:
    enabled: bool = False
    library_path: str = ""  # LIBRNNOISE_PATH (auto-detect if empty)

    def __post_init__(self):
        self.enabled = bool(self.enabled)
        self.library_path = str(self.library_path or "")


@dataclass
class AGCConfig:
    enabled: bool = False
    target_rms_db: float = -20.0
    max_gain_db: float = 30.0
    attack_sec: float = 0.01
    release_sec: float = 0.1

    def __post_init__(self):
        self.enabled = bool(self.enabled)
        self.target_rms_db = float(self.target_rms_db)
        self.max_gain_db = float(self.max_gain_db)
        self.attack_sec = float(self.attack_sec)
        self.release_sec = float(self.release_sec)
        if self.target_rms_db >= 0:
            raise ValueError("target_rms_db must be < 0")
        if self.max_gain_db <= 0:
            raise ValueError("max_gain_db must be > 0")
        if self.attack_sec <= 0:
            raise ValueError("attack_sec must be > 0")
        if self.release_sec <= 0:
            raise ValueError("release_sec must be > 0")


@dataclass
class SileroVADConfig:
    speech_threshold: float = 0.5
    enter_speech_frames: int = 2
    endpoint_silence_frames: int = 22  # 22 x 32ms ~ 704ms
    max_utterance_sec: float = 18.0
    pre_roll_sec: float = 0.2
    min_final_audio_sec: float = 0.1

    def __post_init__(self):
        self.speech_threshold = float(self.speech_threshold)
        self.enter_speech_frames = int(self.enter_speech_frames)
        self.endpoint_silence_frames = int(self.endpoint_silence_frames)
        self.max_utterance_sec = float(self.max_utterance_sec)
        self.pre_roll_sec = float(self.pre_roll_sec)
        self.min_final_audio_sec = float(self.min_final_audio_sec)
        if not 0.0 < self.speech_threshold < 1.0:
            raise ValueError("speech_threshold must be between 0 and 1")
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
    recent_window_minutes: int = 5  # Keep last N minutes of transcript as raw text
    use_progressive_context: bool = True  # Use summary + recent window instead of full transcript


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
        language=_env("ASR_LANGUAGE", _env("FINAL_ASR_LANGUAGE", "")),
        backend="mlx",
        init_timeout_sec=float(_env("ASR_INIT_TIMEOUT_SEC", _env("FINAL_ASR_INIT_TIMEOUT_SEC", "240"))),
        max_new_tokens=int(_env("ASR_MAX_NEW_TOKENS", _env("FINAL_ASR_MAX_NEW_TOKENS", "256"))),
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
        upload_chunk_sec=float(_env("ASR_UPLOAD_CHUNK_SEC", "120.0")),
        upload_min_audio_sec=float(_env("ASR_UPLOAD_MIN_AUDIO_SEC", "30.0")),
        upload_max_new_tokens=int(_env("ASR_UPLOAD_MAX_NEW_TOKENS", "600")),
        upload_workers=int(_env("ASR_UPLOAD_WORKERS", "1")),
        mlx_model_path=_env("ASR_MLX_MODEL_PATH", ""),
        mlx_max_new_tokens=int(_env("ASR_MLX_MAX_NEW_TOKENS", "2048")),
        mlx_temperature=float(_env("ASR_MLX_TEMPERATURE", "0.0")),
        mlx_repetition_penalty=float(_env("ASR_MLX_REPETITION_PENALTY", "0.0")),
        mlx_repetition_context_size=int(_env("ASR_MLX_REPETITION_CONTEXT_SIZE", "100")),
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


def _build_noise_suppression_config() -> NoiseSuppressionConfig:
    return NoiseSuppressionConfig(
        enabled=_env_bool("NOISE_SUPPRESSION", False),
        library_path=_env("LIBRNNOISE_PATH", ""),
    )


def _build_agc_config() -> AGCConfig:
    return AGCConfig(
        enabled=_env_bool("AGC", False),
        target_rms_db=float(_env("AGC_TARGET_RMS_DB", "-20.0")),
        max_gain_db=float(_env("AGC_MAX_GAIN_DB", "30.0")),
        attack_sec=float(_env("AGC_ATTACK_SEC", "0.01")),
        release_sec=float(_env("AGC_RELEASE_SEC", "0.1")),
    )


def _build_silero_vad_config() -> SileroVADConfig:
    return SileroVADConfig(
        speech_threshold=float(_env("SILERO_VAD_THRESHOLD", "0.5")),
        enter_speech_frames=int(_env("SILERO_VAD_ENTER_SPEECH_FRAMES", "2")),
        endpoint_silence_frames=int(_env("SILERO_VAD_ENDPOINT_SILENCE_FRAMES", "22")),
        max_utterance_sec=float(_env("SILERO_VAD_MAX_UTTERANCE_SEC", "18.0")),
        pre_roll_sec=float(_env("SILERO_VAD_PRE_ROLL_SEC", "0.2")),
        min_final_audio_sec=float(_env("SILERO_VAD_MIN_FINAL_AUDIO_SEC", "0.1")),
    )


def load_config() -> tuple[LLMConfig, ASRConfig, WebRTCVADConfig, ServerConfig, MeetingConfig, NoiseSuppressionConfig, AGCConfig, SileroVADConfig, str]:
    """Load all configuration from environment variables."""

    local_only = _env_bool("LOCAL_ONLY_MODE", True)
    openai_base_url = os.getenv("OPENAI_BASE_URL", "")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    provider = "llamacpp"
    if openai_base_url:
        provider = "openai"
    elif not local_only and openai_api_key:
        provider = "openai"

    llm = LLMConfig(
        provider=provider,
        llamacpp_base_url=os.getenv("LLAMACPP_BASE_URL", "http://localhost:8190"),
        llamacpp_model=os.getenv("LLAMACPP_MODEL", "qwen3.5:9b"),
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        local_only=local_only,
        max_context_tokens=int(os.getenv("LLM_MAX_CONTEXT_TOKENS", "0")),
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
        recent_window_minutes=int(os.getenv("RECENT_WINDOW_MINUTES", "5")),
        use_progressive_context=_env_bool("USE_PROGRESSIVE_CONTEXT", True),
    )

    noise_suppression = _build_noise_suppression_config()
    agc = _build_agc_config()
    silero_vad = _build_silero_vad_config()
    vad_backend = _env("VAD_BACKEND", "webrtc").strip().lower()

    return llm, asr, realtime_vad, server, meeting, noise_suppression, agc, silero_vad, vad_backend

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from config import ASRConfig, LLMConfig, ServerConfig


@dataclass(frozen=True)
class DependencyState:
    status: str
    detail: str = ""


@dataclass(frozen=True)
class CapabilityState:
    state: str
    detail: str = ""


@dataclass(frozen=True)
class RuntimeSnapshot:
    mode: str
    dependencies: dict[str, DependencyState]
    capabilities: dict[str, CapabilityState]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "dependencies": {
                key: {"status": value.status, "detail": value.detail}
                for key, value in self.dependencies.items()
            },
            "capabilities": {
                key: {"state": value.state, "detail": value.detail}
                for key, value in self.capabilities.items()
            },
            "warnings": list(self.warnings),
        }


def _is_parent_writable(path_value: str) -> bool:
    try:
        path = Path(os.path.expanduser(path_value))
        candidate = path if path.exists() and path.is_dir() else path.parent
        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                return False
            candidate = parent
        if not candidate.is_dir():
            return False
        return os.access(candidate, os.W_OK)
    except OSError:
        return False


def _is_loopback_host(hostname: str | None) -> bool:
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _local_provider_allowed(llm_config: LLMConfig) -> bool:
    if not llm_config.local_only:
        return True

    if llm_config.provider == "ollama":
        parsed = urlparse(llm_config.ollama_base_url or "")
        return not parsed.hostname or _is_loopback_host(parsed.hostname)

    parsed = urlparse(llm_config.openai_base_url or "")
    return _is_loopback_host(parsed.hostname)


def build_runtime_snapshot(
    llm_config: LLMConfig,
    asr_config: ASRConfig,
    server_config: ServerConfig,
    *,
    asr_state: str,
    asr_error: str | None,
    llm_status: str,
) -> RuntimeSnapshot:
    warnings: list[str] = []

    asr_model_exists = os.path.exists(asr_config.model_path)
    ffmpeg_path = shutil.which("ffmpeg")
    recordings_writable = _is_parent_writable(server_config.recordings_dir)
    database_writable = _is_parent_writable(server_config.database_path)
    local_provider_ok = _local_provider_allowed(llm_config)

    if server_config.host not in {"127.0.0.1", "localhost", "::1"}:
        warnings.append(f"HOST={server_config.host} is not loopback-only.")

    if llm_config.local_only and not local_provider_ok:
        warnings.append("Remote LLM configuration is blocked in local-only mode.")

    dependencies = {
        "asr_model": DependencyState(
            status="ready" if asr_model_exists else "missing",
            detail=asr_config.model_path,
        ),
        "ffmpeg": DependencyState(
            status="ready" if ffmpeg_path else "missing",
            detail=ffmpeg_path or "ffmpeg not found in PATH",
        ),
        "recordings_dir": DependencyState(
            status="ready" if recordings_writable else "missing",
            detail=server_config.recordings_dir,
        ),
        "database_dir": DependencyState(
            status="ready" if database_writable else "missing",
            detail=server_config.database_path,
        ),
        "local_llm": DependencyState(
            status="ready" if llm_status.startswith("ok") and local_provider_ok else "missing",
            detail=llm_status,
        ),
    }

    transcription_state = "ready" if asr_state == "ready" and asr_model_exists else "unavailable"
    transcription_detail = asr_error or asr_state

    llm_capability_state = "ready" if llm_status.startswith("ok") and local_provider_ok else "unavailable"
    llm_capability_detail = llm_status if llm_status else "local llm unavailable"

    capabilities = {
        "transcription_realtime": CapabilityState(transcription_state, transcription_detail),
        "transcription_upload_wav": CapabilityState(transcription_state, transcription_detail),
        "transcription_upload_transcoded": CapabilityState(
            transcription_state
            if transcription_state != "ready"
            else ("ready" if ffmpeg_path else "degraded"),
            transcription_detail
            if transcription_state != "ready"
            else ("ffmpeg available" if ffmpeg_path else "ffmpeg missing; WAV upload still supported"),
        ),
        "summary_local": CapabilityState(llm_capability_state, llm_capability_detail),
        "chat_local": CapabilityState(llm_capability_state, llm_capability_detail),
        "history_read_write": CapabilityState(
            "ready" if recordings_writable and database_writable else "degraded",
            "local storage writable" if recordings_writable and database_writable else "check recordings/database paths",
        ),
    }

    return RuntimeSnapshot(
        mode="local-only" if llm_config.local_only else "mixed",
        dependencies=dependencies,
        capabilities=capabilities,
        warnings=warnings,
    )

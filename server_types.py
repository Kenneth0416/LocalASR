"""
Shared dataclass types and protocol constants used by server modules.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioFrame:
    seq: int
    pcm_chunk: bytes
    sample_count: int


@dataclass(frozen=True)
class AudioQueueItem:
    event_type: str
    frames: tuple[AudioFrame, ...] = ()
    last_seq: int = -1
    reason: str = "stop"

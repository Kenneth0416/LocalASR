"""
Automatic Gain Control (AGC) — normalize audio levels for ASR input.

Uses RMS-based envelope tracking with attack/release smoothing.
Only boosts quiet signals; never attenuates loud ones.
"""

import logging
import math

import numpy as np

from config import AGCConfig

logger = logging.getLogger("meeting.agc")


class AGCProcessor:
    """RMS-based Automatic Gain Control operating on PCM16 byte chunks."""

    def __init__(self, config: AGCConfig, sample_rate: int = 16000):
        self._target_rms = 10.0 ** (config.target_rms_db / 20.0)
        self._max_gain = 10.0 ** (config.max_gain_db / 20.0)
        # 320 = samples per 20ms frame at 16kHz. Time constants are calibrated
        # for 20ms chunks — the expected calling convention from audio_worker.
        self._attack = 1.0 - math.exp(-1.0 / (config.attack_sec * sample_rate / 320))
        self._release = 1.0 - math.exp(-1.0 / (config.release_sec * sample_rate / 320))
        self._envelope: float = 0.0

    def process(self, pcm_chunk: bytes) -> bytes:
        if not pcm_chunk:
            return pcm_chunk

        samples = np.frombuffer(pcm_chunk, dtype="<i2").astype(np.float32) / 32768.0
        chunk_rms = float(np.sqrt(np.mean(samples ** 2)))

        if chunk_rms > self._envelope:
            self._envelope = self._attack * chunk_rms + (1.0 - self._attack) * self._envelope
        else:
            self._envelope = self._release * chunk_rms + (1.0 - self._release) * self._envelope

        safe_envelope = max(self._envelope, 1e-10)
        gain = self._target_rms / safe_envelope
        gain = max(1.0, min(gain, self._max_gain))

        boosted = samples * gain
        # Soft clipping via tanh — only when the boosted signal is large
        # enough to actually clip. For small signals tanh(x) ≈ x so pass
        # through linearly, which avoids the asymptotic-issue with tanh(gain)
        # saturating to 1.0 and destroying the signal in the rescaling step.
        clip_start = 0.8          # begin transition into tanh soft-clip
        clip_full  = 1.2          # fully in tanh region above this
        abs_b      = np.abs(boosted)
        linear_out = boosted      # tanh(x) ≈ x for small x

        # Phase 2: blend linear -> tanh
        blend2 = ((abs_b - clip_start) / (clip_full - clip_start)).clip(0.0, 1.0)
        tanh_raw = np.tanh(boosted)
        # Rescale tanh so its value at clip_full matches clip_full (≈ linear value)
        tanh_scale = np.tanh(clip_full)
        tanh_out = tanh_raw / tanh_scale * clip_full
        mixed = linear_out * (1.0 - blend2) + tanh_out * blend2

        # Phase 3: full tanh hard cap above 1.0
        output = np.where(abs_b > 1.0, tanh_raw, mixed)
        output = np.clip(output, -1.0, 1.0)

        pcm_out = (output * 32767).astype(np.int16).tobytes()
        return pcm_out

    def reset(self) -> None:
        self._envelope = 0.0

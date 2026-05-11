"""
RNNoise noise suppression via ctypes.

Wraps the librnnoise C library for real-time denoising.
RNNoise processes 480-sample frames at 48kHz; this module handles
resampling from/to the caller's sample rate (typically 16kHz).

Requires librnnoise to be installed on the system:
  macOS: brew install rnnoise
  Linux: apt install librnnoise-dev
"""

import ctypes
import ctypes.util
import logging
import math
import os

import numpy as np

logger = logging.getLogger("meeting.noise_suppression")

# RNNoise frame size: 480 samples @ 48kHz = 10ms
RNNOISE_FRAME_SIZE = 480
RNNOISE_SAMPLE_RATE = 48000


class RNNoiseProcessor:
    """RNNoise-based noise suppression operating on PCM16 byte chunks."""

    def __init__(self, sample_rate: int = 16000, library_path: str = ""):
        self._sample_rate = sample_rate
        self._lib = self._load_library(library_path)
        self._state = self._lib.rnnoise_create(None)
        if not self._state:
            raise RuntimeError("rnnoise_create returned NULL")
        # Pre-allocate ctypes buffers
        self._in_buf = (ctypes.c_float * RNNOISE_FRAME_SIZE)()
        self._out_buf = (ctypes.c_float * RNNOISE_FRAME_SIZE)()
        # Resampling ratios
        self._up = RNNOISE_SAMPLE_RATE // math.gcd(RNNOISE_SAMPLE_RATE, sample_rate)
        self._down = sample_rate // math.gcd(RNNOISE_SAMPLE_RATE, sample_rate)

    @staticmethod
    def _load_library(library_path: str):
        path = library_path or os.environ.get("LIBRNNOISE_PATH", "")
        if not path:
            path = ctypes.util.find_library("rnnoise")
        if not path:
            raise RuntimeError(
                "librnnoise not found. Install with: brew install rnnoise (macOS) "
                "or apt install librnnoise-dev (Linux). "
                "Set LIBRNNOISE_PATH env var to specify a custom path."
            )
        try:
            return ctypes.CDLL(path)
        except OSError as e:
            raise RuntimeError(f"Failed to load librnnoise from {path}: {e}") from e

    def process(self, pcm_chunk: bytes) -> bytes:
        if not pcm_chunk:
            return pcm_chunk

        samples_16k = np.frombuffer(pcm_chunk, dtype="<i2").astype(np.float32) / 32768.0

        # Resample 16kHz -> 48kHz
        if self._sample_rate != RNNOISE_SAMPLE_RATE:
            samples_48k = self._resample(samples_16k, self._up, self._down)
        else:
            samples_48k = samples_16k

        # Process in 480-sample frames
        output_frames = []
        for start in range(0, len(samples_48k), RNNOISE_FRAME_SIZE):
            frame = samples_48k[start:start + RNNOISE_FRAME_SIZE]
            if len(frame) < RNNOISE_FRAME_SIZE:
                frame = np.pad(frame, (0, RNNOISE_FRAME_SIZE - len(frame)))

            for i in range(RNNOISE_FRAME_SIZE):
                self._in_buf[i] = float(frame[i])

            self._lib.rnnoise_process_frame(self._state, self._out_buf, self._in_buf)

            output_frames.append(np.array([self._out_buf[i] for i in range(RNNOISE_FRAME_SIZE)], dtype=np.float32))

        denoised_48k = np.concatenate(output_frames)[:len(samples_48k)]

        # Resample 48kHz -> 16kHz
        if self._sample_rate != RNNOISE_SAMPLE_RATE:
            denoised_16k = self._resample(denoised_48k, self._down, self._up)
        else:
            denoised_16k = denoised_48k

        # Match input length
        denoised_16k = denoised_16k[:len(samples_16k)]
        pcm_out = (np.clip(denoised_16k, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        return pcm_out

    def reset(self) -> None:
        if self._state:
            self._lib.rnnoise_destroy(self._state)
        self._state = self._lib.rnnoise_create(None)
        if not self._state:
            raise RuntimeError("rnnoise_create returned NULL on reset")

    @staticmethod
    def _resample(signal: np.ndarray, up: int, down: int) -> np.ndarray:
        """Simple polyphase resampling using scipy."""
        from scipy.signal import resample_poly
        gcd = math.gcd(up, down)
        return resample_poly(signal, up // gcd, down // gcd).astype(np.float32)

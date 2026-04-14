"""
Audio transcoding utilities using FFmpeg subprocess.
Transcodes non-WAV audio formats to 16kHz mono PCM WAV bytes.
"""

import subprocess
import tempfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TranscodingError(Exception):
    """Raised when FFmpeg fails to transcode an audio file."""
    pass


def transcode_to_wav_pcm(
    audio_bytes: bytes,
    filename: str = "",
    timeout_sec: float = 30.0,
) -> bytes:
    """
    Transcode audio bytes to 16kHz mono PCM WAV via FFmpeg subprocess.

    Args:
        audio_bytes: Raw audio file content (any format FFmpeg supports, e.g., M4A, MP3).
        filename: Optional filename for logging purposes only.
        timeout_sec: Max seconds to wait for FFmpeg to complete.

    Returns:
        Raw PCM WAV bytes (16kHz mono).

    Raises:
        TranscodingError: If FFmpeg fails, times out, or returns empty output.
    """
    filename_for_log = filename or "uploaded_audio"
    logger.debug(f"Transcoding {filename_for_log} to 16kHz mono PCM WAV via FFmpeg")

    in_path = None
    out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as tmp_in:
            tmp_in.write(audio_bytes)
            in_path = tmp_in.name

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_out:
            out_path = tmp_out.name

        cmd = [
            "ffmpeg",
            "-i", in_path,
            "-ar", "16000",
            "-ac", "1",
            "-acodec", "pcm_s16le",
            "-y",
            "-loglevel", "error",
            out_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            logger.error(f"FFmpeg failed for {filename_for_log}: {stderr}")
            raise TranscodingError(
                f"Transcription failed: Unsupported audio format or file corrupted"
            )

        with open(out_path, "rb") as f:
            wav_bytes = f.read()

        if not wav_bytes:
            logger.error(f"FFmpeg produced empty output for {filename_for_log}")
            raise TranscodingError(
                f"Transcription failed: Unsupported audio format or file corrupted"
            )

        logger.debug(f"Transcoding {filename_for_log} complete: {len(wav_bytes)} bytes")
        return wav_bytes

    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg transcoding timed out for {filename_for_log} ({timeout_sec}s)")
        raise TranscodingError(
            f"Audio transcoding timed out (file may be too large or corrupted)"
        )
    except FileNotFoundError:
        logger.error("FFmpeg not found in PATH")
        raise TranscodingError(
            f"Transcription failed: ffmpeg not found. Please install ffmpeg."
        )
    finally:
        for path in (in_path, out_path):
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass

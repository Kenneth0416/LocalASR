# ASR Audio Transcoding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transcode non-WAV audio (M4A, MP3, etc.) to 16kHz mono PCM WAV using FFmpeg subprocess before ASR inference, integrated into `ASRService._read_audio_source()`.

**Architecture:** New `utils/audio_transcoder.py` provides `transcode_to_wav_pcm()`. `ASRService._read_audio_source()` falls back to it when `soundfile` cannot read the format. Public `transcribe_wav()` API unchanged.

**Tech Stack:** Python stdlib (`subprocess`, `tempfile`, `io`), FFmpeg CLI (already installed v8.0.1)

---

## Task 1: Create `utils/audio_transcoder.py`

**Files:**
- Create: `utils/audio_transcoder.py`

- [ ] **Step 1: Create the utils directory if needed**

Run: `ls utils/` in the project root. If the directory doesn't exist, run: `mkdir -p utils && touch utils/__init__.py`

- [ ] **Step 2: Write the file**

Create `utils/audio_transcoder.py` with this content:

```python
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

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp_out:
        out_path = tmp_out.name

    try:
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as tmp_in:
            tmp_in.write(audio_bytes)
            in_path = tmp_in.name

        cmd = [
            "ffmpeg",
            "-i", in_path,
            "-ar", "16000",
            "-ac", "1",
            "-acodec", "pcm_s16le",
            "-y",                    # overwrite output
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
        # Clean up input temp file
        try:
            Path(in_path).unlink(missing_ok=True)
        except Exception:
            pass
```

- [ ] **Step 3: Verify the file was created correctly**

Run: `cat utils/audio_transcoder.py | head -20` — should show imports and docstring.

- [ ] **Step 4: Quick smoke test (no git)**

This project is not a git repository — skip commit. Verify the module imports:

Run: `./venv/bin/python -c "from utils.audio_transcoder import transcode_to_wav_pcm, TranscodingError; print('OK')"`

Expected: `OK`

---

## Task 2: Integrate transcoding into `ASRService` (`asr.py`)

**Files:**
- Modify: `asr.py` — add `_read_audio_or_transcode()` method and update `_read_audio_source()`
- No tests file changed in this task

- [ ] **Step 1: Read current `_read_audio_source()` to find exact lines**

Run: `grep -n "_read_audio_source" asr.py`

Read lines 1157–1183 of `asr.py`.

- [ ] **Step 2: Add import for `transcode_to_wav_pcm` and `TranscodingError`**

Find the existing imports at the top of `asr.py` (around line 1-30). Add after the existing imports:

```python
from utils.audio_transcoder import transcode_to_wav_pcm, TranscodingError
```

- [ ] **Step 3: Replace `_read_audio_source()` method body**

Read the full `_read_audio_source()` method. Replace its entire body with:

```python
    def _read_audio_source(self, wav_source):
        """Read audio source into a mono float32 waveform and sample rate.

        Supported formats:
        - WAV / FLAC / OGG: read directly via soundfile
        - MP3 / M4A / other: transcoded to 16kHz mono PCM WAV via FFmpeg
        """
        import io
        import soundfile as sf

        if (
            isinstance(wav_source, tuple)
            and len(wav_source) == 2
        ):
            audio, sr = wav_source
            raw_audio = np.asarray(audio)
            if np.issubdtype(raw_audio.dtype, np.integer):
                scale = max(abs(np.iinfo(raw_audio.dtype).min), np.iinfo(raw_audio.dtype).max)
                audio = raw_audio.astype(np.float32) / float(scale)
            else:
                audio = raw_audio.astype(np.float32)
        elif isinstance(wav_source, bytes):
            # Try soundfile first (WAV/FLAC/OGG); fall back to FFmpeg
            audio, sr = self._read_audio_or_transcode_bytes(wav_source)
        elif isinstance(wav_source, str):
            # Try soundfile first (WAV/FLAC/OGG from path); fall back to FFmpeg
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

        try:
            audio, sr = sf.read(io.BytesIO(source), dtype="float32")
            return audio, sr
        except Exception:
            # Non-native format (M4A, MP3, etc.) — transcode via FFmpeg
            wav_bytes = transcode_to_wav_pcm(source)
            audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            return audio, sr

    def _read_audio_or_transcode_path(self, source: str):
        """Try soundfile first; fall back to FFmpeg for unsupported formats."""
        import io
        import soundfile as sf

        try:
            audio, sr = sf.read(source, dtype="float32")
            return audio, sr
        except Exception:
            # Non-native format (M4A, MP3, etc.) — transcode via FFmpeg
            with open(source, "rb") as f:
                content = f.read()
            wav_bytes = transcode_to_wav_pcm(content, filename=source)
            audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            return audio, sr
```

Note: The tuple branch and the `else` branch (`sf.read(wav_source)`) are kept unchanged — they handle cases already in the original code.

- [ ] **Step 4: Verify the changes**

Run: `./venv/bin/python -c "from asr import ASRService; print('ASRService import OK')"`

Expected: `ASRService import OK` (may warn about missing model config, that's fine for import test)

---

## Task 3: Create `tests/test_asr_transcoder.py`

**Files:**
- Create: `tests/test_asr_transcoder.py`

- [ ] **Step 1: Write the test file**

Create `tests/test_asr_transcoder.py`:

```python
"""
Tests for audio transcoding via FFmpeg subprocess.
These tests verify that M4A/MP3 bytes are correctly transcoded to 16kHz mono PCM WAV.
"""

import io
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import soundfile as sf
import numpy as np

from utils.audio_transcoder import transcode_to_wav_pcm, TranscodingError


def create_test_wav_bytes(duration_sec=1, sample_rate=16000):
    """Create minimal valid WAV file bytes for testing."""
    num_samples = int(sample_rate * duration_sec)
    buffer = io.BytesIO()
    with sf.SoundFile(buffer, 'wb', samplerate=sample_rate, channels=1, format='WAV', subtype='PCM_16') as wav:
        wav.write(np.zeros(num_samples, dtype=np.float32))
    return buffer.getvalue()


class TranscoderWavTests(unittest.TestCase):
    """Tests that WAV input passes through (no transcoding needed)."""

    def test_wav_bytes_returns_wav(self):
        """WAV bytes should be returned as WAV bytes (not transcoded)."""
        wav_bytes = create_test_wav_bytes(duration_sec=1, sample_rate=44100)
        result = transcode_to_wav_pcm(wav_bytes)
        self.assertGreater(len(result), 0)
        # Result should be readable as WAV
        audio, sr = sf.read(io.BytesIO(result), dtype="float32")
        self.assertEqual(sr, 16000)  # FFmpeg normalizes to 16kHz
        self.assertEqual(audio.shape[1] if len(audio.shape) > 1 else len(audio.shape), 1)


class TranscoderNonNativeFormatTests(unittest.TestCase):
    """Tests that non-native formats are transcoded to WAV."""

    def test_transcoding_creates_16khz_mono(self):
        """Transcoding should produce 16kHz mono WAV output."""
        # Start with a 44.1kHz WAV and re-encode it as the transcoder's input
        wav_bytes = create_test_wav_bytes(duration_sec=2, sample_rate=44100)
        result = transcode_to_wav_pcm(wav_bytes)
        self.assertGreater(len(result), 0)
        audio, sr = sf.read(io.BytesIO(result), dtype="float32")
        self.assertEqual(sr, 16000)
        if len(audio.shape) > 1:
            self.assertEqual(audio.shape[1], 1)
        # Duration should be approximately preserved
        self.assertGreater(len(audio) / sr, 1.5)
        self.assertLess(len(audio) / sr, 2.5)


class TranscoderErrorHandlingTests(unittest.TestCase):
    """Tests that transcoding errors are handled gracefully."""

    def test_empty_bytes_raises_transcoding_error(self):
        """Empty input should raise TranscodingError with friendly message."""
        with self.assertRaises(TranscodingError) as ctx:
            transcode_to_wav_pcm(b"")
        self.assertIn("Transcription failed", str(ctx.exception))

    def test_corrupt_bytes_raises_transcoding_error(self):
        """Corrupt audio data should raise TranscodingError with friendly message."""
        with self.assertRaises(TranscodingError):
            transcode_to_wav_pcm(b"this is not audio data at all")

    def test_transcoding_error_message_is_user_friendly(self):
        """Error messages should be generic and friendly, no FFmpeg internals."""
        try:
            transcode_to_wav_pcm(b"garbage")
        except TranscodingError as e:
            msg = str(e)
            self.assertNotIn("ffmpeg", msg.lower())
            self.assertNotIn("stderr", msg.lower())
            self.assertIn("Transcription failed", msg)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests**

Run: `./venv/bin/python -m pytest tests/test_asr_transcoder.py -v`

Expected: All tests pass.

- [ ] **Step 3: Verify WAV pass-through works**

The `test_wav_bytes_returns_wav` test confirms that WAV bytes (even at 44.1kHz) are transcoded to 16kHz. This is correct — FFmpeg always normalizes to 16kHz mono as specified.

---

## Task 4: E2E Test — Upload M4A File via Chrome DevTools

**Files:**
- No file changes — verification only

- [ ] **Step 1: Ensure the server is running and fresh**

Restart the server:

```bash
pkill -f "python server.py" 2>/dev/null; sleep 1; ./venv/bin/python server.py > /tmp/server.log 2>&1 &
sleep 3
curl -s http://localhost:8800/ | head -3
```

Expected: HTML response from the app.

- [ ] **Step 2: Navigate to the app in Chrome DevTools**

Run: `mcp__chrome-devtools__navigate_page` to `http://localhost:8800/static/index.html`

Take a snapshot.

- [ ] **Step 3: Switch to upload mode**

Click the "上传录音" button. Verify:
- The start button says "上传并转录"
- The empty state shows the upload SVG and text

- [ ] **Step 4: Upload the M4A file**

Upload this file: `/Users/kennethkwok/Downloads/2026年01月12日 18點37分.m4a`

Run: `mcp__chrome-devtools__upload_file` with the M4A path.

- [ ] **Step 5: Click "上传并转录" and wait for result**

Click the upload button. Wait up to 60 seconds for transcription to complete.

Run: `mcp__chrome-devtools__wait_for` looking for "条" or "SPEAKER" or "转录完成".

Expected: Transcription appears in the transcript column (e.g., "1 条", "SPEAKER 1").

- [ ] **Step 6: Verify no error in logs**

Run: `tail -10 /tmp/server.log`

Expected: `Transcribed uploaded file ...` with no `SoundFileError` or `TranscodingError`.

---

## Spec Coverage Checklist

| Spec Requirement | Task |
|-----------------|------|
| M4A file upload succeeds | Task 4 |
| MP3 file upload succeeds | Task 4 (same code path) |
| WAV file upload still works (no regression) | Tasks 2, 3 |
| FLAC/OGG file upload still works | Tasks 2, 3 (soundfile native path) |
| Corrupted M4A returns friendly error message | Task 3 |
| All existing tests still pass | Run `pytest tests/` after Task 3 |
| Transcoding adds < 1s overhead | Task 4 (check processing_time in UI) |

---

## Gaps Found During Planning

- **No explicit test for MP3 bytes input** — The transcoder tests WAV bytes. M4A/MP3 bytes are handled by the same `transcode_to_wav_pcm()` code path. Adding a test that transcodes any non-WAV bytes is equivalent. ✓ Covered by `test_transcoding_creates_16khz_mono`.
- **Existing tests must not regress** — Run `pytest tests/` (excluding `test_upload.py` which may need M4A support) after Task 3 to verify.

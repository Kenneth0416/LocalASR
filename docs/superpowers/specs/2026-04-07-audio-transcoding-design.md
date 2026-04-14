# ASR Service Audio Transcoding — Design Spec

**Date:** 2026-04-07
**Status:** Approved

## 1. Problem

`ASRService.transcribe_wav()` declares support for WAV, MP3, M4A, OGG, and FLAC formats, but `_read_audio_source()` only uses `soundfile`, which natively supports WAV/FLAC/OGG and cannot decode MP3 or M4A. Uploading an M4A file causes a `SoundFileError`.

## 2. Solution

Intercept non-WAV audio bytes in `_read_audio_source()`. If `soundfile` cannot read them natively, use `ffmpeg` (already installed: v8.0.1) to transcode to PCM WAV bytes, then feed that to `soundfile`. The public API (`transcribe_wav`) stays unchanged.

## 3. Architecture

```
upload_audio_transcribe (server.py)
  → ASRService.transcribe_wav(bytes)    [unchanged]
    → _read_audio_source(bytes)
        ├── soundfile can read?  → return audio, sr  ✓
        └── soundfile fails
            → transcode_to_wav_pcm(bytes)          [NEW utility]
                → subprocess ffmpeg
                → bytes (PCM WAV)                   [16kHz mono]
            → soundfile read the transcoded bytes
            → return audio, sr                       ✓
    → _transcribe_audio_array(audio, sr)
```

## 4. Audio Parameters

FFmpeg transcoding target: **16kHz, mono, PCM 16-bit signed little-endian**.

```
ffmpeg -i {input} -ar 16000 -ac 1 -acodec pcm_s16le -y -loglevel error pipe:1
```

Rationale: ASR model is trained on 16kHz audio. Standardizing to this sample rate is fast, reduces memory, and maximizes recognition accuracy.

## 5. New File: `utils/audio_transcoder.py`

```python
"""
Audio transcoding utilities using FFmpeg subprocess.
Transcodes non-WAV audio formats to PCM WAV bytes.
"""

import subprocess
import tempfile
import os
import logging

logger = logging.getLogger(__name__)

def transcode_to_wav_pcm(
    audio_bytes: bytes,
    filename: str = "",
    timeout_sec: float = 30.0,
) -> bytes:
    """
    Transcode audio bytes to 16kHz mono PCM WAV via ffmpeg.

    Args:
        audio_bytes: Raw audio file content (any format ffmpeg supports).
        filename: Optional filename for logging.
        timeout_sec: Max seconds to wait for ffmpeg.

    Returns:
        Raw PCM WAV bytes (16kHz mono).

    Raises:
        TranscodingError: If ffmpeg fails, times out, or returns empty output.
    """
    ...
```

## 6. Changes to `asr.py`

In `_read_audio_source()`:

```python
def _read_audio_source(self, wav_source):
    import soundfile as sf

    # Already in memory as (audio, sr) tuple — return directly
    if isinstance(wav_source, tuple) and len(wav_source) == 2:
        audio, sr = wav_source
        audio = np.asarray(audio)
        if np.issubdtype(audio.dtype, np.integer):
            scale = max(abs(np.iinfo(audio.dtype).min), np.iinfo(audio.dtype).max)
            audio = audio.astype(np.float32) / float(scale)
        else:
            audio = audio.astype(np.float32)
    # WAV / FLAC / OGG — soundfile handles natively
    elif isinstance(wav_source, (bytes, str)):
        audio, sr = self._read_audio_or_transcode(wav_source)   # NEW helper
    else:
        audio, sr = sf.read(wav_source, dtype="float32")

    if len(audio.shape) > 1:
        audio = audio.mean(axis=1)

    return audio, sr
```

New method `_read_audio_or_transcode()`:

```python
def _read_audio_or_transcode(self, source):
    """Try soundfile first; fall back to ffmpeg transcoding for unsupported formats."""
    import soundfile as sf
    import io

    if isinstance(source, str):
        # File path — try soundfile directly
        try:
            return sf.read(source, dtype="float32")
        except Exception:
            # Non-native format (e.g., M4A from path) — transcode
            with open(source, "rb") as f:
                content = f.read()
            wav_bytes = transcode_to_wav_pcm(content, filename=source)
            audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            return audio, sr
    else:
        # bytes — try soundfile first
        try:
            return sf.read(io.BytesIO(source), dtype="float32")
        except Exception:
            # Non-native format — transcode
            wav_bytes = transcode_to_wav_pcm(source, filename="uploaded_audio")
            audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            return audio, sr
```

Note: We try `soundfile` first so native formats (WAV/FLAC/OGG) don't pay the subprocess overhead.

## 7. Error Handling

- **FFmpeg returns non-zero exit code** → log error → raise `ASRServiceError("Transcription failed: Unsupported audio format or file corrupted")`
- **FFmpeg timeout (30s)** → kill process → raise `ASRServiceError("Audio transcoding timed out")`
- **FFmpeg outputs empty bytes** → raise `ASRServiceError("Transcription failed: Unsupported audio format or file corrupted")`
- **FFmpeg not installed** → `FileNotFoundError` caught → raise `ASRServiceError("Transcription failed: ffmpeg not found. Please install ffmpeg.")`

User-facing error message is generic and friendly. Internal details go to server logs only.

## 8. Files to Change

| File | Changes |
|------|---------|
| `utils/audio_transcoder.py` | NEW — `transcode_to_wav_pcm()` function |
| `asr.py` | Add `_read_audio_or_transcode()` method; update `_read_audio_source()` |
| `tests/test_asr_transcoder.py` | NEW — test transcoding with WAV/M4A/MP3 inputs |

## 9. Acceptance Criteria

- [ ] M4A file upload succeeds with transcription result displayed
- [ ] MP3 file upload succeeds
- [ ] WAV file upload still works (no regression)
- [ ] FLAC/OGG file upload still works
- [ ] Corrupted M4A returns friendly error message
- [ ] All existing tests still pass
- [ ] Transcoding adds < 1s overhead for a typical 5-minute recording

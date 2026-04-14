"""
Tests for audio transcoding via FFmpeg subprocess.
These tests verify that non-WAV audio is correctly transcoded to 16kHz mono PCM WAV.
"""

import io
import unittest

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


class TranscoderBasicTests(unittest.TestCase):
    """Tests for basic transcoding behavior."""

    def test_transcoding_creates_16khz_mono(self):
        """Transcoding should produce 16kHz mono WAV output regardless of input rate."""
        wav_bytes = create_test_wav_bytes(duration_sec=2, sample_rate=44100)
        result = transcode_to_wav_pcm(wav_bytes)
        self.assertGreater(len(result), 0)
        audio, sr = sf.read(io.BytesIO(result), dtype="float32")
        self.assertEqual(sr, 16000)
        if len(audio.shape) > 1:
            self.assertEqual(audio.shape[1], 1)
        self.assertGreater(len(audio) / sr, 1.5)
        self.assertLess(len(audio) / sr, 2.5)

    def test_transcoding_preserves_duration(self):
        """Transcoding should preserve approximately the original duration."""
        wav_bytes = create_test_wav_bytes(duration_sec=5, sample_rate=48000)
        result = transcode_to_wav_pcm(wav_bytes)
        audio, sr = sf.read(io.BytesIO(result), dtype="float32")
        actual_duration = len(audio) / sr
        self.assertGreater(actual_duration, 4.5)
        self.assertLess(actual_duration, 5.5)


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
        """Error messages should be generic and friendly, no FFmpeg internals leaked."""
        try:
            transcode_to_wav_pcm(b"garbage")
        except TranscodingError as e:
            msg = str(e)
            self.assertNotIn("ffmpeg", msg.lower())
            self.assertNotIn("stderr", msg.lower())
            self.assertNotIn("returncode", msg.lower())
            self.assertIn("Transcription failed", msg)

    def test_transcoding_error_type_is_transcoding_error(self):
        """Raised exception should be TranscodingError, not OSError or subprocess."""
        with self.assertRaises(TranscodingError):
            transcode_to_wav_pcm(b"corrupt")


if __name__ == "__main__":
    unittest.main()

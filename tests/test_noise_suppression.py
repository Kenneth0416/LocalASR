import ctypes
import unittest
from unittest.mock import MagicMock, patch
import numpy as np

from config import NoiseSuppressionConfig


def make_sine_pcm16(duration_sec=0.1, freq=440, amplitude=0.5, sr=16000) -> bytes:
    t = np.linspace(0, duration_sec, int(sr * duration_sec), dtype=np.float32)
    signal = amplitude * np.sin(2 * np.pi * freq * t)
    return (signal * 32767).astype(np.int16).tobytes()


class RNNoiseProcessorTests(unittest.TestCase):
    @patch("noise_suppression.ctypes")
    @patch("noise_suppression.ctypes.util.find_library", return_value="/fake/librnnoise.dylib")
    def test_process_returns_pcm16_bytes(self, mock_find, mock_ctypes):
        """process() should return PCM16 bytes of same length as input."""
        from noise_suppression import RNNoiseProcessor

        # Mock ctypes bindings
        mock_lib = MagicMock()
        mock_ctypes.CDLL.return_value = mock_lib
        mock_ctypes.c_float = ctypes.c_float
        mock_ctypes.c_void_p = ctypes.c_void_p
        mock_ctypes.POINTER = ctypes.POINTER
        mock_ctypes.cast = ctypes.cast

        # rnnoise_process_frame returns 0.5 (VAD probability) and fills output buf
        def fake_process_frame(state, out_buf, in_buf):
            for i in range(480):
                out_buf[i] = in_buf[i] * 0.9  # Simulate denoising
            return ctypes.c_float(0.5)

        mock_lib.rnnoise_create = MagicMock(return_value=ctypes.c_void_p(1))
        mock_lib.rnnoise_process_frame = fake_process_frame
        mock_lib.rnnoise_destroy = MagicMock()

        proc = RNNoiseProcessor(sample_rate=16000, library_path="/fake/librnnoise.dylib")
        pcm_in = make_sine_pcm16(duration_sec=0.1)
        pcm_out = proc.process(pcm_in)

        self.assertIsInstance(pcm_out, bytes)
        self.assertEqual(len(pcm_out), len(pcm_in))
        # Output should differ from input (denoising happened)
        self.assertNotEqual(pcm_out, pcm_in)

    @patch("noise_suppression.ctypes.util.find_library", return_value=None)
    def test_raises_when_library_not_found(self, mock_find):
        """RNNoiseProcessor should raise RuntimeError if librnnoise is missing."""
        from noise_suppression import RNNoiseProcessor
        with self.assertRaises(RuntimeError):
            RNNoiseProcessor()

    def test_process_empty_chunk_returns_empty(self):
        """Empty input should return empty output."""
        from noise_suppression import RNNoiseProcessor
        proc = RNNoiseProcessor.__new__(RNNoiseProcessor)
        proc._state = None  # Won't be called
        self.assertEqual(proc.process(b""), b"")

    @patch("noise_suppression.ctypes")
    @patch("noise_suppression.ctypes.util.find_library", return_value="/fake/lib.dylib")
    def test_reset_creates_new_state(self, mock_find, mock_ctypes):
        """reset() should destroy old state and create a new one."""
        from noise_suppression import RNNoiseProcessor

        mock_lib = MagicMock()
        mock_ctypes.CDLL.return_value = mock_lib
        mock_ctypes.c_void_p = ctypes.c_void_p

        proc = RNNoiseProcessor(sample_rate=16000, library_path="/fake/lib.dylib")
        old_destroy_count = mock_lib.rnnoise_destroy.call_count
        proc.reset()
        self.assertEqual(mock_lib.rnnoise_destroy.call_count, old_destroy_count + 1)
        self.assertEqual(mock_lib.rnnoise_create.call_count, 2)  # __init__ + reset()


if __name__ == "__main__":
    unittest.main()

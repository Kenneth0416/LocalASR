import unittest
import numpy as np
from agc import AGCProcessor
from config import AGCConfig


def make_pcm16(samples: np.ndarray) -> bytes:
    """Convert float32 [-1,1] samples to PCM16 bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767).astype(np.int16).tobytes()


def pcm16_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def rms_db(samples: np.ndarray) -> float:
    rms = np.sqrt(np.mean(samples ** 2))
    if rms < 1e-10:
        return -100.0
    return 20.0 * np.log10(rms)


class AGCProcessorTests(unittest.TestCase):
    def test_quiet_input_is_boosted(self):
        """A quiet signal should be boosted toward the target RMS."""
        config = AGCConfig(enabled=True, target_rms_db=-20.0, max_gain_db=30.0)
        agc = AGCProcessor(config=config, sample_rate=16000)

        # Create a quiet 100ms tone at -60 dB RMS
        t = np.linspace(0, 0.1, 1600, dtype=np.float32)
        quiet_signal = 0.001 * np.sin(2 * np.pi * 440 * t)  # ~-60dB RMS
        pcm_in = make_pcm16(quiet_signal)

        pcm_out = agc.process(pcm_in)
        out_float = pcm16_to_float(pcm_out)

        in_rms_db = rms_db(quiet_signal)
        out_rms_db = rms_db(out_float)
        # Output should be louder than input
        self.assertGreater(out_rms_db, in_rms_db + 10.0)

    def test_loud_input_is_not_amplified(self):
        """A loud signal (above target) should not be amplified (gain >= 1.0 only boosts)."""
        config = AGCConfig(enabled=True, target_rms_db=-20.0, max_gain_db=30.0)
        agc = AGCProcessor(config=config, sample_rate=16000)

        # Create a loud signal near 0 dB
        t = np.linspace(0, 0.1, 1600, dtype=np.float32)
        loud_signal = 0.8 * np.sin(2 * np.pi * 440 * t)
        pcm_in = make_pcm16(loud_signal)

        pcm_out = agc.process(pcm_in)
        out_float = pcm16_to_float(pcm_out)

        # Output peak should not exceed input peak by much (soft clipping via tanh)
        self.assertLessEqual(np.max(np.abs(out_float)), np.max(np.abs(loud_signal)) + 0.1)

    def test_output_is_pcm16(self):
        """Output must be valid PCM16 bytes (even length, correct dtype)."""
        config = AGCConfig(enabled=True)
        agc = AGCProcessor(config=config, sample_rate=16000)

        pcm_in = make_pcm16(np.random.randn(1600).astype(np.float32) * 0.01)
        pcm_out = agc.process(pcm_in)

        self.assertEqual(len(pcm_out) % 2, 0)
        recovered = np.frombuffer(pcm_out, dtype=np.int16)
        self.assertEqual(len(recovered), 1600)

    def test_reset_clears_envelope(self):
        """After reset, AGC starts fresh (envelope resets to 0)."""
        config = AGCConfig(enabled=True, target_rms_db=-20.0)
        agc = AGCProcessor(config=config, sample_rate=16000)

        t = np.linspace(0, 0.1, 1600, dtype=np.float32)
        signal = 0.5 * np.sin(2 * np.pi * 440 * t)
        agc.process(make_pcm16(signal))

        self.assertGreater(agc._envelope, 0.0)
        agc.reset()
        self.assertEqual(agc._envelope, 0.0)

    def test_envelope_tracks_across_chunks(self):
        """Envelope should persist across multiple process() calls."""
        config = AGCConfig(enabled=True, target_rms_db=-20.0, attack_sec=0.5, release_sec=1.0)
        agc = AGCProcessor(config=config, sample_rate=16000)

        t = np.linspace(0, 0.1, 1600, dtype=np.float32)
        loud = 0.5 * np.sin(2 * np.pi * 440 * t)
        agc.process(make_pcm16(loud))
        env_after_loud = agc._envelope

        quiet = 0.001 * np.sin(2 * np.pi * 440 * t)
        agc.process(make_pcm16(quiet))
        env_after_quiet = agc._envelope

        # With release > attack, envelope should still be above the quiet level
        self.assertGreater(env_after_quiet, np.sqrt(np.mean(quiet ** 2)))


if __name__ == "__main__":
    unittest.main()

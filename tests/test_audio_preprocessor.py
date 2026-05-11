import unittest
from audio_preprocessor import AudioPreprocessor


class MockProcessor:
    def __init__(self, transform):
        self._transform = transform
        self.reset_count = 0

    def process(self, pcm_chunk: bytes) -> bytes:
        return self._transform(pcm_chunk)

    def reset(self):
        self.reset_count += 1


class AudioPreprocessorTests(unittest.TestCase):
    def test_passthrough_when_no_processors(self):
        pp = AudioPreprocessor()
        pcm = b"\x00\x01\x02\x03"
        self.assertEqual(pp.process(pcm), pcm)

    def test_chains_noise_suppression_then_agc(self):
        """Processors run in order: noise_suppression → agc."""
        ns = MockProcessor(lambda b: b + b"_ns")
        agc = MockProcessor(lambda b: b + b"_agc")
        pp = AudioPreprocessor(noise_suppression=ns, agc=agc)

        result = pp.process(b"audio")
        self.assertEqual(result, b"audio_ns_agc")

    def test_only_noise_suppression(self):
        ns = MockProcessor(lambda b: b + b"_ns")
        pp = AudioPreprocessor(noise_suppression=ns)
        self.assertEqual(pp.process(b"audio"), b"audio_ns")

    def test_only_agc(self):
        agc = MockProcessor(lambda b: b + b"_agc")
        pp = AudioPreprocessor(agc=agc)
        self.assertEqual(pp.process(b"audio"), b"audio_agc")

    def test_reset_calls_all_processors(self):
        ns = MockProcessor(lambda b: b)
        agc = MockProcessor(lambda b: b)
        pp = AudioPreprocessor(noise_suppression=ns, agc=agc)
        pp.reset()
        self.assertEqual(ns.reset_count, 1)
        self.assertEqual(agc.reset_count, 1)

    def test_reset_with_no_processors(self):
        pp = AudioPreprocessor()
        pp.reset()  # Should not raise


if __name__ == "__main__":
    unittest.main()

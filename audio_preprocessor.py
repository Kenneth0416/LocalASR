"""
Audio preprocessing chain — runs noise suppression and AGC on PCM16 bytes.
"""


class AudioPreprocessor:
    """Container for the audio preprocessing chain.

    Processing order: noise_suppression → agc.
    Each processor is optional; if None, that step is skipped.
    """

    def __init__(self, noise_suppression=None, agc=None):
        self._noise_suppression = noise_suppression
        self._agc = agc

    def process(self, pcm_chunk: bytes) -> bytes:
        if self._noise_suppression is not None:
            pcm_chunk = self._noise_suppression.process(pcm_chunk)
        if self._agc is not None:
            pcm_chunk = self._agc.process(pcm_chunk)
        return pcm_chunk

    def reset(self) -> None:
        if self._noise_suppression is not None:
            self._noise_suppression.reset()
        if self._agc is not None:
            self._agc.reset()

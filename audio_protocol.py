"""
Audio packet encoding/decoding - used by both WebSocket and server modules.
"""

import struct

from server_types import AudioFrame

AUDIO_PACKET_MAGIC = b"MRV1"


class AudioPacketDecoder:
    """Decode framed audio packets while keeping legacy raw PCM compatibility."""

    def __init__(self, frame_samples: int):
        self.frame_samples = frame_samples
        self.frame_bytes = frame_samples * 2
        self._legacy_buffer = bytearray()
        self._next_seq = 0
        self.last_seq = -1

    def decode(self, payload: bytes) -> tuple[AudioFrame, ...]:
        if not payload:
            return ()

        if payload.startswith(AUDIO_PACKET_MAGIC):
            frames = self._decode_packet(payload)
        else:
            frames = self._decode_legacy_pcm(payload)

        if frames:
            self.last_seq = frames[-1].seq
        return frames

    def flush_legacy_tail(self) -> tuple[AudioFrame, ...]:
        aligned_len = len(self._legacy_buffer) - (len(self._legacy_buffer) % 2)
        if aligned_len <= 0:
            self._legacy_buffer.clear()
            return ()

        pcm_chunk = bytes(self._legacy_buffer[:aligned_len])
        self._legacy_buffer.clear()
        frame = AudioFrame(
            seq=self._next_seq,
            pcm_chunk=pcm_chunk,
            sample_count=aligned_len // 2,
        )
        self._next_seq += 1
        self.last_seq = frame.seq
        return (frame,)

    def _decode_packet(self, payload: bytes) -> tuple[AudioFrame, ...]:
        if len(payload) < 6:
            raise ValueError("audio packet header too short")

        frame_count = struct.unpack_from("<H", payload, 4)[0]
        offset = 6
        frames: list[AudioFrame] = []

        for _ in range(frame_count):
            if offset + 6 > len(payload):
                raise ValueError("audio frame header truncated")

            seq, sample_count = struct.unpack_from("<IH", payload, offset)
            offset += 6
            byte_len = sample_count * 2
            if offset + byte_len > len(payload):
                raise ValueError("audio frame payload truncated")

            pcm_chunk = payload[offset:offset + byte_len]
            offset += byte_len
            frames.append(
                AudioFrame(
                    seq=seq,
                    pcm_chunk=pcm_chunk,
                    sample_count=sample_count,
                )
            )
            self._next_seq = max(self._next_seq, seq + 1)

        if offset != len(payload):
            raise ValueError("audio packet contains trailing bytes")

        return tuple(frames)

    def _decode_legacy_pcm(self, payload: bytes) -> tuple[AudioFrame, ...]:
        aligned_payload = payload[: len(payload) - (len(payload) % 2)]
        if not aligned_payload:
            return ()

        self._legacy_buffer.extend(aligned_payload)
        frames: list[AudioFrame] = []

        while len(self._legacy_buffer) >= self.frame_bytes:
            pcm_chunk = bytes(self._legacy_buffer[:self.frame_bytes])
            del self._legacy_buffer[:self.frame_bytes]
            frames.append(
                AudioFrame(
                    seq=self._next_seq,
                    pcm_chunk=pcm_chunk,
                    sample_count=self.frame_samples,
                )
            )
            self._next_seq += 1

        return tuple(frames)

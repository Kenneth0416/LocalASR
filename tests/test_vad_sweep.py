#!/usr/bin/env python
"""
VAD parameter sweep: measure segmentation quality at different endpoint_silence_frames.

Usage:
    ./venv/bin/python tests/test_vad_sweep.py
"""

import os
import webrtcvad
import soundfile as sf
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = os.path.join(PROJECT_ROOT, "recordings/20260409-094158-a10ebb41.wav")


def vad_segment(
    audio: np.ndarray,
    sr: int,
    aggressiveness: int,
    enter_speech_frames: int,
    endpoint_silence_frames: int,
    min_duration_sec: float = 0.3,
) -> list[dict]:
    """Split audio into utterances using WebRTC VAD. Logic mirrors vad.py."""
    assert sr == 16000

    vad = webrtcvad.Vad(aggressiveness)
    frame_samples = int(sr * 20 / 1000)  # 20ms
    frame_bytes = frame_samples * 2       # 16-bit PCM

    audio_i16 = (audio * 32767).astype(np.int16)
    pcm_bytes = audio_i16.tobytes()

    utterances = []
    next_id = 1
    active = None
    speech_run = 0
    silence_run = 0
    speech_buffer = bytearray()
    speech_start_sample = 0
    sample_cursor = 0

    for offset in range(0, len(pcm_bytes), frame_bytes):
        frame = pcm_bytes[offset:offset + frame_bytes]
        if len(frame) < frame_bytes:
            break

        is_speech = vad.is_speech(frame, sr)
        frame_start = sample_cursor
        frame_end = sample_cursor + frame_samples
        sample_cursor = frame_end

        if is_speech:
            speech_run += 1
            silence_run = 0
            if speech_run == 1:
                speech_start_sample = frame_start
                speech_buffer.clear()
            if active is None:
                speech_buffer.extend(frame)
        else:
            speech_run = 0
            silence_run += 1

        # Start utterance
        if active is None and is_speech and speech_run == enter_speech_frames:
            active = {
                "id": next_id,
                "start_sample": speech_start_sample,
                "pcm": bytearray(speech_buffer),
                "last_speech_sample": frame_end,
            }
            next_id += 1
            speech_buffer.clear()

        # Accumulate audio into active utterance
        if active is not None:
            if not is_speech:
                pass  # don't add silence frames
            active["pcm"].extend(frame)
            if is_speech:
                active["last_speech_sample"] = frame_end

            # End utterance
            if silence_run >= endpoint_silence_frames:
                end_sample = active["start_sample"] + len(active["pcm"]) // 2
                duration = (end_sample - active["start_sample"]) / sr
                if duration >= min_duration_sec:
                    utterances.append({
                        "id": active["id"],
                        "start_sec": active["start_sample"] / sr,
                        "end_sec": end_sample / sr,
                        "duration_sec": duration,
                    })
                active = None

    # Flush remaining
    if active is not None:
        end_sample = active["start_sample"] + len(active["pcm"]) // 2
        duration = (end_sample - active["start_sample"]) / sr
        if duration >= min_duration_sec:
            utterances.append({
                "id": active["id"],
                "start_sec": active["start_sample"] / sr,
                "end_sec": end_sample / sr,
                "duration_sec": duration,
            })

    return utterances


def measure(utts, audio_dur):
    if not utts:
        return {"count": 0, "avg": 0, "cover": 0, "short": 0, "med": 0, "long": 0, "frag": 0}
    durs = [u["duration_sec"] for u in utts]
    covered = sum(u["end_sec"] - u["start_sec"] for u in utts)
    s = sum(1 for d in durs if d < 1.5)
    m = sum(1 for d in durs if 1.5 <= d < 4.0)
    l = sum(1 for d in durs if d >= 4.0)
    return {
        "count": len(utts),
        "avg": sum(durs) / len(durs),
        "cover": covered / audio_dur,
        "short": s,
        "med": m,
        "long": l,
        "frag": s / len(utts),
    }


def main():
    audio, sr = sf.read(TEST_FILE, dtype="float32")
    dur = len(audio) / sr
    print(f"Audio: {dur:.1f}s, {sr}Hz\n")

    print(f"{'ep':>4} | {'ms':>4} | {'cnt':>4} | {'avg':>5} | {'short':>5} | "
          f"{'med':>4} | {'long':>4} | {'frag':>6} | {'cover':>6}")
    print("-" * 62)

    best = None
    for ep in [10, 15, 18, 24, 27, 30, 36, 45, 54, 72]:
        ms = ep * 20
        utts = vad_segment(audio, sr, aggressiveness=2,
                           enter_speech_frames=2, endpoint_silence_frames=ep)
        m = measure(utts, dur)
        m["ep"] = ep
        if best is None or m["frag"] < best["frag"]:
            best = m
        marker = " ← BEST" if ep == 30 else ""
        print(f"{ep:>4} | {ms:>3}ms | {m['count']:>4} | {m['avg']:>4.1f}s | "
              f"{m['short']:>5} | {m['med']:>4} | {m['long']:>4} | "
              f"{m['frag']:>5.1%} | {m['cover']:>5.1%}{marker}")

    print(f"\n→ Recommended: endpoint={best['ep']} frames ({best['ep']*20}ms)")
    print(f"  {best['count']} utterances, avg={best['avg']:.1f}s, frag={best['frag']:.1%}")

    # Aggressiveness sweep at recommended endpoint
    print(f"\n--- Aggressiveness at endpoint=30 frames ---")
    for agg in [1, 2, 3]:
        utts = vad_segment(audio, sr, aggressiveness=agg,
                           enter_speech_frames=2, endpoint_silence_frames=30)
        m = measure(utts, dur)
        print(f"  agg={agg}: {m['count']} utts, avg={m['avg']:.1f}s, "
              f"short={m['short']}, frag={m['frag']:.1%}, cover={m['cover']:.1%}")

    # Show sample utterances
    print(f"\n--- Sample (agg=2, ep=30) first 15 utterances ---")
    utts = vad_segment(audio, sr, aggressiveness=2,
                        enter_speech_frames=2, endpoint_silence_frames=30)
    print(f"{'#':>3}  {'Start':>6}  {'End':>6}  {'Dur':>5}  Class")
    print("-" * 45)
    for u in utts[:15]:
        d = u["duration_sec"]
        cls = "SHORT" if d < 1.5 else "MED  " if d < 4.0 else "LONG "
        print(f"{u['id']:>3}  {u['start_sec']:>6.1f}s  {u['end_sec']:>6.1f}s  "
              f"{d:>5.1f}s  {cls}")
    if len(utts) > 15:
        print(f"... ({len(utts) - 15} more)")


if __name__ == "__main__":
    main()

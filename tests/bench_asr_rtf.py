"""
ASR Inference RTF (Real-Time Factor) Benchmark
================================================

Measures the ratio: processing_time / audio_duration for the real ASR model.

If RTF > 1.0, inference is slower than real-time and the pipeline WILL
accumulate unbounded delay during continuous speech.

Usage:
    cd /path/to/meeting_realtime_voice
    python tests/bench_asr_rtf.py
"""

import asyncio
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SAMPLE_RATE = 16_000
DURATIONS = [1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 18.0]
REPEATS = 3  # runs per duration


def make_test_audio(duration_sec: float) -> tuple[np.ndarray, int]:
    """Generate a multi-tone signal that exercises the ASR decoder.

    A mix of frequencies + pink noise simulates speech-like spectral
    energy so the model produces non-trivial output.
    """
    n = int(duration_sec * SAMPLE_RATE)
    t = np.linspace(0, duration_sec, n, dtype=np.float32)

    # multi-tone (fundamental + harmonics typical of voice)
    signal = np.zeros(n, dtype=np.float32)
    for freq in [200, 400, 800, 1200, 2000]:
        signal += 0.15 * np.sin(2 * np.pi * freq * t + np.random.uniform(0, 2 * np.pi))

    # add pink-ish noise
    white = np.random.randn(n).astype(np.float32)
    # crude 1/f: cumsum + high-pass
    pink = np.cumsum(white)
    pink -= np.convolve(pink, np.ones(160) / 160, mode="same")
    pink /= max(np.abs(pink).max(), 1e-6)
    signal += 0.08 * pink

    # amplitude envelope (fade in/out to avoid click transients)
    fade = min(int(0.05 * SAMPLE_RATE), n // 4)
    signal[:fade] *= np.linspace(0, 1, fade)
    signal[-fade:] *= np.linspace(1, 0, fade)

    # normalize to [-0.9, 0.9]
    peak = np.abs(signal).max()
    if peak > 0:
        signal = signal / peak * 0.9

    return signal, SAMPLE_RATE


async def main():
    from config import load_config
    from asr_service import ASRService

    print("=" * 64)
    print("  ASR Inference RTF Benchmark (real model)")
    print("=" * 64)
    print()

    _, asr_cfg, _, _, _ = load_config()
    print(f"Model:   {asr_cfg.model_path}")
    print(f"Backend: {asr_cfg.backend}")
    print()

    service = ASRService(asr_cfg)
    print("Loading model …")
    t0 = time.perf_counter()
    await service.wait_ready(timeout=asr_cfg.init_timeout_sec)
    load_time = time.perf_counter() - t0
    print(f"Model loaded in {load_time:.1f}s")
    print()

    # ── warm-up (first inference is always slow) ─────────────────────────
    print("Warm-up inference …")
    warmup_audio = make_test_audio(2.0)
    await service.transcribe_wav(warmup_audio)
    print()

    # ── benchmark ────────────────────────────────────────────────────────
    header = f"{'Duration':>10s}  {'Run':>4s}  {'Proc(s)':>8s}  {'RTF':>8s}  {'Text':>s}"
    print(header)
    print("-" * len(header) + "-" * 40)

    results: dict[float, list[float]] = {}

    for dur in DURATIONS:
        results[dur] = []
        audio = make_test_audio(dur)
        for run in range(1, REPEATS + 1):
            t0 = time.perf_counter()
            result = await service.transcribe_wav(audio)
            proc = time.perf_counter() - t0
            rtf = proc / dur
            results[dur].append(rtf)
            text_preview = (result.text or "").strip()[:50]
            print(f"{dur:10.1f}  {run:4d}  {proc:8.3f}  {rtf:8.3f}  {text_preview}")

    # ── serialized back-to-back (simulates pipeline under load) ──────────
    print()
    print("Serialized back-to-back: 5 × 5s segments …")
    segments = [make_test_audio(5.0) for _ in range(5)]
    total_audio = 5.0 * len(segments)
    t0 = time.perf_counter()
    for seg in segments:
        await service.transcribe_wav(seg)
    total_proc = time.perf_counter() - t0
    serial_rtf = total_proc / total_audio
    print(f"  Total audio: {total_audio:.0f}s  Total proc: {total_proc:.1f}s  RTF: {serial_rtf:.3f}")

    # ── concurrent (simulates multiple sealed utterances waiting) ────────
    print()
    print("Concurrent: 5 × 5s segments (all submitted at once) …")
    t0 = time.perf_counter()
    await asyncio.gather(*(service.transcribe_wav(seg) for seg in segments))
    total_proc_conc = time.perf_counter() - t0
    conc_rtf = total_proc_conc / total_audio
    print(f"  Total audio: {total_audio:.0f}s  Total proc: {total_proc_conc:.1f}s  RTF: {conc_rtf:.3f}")
    print(f"  (Concurrent vs serial speedup: {total_proc / max(total_proc_conc, 0.001):.2f}x)")

    # ── summary ──────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("  Summary")
    print("=" * 64)
    print()
    print(f"{'Duration':>10s}  {'Mean RTF':>10s}  {'Min RTF':>10s}  {'Max RTF':>10s}  {'Verdict':>s}")
    print("-" * 70)

    all_rtfs = []
    for dur in DURATIONS:
        rtfs = results[dur]
        all_rtfs.extend(rtfs)
        mean_rtf = sum(rtfs) / len(rtfs)
        min_rtf = min(rtfs)
        max_rtf = max(rtfs)
        verdict = "OK" if mean_rtf < 0.8 else ("WARN" if mean_rtf < 1.0 else "SLOW")
        print(f"{dur:10.1f}  {mean_rtf:10.3f}  {min_rtf:10.3f}  {max_rtf:10.3f}  {verdict}")

    overall_rtf = sum(all_rtfs) / len(all_rtfs)
    print("-" * 70)
    print(f"{'Overall':>10s}  {overall_rtf:10.3f}")
    print(f"{'Serial':>10s}  {serial_rtf:10.3f}")
    print(f"{'Concurrent':>10s}  {conc_rtf:10.3f}")
    print()

    if overall_rtf >= 1.0:
        print("RESULT: RTF >= 1.0 — inference CANNOT keep up with real-time audio.")
        print("        The pipeline WILL accumulate unbounded delay during speech.")
        print()
        print("  Mitigations:")
        print("  1. Use a smaller model (e.g. 0.6B instead of 1.7B)")
        print("  2. Lower max_utterance_sec to shorten each inference call")
        print("  3. Lower endpoint_silence_frames to cut utterances sooner")
        print("  4. Use GPU (CUDA) instead of CPU/MPS")
    elif overall_rtf >= 0.8:
        print("RESULT: RTF 0.8–1.0 — inference is marginal.")
        print("        Delays may accumulate during dense continuous speech.")
    else:
        print(f"RESULT: RTF {overall_rtf:.3f} — inference is comfortably real-time.")
        print("        Buffer accumulation is likely caused by VAD (no silence gap),")
        print("        not inference speed.")


if __name__ == "__main__":
    asyncio.run(main())

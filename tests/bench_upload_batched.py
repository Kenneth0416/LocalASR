#!/usr/bin/env python3
"""
Benchmark: batched upload transcription vs single-pass.
Usage:
    python3 tests/bench_upload_batched.py <audio_file> [--batch-size 4] [--chunk-sec 120]
"""

import argparse
import asyncio
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("audio", help="Audio file path (mp3/wav/m4a/...)")
    p.add_argument("--chunk-sec", type=float, default=120.0)
    p.add_argument("--max-new-tokens", type=int, default=600)
    p.add_argument("--single-pass", action="store_true",
                   help="Also run single-pass for comparison (slow)")
    return p.parse_args()


async def main():
    args = parse_args()

    from config import ASRConfig
    from asr_service import ASRService

    audio_path = args.audio
    if not os.path.exists(audio_path):
        print(f"ERROR: file not found: {audio_path}")
        sys.exit(1)

    file_size = os.path.getsize(audio_path)
    print(f"File: {audio_path}")
    print(f"Size: {file_size / 1_000_000:.1f} MB")

    # --- Load model ---
    model_path = os.path.expanduser(
        os.environ.get("ASR_MODEL_PATH", "~/whisper-models/Qwen3-ASR-1.7B")
    )
    print(f"\nLoading model: {model_path}")
    t0 = time.time()
    cfg = ASRConfig(
        model_path=model_path,
        max_new_tokens=256,
        upload_chunk_sec=args.chunk_sec,
        upload_min_audio_sec=0,  # force batched path always
        upload_max_new_tokens=args.max_new_tokens,
        upload_workers=args.workers,
    )
    svc = ASRService(cfg)
    await svc.initialize()
    await svc.wait_ready()
    print(f"Model loaded in {time.time() - t0:.1f}s\n")

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    # --- Batched transcription ---
    print(f"=== Batched (chunk={args.chunk_sec:.0f}s, "
          f"max_new_tokens={args.max_new_tokens}) ===")
    t1 = time.time()
    result = await svc.transcribe_wav_batched(
        audio_bytes,
        target_chunk_sec=args.chunk_sec,
        min_audio_sec=0,
    )
    batched_elapsed = time.time() - t1

    audio_dur = result.audio_duration or 0
    rtf = batched_elapsed / audio_dur if audio_dur > 0 else 0
    print(f"Audio duration : {audio_dur:.1f}s ({audio_dur/60:.1f} min)")
    print(f"Segments       : {len(result.segments)}")
    print(f"Characters     : {len(result.text)}")
    print(f"Processing time: {batched_elapsed:.1f}s ({batched_elapsed/60:.1f} min)")
    print(f"RTF            : {rtf:.3f}x  (1.0 = real-time)")
    print(f"Speed          : {audio_dur/batched_elapsed:.1f}x faster than real-time")
    if result.segments:
        print("\nFirst 5 segments:")
        for seg in result.segments[:5]:
            preview = seg['text'][:80].replace('\n', ' ')
            print(f"  [{seg['start']:6.1f}s - {seg['end']:6.1f}s] {preview}")

    # --- Optionally run single-pass for comparison ---
    if args.single_pass:
        print(f"\n=== Single-pass (no chunking) ===")
        t2 = time.time()
        try:
            r2 = await svc.transcribe_wav(audio_bytes)
            single_elapsed = time.time() - t2
            rtf2 = single_elapsed / (r2.audio_duration or 1)
            print(f"Processing time: {single_elapsed:.1f}s ({single_elapsed/60:.1f} min)")
            print(f"RTF            : {rtf2:.3f}x")
            print(f"Characters     : {len(r2.text)}")
            print(f"\nSpeedup (batched vs single): {single_elapsed/batched_elapsed:.2f}x")
        except Exception as e:
            print(f"Single-pass failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())

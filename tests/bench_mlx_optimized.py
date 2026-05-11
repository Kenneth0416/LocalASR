#!/usr/bin/env python3
"""
MLX Qwen3-ASR optimized transcription:
  - Per-chunk independent max_tokens (no shared budget)
  - 2-minute VAD-based chunks for optimal quality
  - Proper Chinese text merging (no spaces)
  - Higher per-chunk token budget

Usage: python3 tests/bench_mlx_optimized.py <audio_file> [model_path]
"""
import sys, time, os, subprocess, json, tempfile
import numpy as np

audio_path = sys.argv[1] if len(sys.argv) > 1 else None
model_path = sys.argv[2] if len(sys.argv) > 2 else \
    "/Users/kennethkwok/.cache/huggingface/hub/models--mlx-community--Qwen3-ASR-1.7B-4bit"

if not audio_path:
    print("Usage: python3 tests/bench_mlx_optimized.py <audio_file> [model_path]")
    sys.exit(1)

# Config
CHUNK_SEC = 120.0        # target chunk size ~2 min
MAX_TOKENS_PER_CHUNK = 2048  # generous per-chunk budget
SEARCH_EXPAND = 15.0     # boundary search window

# Convert to WAV if needed
if not audio_path.endswith('.wav'):
    tmp_wav = tempfile.mktemp(suffix='.wav')
    print(f"Converting {os.path.basename(audio_path)} → WAV ...")
    subprocess.run(['ffmpeg', '-y', '-i', audio_path, '-ar', '16000', '-ac', '1', tmp_wav],
                   capture_output=True, check=True)
    audio_path = tmp_wav

# Get duration
probe = subprocess.run(
    ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', audio_path],
    capture_output=True, text=True
)
info = json.loads(probe.stdout)
duration = max(float(s.get('duration', 0)) for s in info.get('streams', []) if s.get('codec_type') == 'audio')

print(f"Model: {model_path}")
print(f"Audio: {os.path.basename(audio_path)}")
print(f"Duration: {duration:.1f}s ({duration/60:.1f} min)")

# Load model
import mlx
from mlx_audio.stt.utils import load_model
t0 = time.time()
model = load_model(model_path)
print(f"Model loaded: {time.time()-t0:.1f}s")

# Load audio
from mlx_audio.stt.utils import load_audio as mlx_load_audio
import mlx.core as mx
from mlx_audio.audio_io import read as audio_read

raw_audio, sr = audio_read(audio_path, always_2d=True)
if sr != model.sample_rate:
    from mlx_audio.audio_utils import resample_audio
    raw_audio = resample_audio(raw_audio, sr, model.sample_rate)
audio_np = mx.array(raw_audio, dtype=mx.float32).mean(axis=1)
audio_np = np.array(audio_np)

total_len = len(audio_np)
sr_m = model.sample_rate
chunk_samples = int(CHUNK_SEC * sr_m)
expand_samples = int(SEARCH_EXPAND * sr_m)

print(f"Audio loaded: {total_len/sr_m:.1f}s at {sr_m}Hz")

# ── Custom chunking with energy-based boundaries ──
def find_energy_chunks(audio: np.ndarray, sr: int, target_sec: float, expand_sec: float):
    """Split at energy minima, matching MLX's approach."""
    if len(audio) / sr <= target_sec:
        return [(audio, 0.0)]

    chunk_samples = int(target_sec * sr)
    expand = int(expand_sec * sr)
    win = max(4, int(0.05 * sr))  # 50ms window

    chunks = []
    start = 0

    while start < len(audio):
        cut = start + chunk_samples
        if cut >= len(audio):
            chunks.append((audio[start:], start / sr))
            break

        left = max(start, cut - expand)
        right = min(len(audio), cut + expand)
        seg = audio[left:right]
        seg_abs = np.abs(seg)

        if right - left <= win:
            cut_sample = cut
        else:
            energy = np.convolve(seg_abs**2, np.ones(win)/win, mode='valid')
            best = int(np.argmin(energy))
            cut_sample = left + best + win // 2

        chunks.append((audio[start:cut_sample], start / sr))
        start = cut_sample

    return chunks

# ── Per-chunk transcription with independent token budget ──
chunks = find_energy_chunks(audio_np, sr_m, CHUNK_SEC, SEARCH_EXPAND)
print(f"Split into {len(chunks)} chunks (~{CHUNK_SEC:.0f}s each)\n")

all_texts = []
all_segments = []
wall_start = time.time()

for i, (chunk_audio, offset_sec) in enumerate(chunks):
    chunk_dur = len(chunk_audio) / sr_m

    # Call _generate_single_chunk directly with independent budget
    text, prompt_toks, gen_toks = model._generate_single_chunk(
        chunk_audio,
        max_tokens=MAX_TOKENS_PER_CHUNK,
        language="zh",
        prefill_step_size=2048,
        verbose=False,
    )

    # Clean: strip, deduplicate
    text = text.strip()

    all_texts.append(text)
    all_segments.append({
        "text": text,
        "start": offset_sec,
        "end": offset_sec + chunk_dur,
    })

    mx.clear_cache()
    print(f"  [{i+1}/{len(chunks)}] {offset_sec/60:.1f}min - {(offset_sec+chunk_dur)/60:.1f}min: {len(text)} chars, {gen_toks} tokens")

wall_time = time.time() - wall_start

# Merge Chinese text (no spaces between chunks)
full_text = "".join(all_texts)
rtf = wall_time / duration if duration > 0 else 0

print(f"\n{'='*60}")
print(f"Characters : {len(full_text)}")
print(f"Time       : {wall_time:.1f}s ({wall_time/60:.1f} min)")
print(f"RTF        : {rtf:.3f}x")
print(f"Speed      : {duration/wall_time:.1f}x faster than real-time")
print(f"\nFirst 300 chars: {full_text[:300]}")
print(f"\n{'='*60}")
print(f"PyTorch MPS 1.7B:     RTF 0.81x, {duration*0.81/60:.1f}min, 17464 chars")
print(f"MLX 1.7B-4bit (old):  RTF 0.031x, 2.5min, 7083 chars")
print(f"MLX 1.7B-4bit (opt):  RTF {rtf:.3f}x, {wall_time/60:.1f}min, {len(full_text)} chars")
if rtf < 0.81:
    print(f"Speedup vs PyTorch:   {(0.81/rtf):.1f}x FASTER")

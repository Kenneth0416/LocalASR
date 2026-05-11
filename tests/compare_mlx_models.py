#!/usr/bin/env python3
"""
Compare MLX Qwen3-ASR 0.6B vs 1.7B transcription quality.
Usage: python3 tests/compare_mlx_models.py <audio_file>
"""
import sys, time, os, subprocess, json, tempfile
import numpy as np

audio_path = sys.argv[1] if len(sys.argv) > 1 else None
if not audio_path:
    print("Usage: python3 tests/compare_mlx_models.py <audio_file>")
    sys.exit(1)

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
print(f"Audio: {os.path.basename(audio_path)}, Duration: {duration:.1f}s ({duration/60:.1f} min)\n")

# Load audio once
import mlx.core as mx
from mlx_audio.audio_io import read as audio_read

raw_audio, sr = audio_read(audio_path, always_2d=True)
audio_np = mx.array(raw_audio, dtype=mx.float32).mean(axis=1)
audio_np = np.array(audio_np)

# Config
CHUNK_SEC = 120.0
MAX_TOKENS = 2048
SEARCH_EXPAND = 15.0
TEMPERATURE = 0.1
REPETITION_PENALTY = 1.2
CONTEXT_SIZE = 100

# Energy-based chunking
def find_energy_chunks(audio: np.ndarray, sr: int, target_sec: float, expand_sec: float):
    if len(audio) / sr <= target_sec:
        return [(audio, 0.0)]
    chunk_samples = int(target_sec * sr)
    expand = int(expand_sec * sr)
    win = max(4, int(0.05 * sr))
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
        if right - left <= win:
            cut_sample = cut
        else:
            energy = np.convolve(seg**2, np.ones(win)/win, mode='valid')
            best = int(np.argmin(energy))
            cut_sample = left + best + win // 2
        chunks.append((audio[start:cut_sample], start / sr))
        start = cut_sample
    return chunks

from mlx_lm.sample_utils import make_sampler, make_logits_processors
from mlx_audio.stt.utils import load_model

models = {
    "0.6B-4bit": "mlx-community/Qwen3-ASR-0.6B-4bit",
    "1.7B-4bit": "mlx-community/Qwen3-ASR-1.7B-4bit",
}

results = {}

for name, model_path in models.items():
    print(f"\n{'='*60}")
    print(f"Model: {name}")
    print(f"{'='*60}")

    t0 = time.time()
    model = load_model(model_path)
    print(f"Model loaded in {time.time()-t0:.1f}s")

    sr_m = model.sample_rate
    if sr != sr_m:
        import scipy.signal
        num_samples = int(len(audio_np) * sr_m / sr)
        audio_work = scipy.signal.resample(audio_np, num_samples)
    else:
        audio_work = audio_np

    chunks = find_energy_chunks(audio_work, sr_m, CHUNK_SEC, SEARCH_EXPAND)
    print(f"Chunks: {len(chunks)}")

    sampler = make_sampler(TEMPERATURE)
    logits_processors = make_logits_processors(
        repetition_penalty=REPETITION_PENALTY,
        repetition_context_size=CONTEXT_SIZE,
    )

    all_texts = []
    wall_start = time.time()

    for i, (chunk_audio, offset_sec) in enumerate(chunks):
        chunk_dur = len(chunk_audio) / sr_m
        text, _prompt_toks, gen_toks = model._generate_single_chunk(
            chunk_audio,
            max_tokens=MAX_TOKENS,
            sampler=sampler,
            logits_processors=logits_processors,
            language="zh",
            prefill_step_size=2048,
            verbose=False,
        )
        text = text.strip()
        if "<asr_text>" in text:
            text = text.split("<asr_text>", 1)[-1]
        all_texts.append(text)
        mx.clear_cache()
        print(f"  [{i+1}/{len(chunks)}] {offset_sec/60:.1f}min-{offset_sec/60+chunk_dur/60:.1f}min: {len(text)} chars, {gen_toks} tokens")

    wall_time = time.time() - wall_start
    full_text = "".join(all_texts)
    rtf = wall_time / duration if duration > 0 else 0

    print(f"\n--- RESULTS for {name} ---")
    print(f"Characters : {len(full_text)}")
    print(f"Time       : {wall_time:.1f}s ({wall_time/60:.1f} min)")
    print(f"RTF        : {rtf:.3f}x")
    print(f"Speed      : {duration/wall_time:.1f}x real-time")
    print(f"\nFirst 500 chars:\n{full_text[:500]}")

    results[name] = {
        "text": full_text,
        "chars": len(full_text),
        "time": wall_time,
        "rtf": rtf,
    }

# Summary comparison
print(f"\n\n{'='*60}")
print("COMPARISON")
print(f"{'='*60}")
for name, r in results.items():
    print(f"{name:12s}: {r['chars']:6d} chars, {r['time']:5.1f}s, RTF={r['rtf']:.3f}")

# Save outputs
for name, r in results.items():
    safe_name = name.replace(".", "_")
    out_path = f"/tmp/mlx_compare_{safe_name}.txt"
    with open(out_path, "w") as f:
        f.write(r["text"])
    print(f"\nSaved {name} output to: {out_path}")

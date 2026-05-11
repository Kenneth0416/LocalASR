#!/usr/bin/env python3
"""
MLX Qwen3-ASR benchmark: load from local path directly.
Usage: python3 tests/bench_mlx_asr.py <audio_file> [model_path]
"""
import sys, time, os, subprocess, json, tempfile

audio_path = sys.argv[1] if len(sys.argv) > 1 else None
# ModelScope downloaded model.safetensors to this HF cache dir
model_path = sys.argv[2] if len(sys.argv) > 2 else \
    "/Users/kennethkwok/.cache/huggingface/hub/models--mlx-community--Qwen3-ASR-1.7B-4bit"

if not audio_path:
    print("Usage: python3 tests/bench_mlx_asr.py <audio_file> [model_path]")
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

print(f"Model: {model_path}")
print(f"Audio: {os.path.basename(audio_path)}")
print(f"Duration: {duration:.1f}s ({duration/60:.1f} min)")

# Load
t0 = time.time()
from mlx_audio.stt.utils import load_model
model = load_model(model_path)
print(f"Model loaded: {time.time()-t0:.1f}s")

# Transcribe
print(f"\n=== MLX 1.7B-4bit transcription ===")
t1 = time.time()
output = model.generate(
    audio_path,
    language="zh",
    chunk_duration=1200.0,
    min_chunk_duration=1.0,
    verbose=False,
)
t1 = time.time() - t1

if hasattr(output, 'text'):
    text = output.text
elif hasattr(output, 'segments') and output.segments:
    text = " ".join(seg.text for seg in output.segments if hasattr(seg, 'text'))
else:
    text = str(output)

rtf = t1 / duration if duration > 0 else 0

print(f"Characters : {len(text)}")
print(f"Time       : {t1:.1f}s ({t1/60:.1f} min)")
print(f"RTF        : {rtf:.3f}x")
print(f"Speed      : {duration/t1:.1f}x faster than real-time")
print(f"\nFirst 300 chars: {text[:300]}")
print(f"\n{'='*50}")
print(f"PyTorch MPS 1.7B:     RTF 0.81x, {duration*0.81/60:.1f}min, 17464 chars")
print(f"MLX 0.6B-4bit:        RTF 0.026x, 2.0min, 7218 chars")
print(f"MLX 1.7B-4bit:        RTF {rtf:.3f}x, {t1/60:.1f}min, {len(text)} chars")
if rtf < 0.81:
    print(f"MLX 1.7B is {(0.81/rtf):.1f}x FASTER than PyTorch MPS!")

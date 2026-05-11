"""
Post-processing utilities for ASR text output.

Detects and removes hallucination patterns common in Whisper-family models:
  - Single-character runs  (哎哎哎哎哎哎… → 哎哎)
  - N-gram loops           (ABCABC… → ABC)
  - Trailing repetition    (…XYZXYZXYZ → …XYZ)
"""

import re

__all__ = ["strip_hallucination"]

# ── single-character run ────────────────────────────────────────────────────
_CHAR_RUN_RE = re.compile(r"(.)\1{9,}")          # 10+ identical chars


def _collapse_char_runs(text: str) -> str:
    """哎哎哎哎哎哎哎哎哎哎 → 哎哎"""
    return _CHAR_RUN_RE.sub(r"\1\1", text)


# ── n-gram loop ─────────────────────────────────────────────────────────────
# Pre-compile patterns for common repeating-unit lengths (4-40 chars).
_NGRAM_RES: list[re.Pattern] = [
    re.compile(r"(.{" + str(n) + r"})\1{2,}")
    for n in range(4, 41)
]


def _collapse_ngram_loops(text: str) -> str:
    """Remove any substring of 4-40 chars repeated 3+ times consecutively."""
    for pat in _NGRAM_RES:
        text = pat.sub(r"\1", text)
    return text


# ── trailing repetition ─────────────────────────────────────────────────────

def _trim_trailing_loop(text: str, min_unit: int = 4, max_unit: int = 60) -> str:
    """If the text *ends* with a repeating pattern, trim to one occurrence."""
    length = len(text)
    if length < min_unit * 3:
        return text
    upper = min(max_unit, length // 3) + 1
    for unit_len in range(min_unit, upper):
        tail = text[-unit_len:]
        count = 0
        pos = length - unit_len
        while pos >= 0 and text[pos : pos + unit_len] == tail:
            count += 1
            pos -= unit_len
        if count >= 2:
            # Keep one occurrence
            keep_end = pos + unit_len + unit_len
            return text[:keep_end]
    return text


# ── public API ──────────────────────────────────────────────────────────────

def strip_hallucination(text: str) -> str:
    """Remove repetitive hallucination patterns from ASR output.

    Safe to call on any text — legitimate speech is not affected because
    the thresholds (10+ identical chars, 3+ consecutive phrase repeats)
    are far beyond what natural language produces.
    """
    if not text or len(text) < 20:
        return text

    text = _collapse_char_runs(text)
    text = _collapse_ngram_loops(text)
    text = _trim_trailing_loop(text)

    return text.rstrip()

"""
Alignment model adapters used by the realtime ASR pipeline.
"""

from dataclasses import dataclass
from typing import Any


class AlignmentModelError(RuntimeError):
    """Raised when an alignment backend cannot be loaded."""


@dataclass
class Qwen3Alignment:
    """
    Thin compatibility wrapper around qwen_asr's forced aligner.

    The project refers to the alignment backend as `qwen3alignment`, while the
    upstream package currently exposes `Qwen3ForcedAligner`. This adapter keeps
    that naming stable on our side and still provides the attributes used by the
    ASR model integration (`model`, `device`, and `align`).
    """

    backend: Any

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path: str, **kwargs) -> "Qwen3Alignment":
        try:
            from qwen_asr import Qwen3ForcedAligner
        except ImportError as exc:
            raise AlignmentModelError(f"Missing qwen_asr forced aligner dependency: {exc}") from exc

        backend = Qwen3ForcedAligner.from_pretrained(pretrained_model_name_or_path, **kwargs)
        return cls(backend=backend)

    @property
    def model(self):
        return self.backend.model

    @model.setter
    def model(self, value):
        self.backend.model = value

    @property
    def device(self):
        return getattr(self.backend, "device", None)

    @device.setter
    def device(self, value):
        self.backend.device = value

    def align(self, *args, **kwargs):
        return self.backend.align(*args, **kwargs)


def load_alignment_model(
    backend: str,
    pretrained_model_name_or_path: str,
    **kwargs,
):
    """Load a supported alignment backend by stable project-facing name."""
    normalized = str(backend or "qwen3alignment").strip().lower()

    if normalized in {
        "qwen3alignment",
        "qwen3-alignment",
        "forced_aligner",
        "qwen3forcedaligner",
        "qwen3-forced-aligner",
    }:
        return Qwen3Alignment.from_pretrained(pretrained_model_name_or_path, **kwargs)

    raise AlignmentModelError(f"Unsupported alignment backend: {backend}")

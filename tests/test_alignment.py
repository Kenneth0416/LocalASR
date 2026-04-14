import unittest
from unittest.mock import patch

from alignment import AlignmentModelError, load_alignment_model


class AlignmentModelTests(unittest.TestCase):
    def test_load_alignment_model_supports_qwen3alignment_alias(self):
        sentinel = object()

        with patch("alignment.Qwen3Alignment.from_pretrained", return_value=sentinel) as mocked:
            result = load_alignment_model("qwen3alignment", "/tmp/qwen3-aligner")

        self.assertIs(result, sentinel)
        mocked.assert_called_once_with("/tmp/qwen3-aligner")

    def test_load_alignment_model_rejects_unknown_backend(self):
        with self.assertRaises(AlignmentModelError):
            load_alignment_model("unknown-aligner", "/tmp/qwen3-aligner")

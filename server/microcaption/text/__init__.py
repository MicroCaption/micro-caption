"""Text utilities — word-error-rate scoring and tokenisation."""

from .wer import compute_wer, compute_wer_detailed, tokenize

__all__ = ['compute_wer', 'compute_wer_detailed', 'tokenize']

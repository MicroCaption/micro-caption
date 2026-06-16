"""
Per-stream accuracy verification + persistent logging.

  - SessionStore     — filesystem persistence of per-stream caption logs and
                       accuracy summaries (survives server restarts).
  - AccuracyVerifier — a second, slower ASR pass that re-transcribes the same
                       audio at high accuracy and scores how closely the live
                       captions matched it (WER → accuracy %).
"""

from .store import SessionStore
from .verifier import AccuracyVerifier

__all__ = ['SessionStore', 'AccuracyVerifier']

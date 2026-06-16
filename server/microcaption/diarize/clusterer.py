"""
Online speaker clustering → stable ``SPEAKER N`` labels.

Maintains one centroid per discovered speaker. Each utterance embedding is
assigned to the nearest centroid if it's similar enough (cosine >=
cluster_threshold); otherwise it starts a new speaker. Centroids update as a
running mean, so they sharpen over the course of a meeting. Labels are handed out
in first-appearance order and never renumbered — SPEAKER 1 stays SPEAKER 1 for
the whole stream, even when that speaker stops talking and returns later.
"""

from typing import List, Optional

import numpy as np

from .embedder import cosine


class OnlineSpeakerClusterer:
    def __init__(self, config: Optional[dict] = None) -> None:
        d = config or {}
        sp = d.get('second_pass', {})
        self._threshold: float = float(sp.get('cluster_threshold', 0.45))
        self._centroids: List[np.ndarray] = []   # L2-normalised running means
        self._counts: List[int] = []

    @property
    def num_speakers(self) -> int:
        return len(self._centroids)

    def assign(self, emb: Optional[np.ndarray]) -> int:
        """Return a 0-based stable speaker index for this embedding (-1 if None)."""
        if emb is None:
            return -1
        if not self._centroids:
            self._add(emb)
            return 0
        sims = [cosine(emb, c) for c in self._centroids]
        best = int(np.argmax(sims))
        if sims[best] >= self._threshold:
            self._update(best, emb)
            return best
        self._add(emb)
        return len(self._centroids) - 1

    @staticmethod
    def label(idx: int) -> str:
        """Human-readable label for a speaker index ('' for the no-speaker -1)."""
        return f'SPEAKER {idx + 1}' if idx >= 0 else ''

    # ── internals ─────────────────────────────────────────────────────────────

    def _add(self, emb: np.ndarray) -> None:
        self._centroids.append(self._normed(emb))
        self._counts.append(1)

    def _update(self, i: int, emb: np.ndarray) -> None:
        k = self._counts[i]
        merged = (self._centroids[i] * k + self._normed(emb)) / (k + 1)
        self._centroids[i] = self._normed(merged)
        self._counts[i] = k + 1

    @staticmethod
    def _normed(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v / n if n > 0.0 else v

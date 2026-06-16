"""
Online speaker clustering → stable ``SPEAKER N`` labels.

Maintains one centroid per discovered speaker. Each utterance embedding is
assigned to the nearest centroid if it's similar enough, otherwise it starts a
new speaker. Labels are handed out in first-appearance order and never
renumbered — SPEAKER 1 stays SPEAKER 1 for the whole stream.

**Mean-normalisation (centering).** Raw speaker embeddings carry a large shared
component (recording channel, room, codec) that dominates cosine similarity —
measured on real broadcast audio, *different* speakers sat at ~0.7 cosine, so no
threshold could separate them. We therefore keep a running mean of all embeddings
and compare in the *centered* space (embedding − mean), where same-speaker
similarity is positive and different-speaker is negative. Centroids are stored in
raw space (a running mean per speaker) and centered at comparison time with the
current global mean, so they never go stale as the mean settles. Before warmup
(too few embeddings to estimate the mean) we fall back to raw cosine.
"""

from typing import List, Optional

import numpy as np

from .embedder import cosine


class OnlineSpeakerClusterer:
    def __init__(self, config: Optional[dict] = None) -> None:
        d = config or {}
        sp = d.get('second_pass', {})
        # Threshold is a cosine similarity in the CENTERED space (≈ -1..1):
        # >= threshold → same speaker, else a new speaker. ~0.0–0.15 in practice.
        self._threshold: float = float(sp.get('cluster_threshold', 0.10))
        self._warmup: int = int(sp.get('center_warmup', 3))
        self._centroids: List[np.ndarray] = []   # running mean of raw embeddings
        self._counts: List[int] = []
        self._sum: Optional[np.ndarray] = None    # running sum for the global mean
        self._n: int = 0

    @property
    def num_speakers(self) -> int:
        return len(self._centroids)

    def assign(self, emb: Optional[np.ndarray]) -> int:
        """Return a 0-based stable speaker index for this embedding (-1 if None)."""
        if emb is None:
            return -1
        emb = np.asarray(emb, dtype=np.float32)
        self._sum = emb.copy() if self._sum is None else self._sum + emb
        self._n += 1

        if not self._centroids:
            self._add(emb)
            return 0

        mean = (self._sum / self._n) if self._n >= self._warmup else None
        q = self._centered(emb, mean)
        if q is None:                       # degenerate (emb ≈ mean): use raw
            q = self._centered(emb, None)
        sims = []
        for c in self._centroids:
            cc = self._centered(c, mean)
            if cc is None:
                cc = self._centered(c, None)
            sims.append(cosine(q, cc) if (q is not None and cc is not None) else -1.0)
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

    @staticmethod
    def _centered(v: np.ndarray, mean: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """Unit-normalised (v − mean); None if degenerate (≈ zero length).
        With mean=None this is just the unit-normalised raw vector."""
        c = v if mean is None else v - mean
        n = float(np.linalg.norm(c))
        return c / n if n > 1e-8 else None

    def _add(self, emb: np.ndarray) -> None:
        self._centroids.append(emb.copy())
        self._counts.append(1)

    def _update(self, i: int, emb: np.ndarray) -> None:
        k = self._counts[i]
        self._centroids[i] = (self._centroids[i] * k + emb) / (k + 1)
        self._counts[i] = k + 1

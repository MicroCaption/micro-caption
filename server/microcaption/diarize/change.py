"""
Live speaker-change detection — the ``>>`` mark.

Compares each new utterance's speaker embedding to the previous utterance's; when
they're dissimilar the turn changed hands. Like the behind-live clusterer, it
compares in the **centered** space (embedding − running mean): raw embeddings
share a large channel component that pins different speakers near ~0.7 cosine, so
centering is what makes the comparison actually reflect speaker identity. Cheap
(one embedding per utterance); per-session state lives here, the model is shared.

The authoritative, consistent ``SPEAKER N`` numbering is still produced behind
live by OnlineSpeakerClusterer; this is the immediate, best-effort live hint.
"""

from typing import Optional

import numpy as np

from .embedder import cosine

_SAMPLE_RATE = 16000


class SpeakerChangeDetector:
    def __init__(self, embedder, config: Optional[dict] = None) -> None:
        d = config or {}
        live = d.get('live', {})
        self._embedder = embedder
        # A cosine similarity in the CENTERED space: consecutive utterances below
        # this are treated as different speakers (a turn change).
        self._sim_threshold: float = float(live.get('change_threshold', 0.10))
        self._warmup: int = int(live.get('center_warmup', 3))
        self._min_samples = int(float(d.get('min_utterance_seconds', 0.8)) * _SAMPLE_RATE)
        self._prev_raw: Optional[np.ndarray] = None
        self._sum: Optional[np.ndarray] = None
        self._n: int = 0

    @property
    def enabled(self) -> bool:
        return self._embedder is not None and getattr(self._embedder, 'available', False)

    def reset(self) -> None:
        """Forget the previous speaker + running mean (new session / long gap)."""
        self._prev_raw = None
        self._sum = None
        self._n = 0

    def is_change(self, samples: np.ndarray) -> bool:
        """True if this utterance is a different speaker than the previous one.

        The first utterance (no prior reference) is never a change. Utterances
        shorter than min_utterance_seconds — e.g. roll-call "Aye"/"Nay" — are
        skipped: they don't update the reference and never flag a change.
        """
        if not self.enabled:
            return False
        if samples is None or len(samples) < self._min_samples:
            return False
        emb = self._embedder.embed(samples)
        if emb is None:
            return False
        emb = np.asarray(emb, dtype=np.float32)
        self._sum = emb.copy() if self._sum is None else self._sum + emb
        self._n += 1

        prev_raw = self._prev_raw
        self._prev_raw = emb
        if prev_raw is None:
            return False

        mean = (self._sum / self._n) if self._n >= self._warmup else None
        a = self._centered(prev_raw, mean)
        b = self._centered(emb, mean)
        if a is None or b is None:
            return False
        return cosine(a, b) < self._sim_threshold

    @staticmethod
    def _centered(v: np.ndarray, mean: Optional[np.ndarray]) -> Optional[np.ndarray]:
        c = v if mean is None else v - mean
        n = float(np.linalg.norm(c))
        return c / n if n > 1e-8 else None

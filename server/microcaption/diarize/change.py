"""
Live speaker-change detection — the ``>>`` mark.

Compares each new utterance's speaker embedding to the previous utterance's; a
large cosine distance means the turn changed hands. Deliberately cheap (one
embedding per utterance) because the authoritative, consistent ``SPEAKER N``
numbering is produced behind live by OnlineSpeakerClusterer. Per-session state
(the previous embedding) lives here; the embedding model is shared.
"""

from typing import Optional

import numpy as np

from .embedder import cosine_distance

_SAMPLE_RATE = 16000


class SpeakerChangeDetector:
    def __init__(self, embedder, config: Optional[dict] = None) -> None:
        d = config or {}
        live = d.get('live', {})
        self._embedder = embedder
        self._threshold: float = float(live.get('change_threshold', 0.55))
        self._min_samples = int(float(d.get('min_utterance_seconds', 0.8)) * _SAMPLE_RATE)
        self._prev: Optional[np.ndarray] = None

    @property
    def enabled(self) -> bool:
        return self._embedder is not None and getattr(self._embedder, 'available', False)

    def reset(self) -> None:
        """Forget the previous speaker (e.g. on a long silence / new session)."""
        self._prev = None

    def is_change(self, samples: np.ndarray) -> bool:
        """True if this utterance is a different speaker than the previous one.

        The first utterance (no prior reference) is never a change. Utterances
        shorter than min_utterance_seconds — e.g. roll-call "Aye"/"Nay" — are
        skipped: they don't update the reference and never flag a change, so the
        previous speaker's context carries through.
        """
        if not self.enabled:
            return False
        if samples is None or len(samples) < self._min_samples:
            return False
        emb = self._embedder.embed(samples)
        if emb is None:
            return False
        prev = self._prev
        self._prev = emb
        if prev is None:
            return False
        return cosine_distance(prev, emb) > self._threshold

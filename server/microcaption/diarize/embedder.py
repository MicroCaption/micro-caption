"""
Speaker embedding for diarization.

A speaker-verification model turns a slice of utterance audio into a fixed-length
embedding; cosine similarity between embeddings says whether two utterances are
the same voice. Used by the live SpeakerChangeDetector (``>>`` marks) and the
behind-live OnlineSpeakerClusterer (``SPEAKER N`` labels).

Backend: **sherpa-onnx** (k2-fsa) — ONNX Runtime under the hood, with a cp314
wheel, so no PyTorch on the critical path (which matters on Python 3.14). Crucially,
sherpa-onnx performs the *correct* kaldi-fbank feature extraction the WeSpeaker /
3D-Speaker models were trained on, so we don't have to replicate it by hand.

Models: a single ``.onnx`` file from
https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models
(e.g. ``wespeaker_en_voxceleb_resnet34_LM.onnx``). Point ``diarize.model`` at it.

Graceful degradation is a hard requirement: any failure to import sherpa-onnx,
find/open the model, or run inference leaves ``available = False`` and the whole
diarization layer becomes a no-op — the caption pipeline is untouched.
"""

import os
import threading
from typing import Optional

import numpy as np

_SAMPLE_RATE = 16000


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors. Returns 0.0 for a degenerate vector."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """1 - cosine similarity; 0 = identical direction, 2 = opposite."""
    return 1.0 - cosine(a, b)


class SpeakerEmbedder:
    """sherpa-onnx speaker-embedding model, shared across sessions.

    ``embed()`` is serialised by a lock so concurrent per-session callers (live
    detector + behind-live pass across multiple streams) can't race the shared
    extractor. Embedding is cheap and per-utterance, so the lock isn't a
    throughput concern.
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        d = config or {}
        # Local path to a sherpa-onnx speaker-embedding .onnx file.
        self._model_ref: str = d.get('model', '')
        self._device: str = d.get('device', 'cpu')
        self._num_threads: int = int(d.get('num_threads', 1))
        self._sr = _SAMPLE_RATE

        self._extractor = None
        self._lock = threading.Lock()
        self._available = False
        self._load_error = ''

    @property
    def available(self) -> bool:
        return self._available

    @property
    def load_error(self) -> str:
        return self._load_error

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load the model. Never raises — failures set load_error and leave
        available=False so callers can disable diarization cleanly."""
        try:
            import sherpa_onnx  # type: ignore
        except Exception as exc:
            self._load_error = f'sherpa-onnx import failed: {exc}'
            print(f'[Embedder] {self._load_error}')
            return

        if not self._model_ref:
            self._load_error = 'no speaker-embedding model configured (diarize.model)'
            print(f'[Embedder] disabled: {self._load_error}')
            return
        if not os.path.isfile(self._model_ref):
            self._load_error = f'model file not found: {self._model_ref}'
            print(f'[Embedder] disabled: {self._load_error}')
            return

        try:
            # sherpa-onnx provider: 'cpu' | 'cuda' | 'coreml'.
            provider = 'cuda' if self._device == 'cuda' else 'cpu'
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self._model_ref,
                num_threads=self._num_threads,
                provider=provider,
            )
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            self._available = True
            print(f'[Embedder] Loaded speaker model {self._model_ref} '
                  f'(dim={self.dim}, {provider})')
        except Exception as exc:
            self._load_error = f'extractor init failed: {exc}'
            self._extractor = None
            print(f'[Embedder] disabled: {self._load_error}')

    @property
    def dim(self) -> int:
        """Embedding dimension, or 0 if unavailable."""
        if self._extractor is None:
            return 0
        try:
            return int(self._extractor.dim)
        except Exception:
            return 0

    # ── inference ─────────────────────────────────────────────────────────────

    def embed(self, samples: np.ndarray) -> Optional[np.ndarray]:
        """L2-normalised speaker embedding for a mono 16 kHz float32 slice in
        [-1, 1], or None if the model is unavailable / the slice is too short /
        inference errors. sherpa-onnx computes the kaldi-fbank front end itself."""
        if not self._available or self._extractor is None:
            return None
        if samples is None or len(samples) < int(0.2 * self._sr):
            return None
        try:
            wav = np.ascontiguousarray(samples, dtype=np.float32)
            with self._lock:
                stream = self._extractor.create_stream()
                stream.accept_waveform(sample_rate=self._sr, waveform=wav)
                stream.input_finished()
                emb = self._extractor.compute(stream)
            emb = np.asarray(emb, dtype=np.float32).reshape(-1)
            n = float(np.linalg.norm(emb))
            return emb / n if n > 0.0 else None
        except Exception as exc:
            print(f'[Embedder] embed failed: {exc}')
            return None

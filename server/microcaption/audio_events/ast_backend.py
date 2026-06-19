"""AudioSet tagging via the Audio Spectrogram Transformer (AST).

Wraps ``MIT/ast-finetuned-audioset-10-10-0.4593`` (527-class AudioSet, the
universal sound taxonomy). Loaded once and shared across sessions — inference
is serialised by a lock like the ASR backend, since all sessions share one GPU.

The model is multi-label: each class gets an independent sigmoid probability,
so several events (e.g. Music + Applause) can be reported for one window.
"""

import threading
from typing import Dict, List

import numpy as np

_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
_SAMPLE_RATE = 16000


class ASTTagger:
    """Shared AudioSet tagger. Call load() once, then classify() per window."""

    def __init__(self, config: dict) -> None:
        self._model_name: str = config.get("model", _MODEL)
        self._device: str = config.get("device", "cuda")
        self._lock = threading.Lock()
        self._model = None
        self._fe = None
        self._id2label: Dict[int, str] = {}
        self._sigmoid = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def load(self) -> None:
        import torch
        from transformers import (AutoFeatureExtractor,
                                   AutoModelForAudioClassification)

        self._fe = AutoFeatureExtractor.from_pretrained(self._model_name)
        model = AutoModelForAudioClassification.from_pretrained(self._model_name)
        model.eval()
        # Fall back to CPU if CUDA was requested but is unavailable.
        if self._device.startswith("cuda") and not torch.cuda.is_available():
            print("[AudioEvents] CUDA unavailable, AST tagger on CPU")
            self._device = "cpu"
        model.to(self._device)
        self._model = model
        self._id2label = model.config.id2label
        self._sigmoid = torch.sigmoid
        print(f"[AudioEvents] AST tagger loaded ({self._model_name}) on "
              f"{self._device} — {model.config.num_labels} AudioSet classes")

    @property
    def id2label(self) -> List[str]:
        return list(self._id2label.values())

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE

    # ── inference ────────────────────────────────────────────────────────────
    def classify(self, samples: np.ndarray) -> Dict[str, float]:
        """Return {AudioSet label: probability} for one mono 16 kHz window."""
        import torch

        if self._model is None:
            return {}
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)

        with self._lock:
            inputs = self._fe(samples, sampling_rate=_SAMPLE_RATE,
                              return_tensors="pt")
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
            with torch.no_grad():
                logits = self._model(**inputs).logits
            probs = self._sigmoid(logits)[0].float().cpu().numpy()

        return {self._id2label[i]: float(p) for i, p in enumerate(probs)}

    def unload(self) -> None:
        self._model = None
        self._fe = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

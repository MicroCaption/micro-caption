"""
DiarizationPass — second, behind-live pass that assigns stable SPEAKER N labels.

Mirrors AccuracyVerifier (accuracy/verifier.py): it buffers the same audio the
live pipeline hears, then, behind live, groups committed caption cues into
utterances (consecutive cues separated by less than ``utterance_gap_seconds``),
embeds each utterance's audio slice, clusters it into a stable speaker, and emits
``[{start, speaker}]`` updates so the stored cues / replay transcript get
labelled. Never blocks live. This pass is the source of truth for who-spoke
labels; the live ``>>`` marks are best-effort.
"""

import threading
import time
from typing import Callable, List, Optional

import numpy as np

from ..io.base import AudioChunk
from .clusterer import OnlineSpeakerClusterer

_SAMPLE_RATE = 16000

# updates: list of {'start': float, 'speaker': str} → None
UpdateCallback = Callable[[List[dict]], None]


class DiarizationPass:
    def __init__(self, embedder, config: dict,
                 get_live_cues: Callable[[], List[dict]],
                 on_update: Optional[UpdateCallback] = None,
                 worker_name: str = 'diarizer') -> None:
        self._embedder = embedder
        self._get_live_cues = get_live_cues
        self._on_update = on_update
        self._worker_name = worker_name
        self._sr = _SAMPLE_RATE

        d = config or {}
        sp = d.get('second_pass', {})
        self._settle: float = float(sp.get('settle_seconds', 5.0))
        self._gap: float = float(sp.get('utterance_gap_seconds', 0.8))
        self._pad: float = float(sp.get('context_seconds', 0.25))
        self._min_utt_samples = int(float(d.get('min_utterance_seconds', 0.8)) * self._sr)
        # Retain enough audio to re-embed an utterance after the settle delay.
        self._retain_seconds: float = max(60.0, self._settle + 60.0)

        self._clusterer = OnlineSpeakerClusterer(d)

        # Rolling audio buffer with an absolute-time index (same scheme as the verifier).
        self._audio_lock = threading.Lock()
        self._buf = np.array([], dtype=np.float32)
        self._buf_start = 0.0
        self._primed = False

        self._cursor = 0          # index of the next un-labelled live cue
        self._running = False
        self._worker: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return self._embedder is not None and getattr(self._embedder, 'available', False)

    @property
    def num_speakers(self) -> int:
        return self._clusterer.num_speakers

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.enabled:
            print('[Diarizer] speaker embedder unavailable — SPEAKER labels disabled')
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._loop, daemon=True, name=self._worker_name)
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        if self._worker:
            self._worker.join(timeout=8.0)
        try:
            self._label_ready(force=True)
        except Exception as exc:
            print(f'[Diarizer] final labelling error: {exc}')

    def on_audio(self, chunk: AudioChunk) -> None:
        if not self.enabled:
            return
        n = len(chunk.samples)
        with self._audio_lock:
            if not self._primed:
                self._buf_start = chunk.timestamp - n / self._sr
                self._primed = True
            self._buf = np.concatenate([self._buf, chunk.samples])
            excess = len(self._buf) - int(self._retain_seconds * self._sr)
            if excess > 0:
                self._buf = self._buf[excess:]
                self._buf_start += excess / self._sr

    # ── worker ────────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._label_ready()
            except Exception as exc:
                print(f'[Diarizer] labelling error: {exc}')
            time.sleep(0.5)

    def _label_ready(self, force: bool = False) -> None:
        cues = self._get_live_cues() or []
        n = len(cues)
        audio_end = self._audio_end()

        while self._cursor < n:
            i = self._cursor
            t0 = cues[i].get('start', 0.0)
            # Grow the utterance while the inter-cue gap stays small.
            j = i + 1
            while j < n and (cues[j].get('start', 0.0)
                             - cues[j - 1].get('end', 0.0)) <= self._gap:
                j += 1
            # Need the following cue (or a forced flush) to confirm the utterance
            # ended; otherwise it may still be growing.
            if j >= n and not force:
                break
            group = cues[i:j]
            t1 = max(c.get('end', 0.0) for c in group)

            # Wait for the settle delay so live has fully committed this span and
            # its audio is buffered.
            if not force and audio_end < t1 + self._settle:
                break

            self._cursor = j
            if t0 < self._buf_start - 0.05:
                # Audio already pruned (we fell behind) — can't embed; skip.
                continue

            samples = self._slice(t0, t1)
            if samples is None or len(samples) < self._min_utt_samples:
                continue
            emb = self._embedder.embed(samples)
            idx = self._clusterer.assign(emb)
            speaker = self._clusterer.label(idx)
            if speaker and self._on_update:
                updates = [{'start': c.get('start'), 'speaker': speaker}
                           for c in group]
                try:
                    self._on_update(updates)
                except Exception as exc:
                    print(f'[Diarizer] update callback failed: {exc}')

    # ── helpers ───────────────────────────────────────────────────────────────

    def _audio_end(self) -> float:
        with self._audio_lock:
            if not self._primed:
                return 0.0
            return self._buf_start + len(self._buf) / self._sr

    def _slice(self, t0: float, t1: float) -> Optional[np.ndarray]:
        a = t0 - self._pad
        b = t1 + self._pad
        with self._audio_lock:
            start = self._buf_start
            buf_end = start + len(self._buf) / self._sr
            a = max(a, start)
            b = min(b, buf_end)
            if b - a <= 0:
                return None
            i0 = max(0, int((a - start) * self._sr))
            i1 = min(len(self._buf), int((b - start) * self._sr))
            return self._buf[i0:i1].copy()

"""Parallel non-speech sound-event captioner.

Runs alongside ``ASRPipeline`` on the *same* PCM stream. A worker thread buffers
short windows, runs the shared AudioSet tagger, debounces with hysteresis, and
emits bracketed captions ([APPLAUSE], [MUSIC], ...) through the same caption
callback the ASR pipeline uses — so they flow into WebVTT, the CEA-608/708
packetizers, and the relay SEI injector identically to dialogue.

Speech is left to ASR: when a speech class dominates a window, non-speech
captions are suppressed (except those that legitimately ride under dialogue,
e.g. background music).
"""

import queue
import threading
import time
from typing import Callable, Dict, Optional

import numpy as np

from ..io.base import AudioChunk
from ..asr.pipeline import CaptionResult
from . import labels as L

CaptionCallback = Callable[[CaptionResult], None]
_SAMPLE_RATE = 16000


class _EventState:
    __slots__ = ("hot", "cold", "active", "last_emit")

    def __init__(self) -> None:
        self.hot = 0          # consecutive windows above on_threshold
        self.cold = 0         # consecutive windows below off_threshold
        self.active = False   # currently considered "on screen"
        self.last_emit = 0.0  # monotonic time of last emission


class SoundEventDetector:
    """Sliding-window AudioSet tagger → debounced bracketed captions."""

    def __init__(self, config: dict, shared_tagger,
                 worker_name: str = "sed-worker") -> None:
        c = config or {}
        self._tagger = shared_tagger
        self._worker_name = worker_name
        self._sr = _SAMPLE_RATE
        self._window = int(c.get("window_sec", 2.0) * self._sr)
        self._hop = int(c.get("hop_sec", 1.0) * self._sr)
        self._on = float(c.get("on_threshold", 0.45))
        self._off = float(c.get("off_threshold", 0.25))
        self._min_hot = int(c.get("min_hot_windows", 1))
        self._clear_windows = int(c.get("clear_windows", 2))
        self._cooldown = float(c.get("cooldown_sec", 8.0))
        self._speech_gate = float(c.get("speech_gate", 0.5))
        self._max_per_window = int(c.get("max_captions_per_window", 1))

        self._buf = np.zeros(0, dtype=np.float32)
        self._since_hop = 0          # new samples since last classification
        self._clock = 0.0            # timeline secs at end of buffer
        self._states: Dict[str, _EventState] = {}

        self._audio_q: "queue.Queue[AudioChunk]" = queue.Queue(maxsize=100)
        self._caption_cb: Optional[CaptionCallback] = None
        self._running = False
        self._worker: Optional[threading.Thread] = None

    # ── public API (mirrors ASRPipeline) ─────────────────────────────────────
    def set_caption_callback(self, cb: CaptionCallback) -> None:
        self._caption_cb = cb

    def start(self) -> None:
        if self._tagger is None:
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._loop, daemon=True, name=self._worker_name)
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        if self._worker:
            self._worker.join(timeout=4.0)

    def on_audio(self, chunk: AudioChunk) -> None:
        """Called from the adapter thread — must be non-blocking."""
        try:
            self._audio_q.put_nowait(chunk)
        except queue.Full:
            pass

    # ── worker ───────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while self._running:
            try:
                chunk = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._ingest(chunk)
            while self._since_hop >= self._hop and self._running:
                self._since_hop -= self._hop
                try:
                    self._classify_window()
                except Exception as exc:  # never take down the stream
                    print(f"[AudioEvents] classify error: {exc}")

    def _ingest(self, chunk: AudioChunk) -> None:
        s = chunk.samples
        if s.dtype != np.float32:
            s = s.astype(np.float32)
        self._buf = np.concatenate((self._buf, s))
        self._since_hop += len(s)
        self._clock = chunk.timestamp + len(s) / self._sr
        # keep only what we need for the next window
        if len(self._buf) > self._window:
            self._buf = self._buf[-self._window:]

    def _classify_window(self) -> None:
        if len(self._buf) < self._hop:
            return
        probs = self._tagger.classify(self._buf.copy())
        if not probs:
            return

        speech = max((probs.get(s, 0.0) for s in L.SPEECH_LABELS), default=0.0)
        speech_dominant = speech >= self._speech_gate

        # Collapse AudioSet classes onto their caption text, keeping the max prob.
        scored: Dict[str, float] = {}
        for cls, cap in L.CAPTION_MAP.items():
            p = probs.get(cls, 0.0)
            if p > scored.get(cap, 0.0):
                scored[cap] = p

        now = time.monotonic()
        fired = []  # (prob, caption_text)
        for cap, p in scored.items():
            st = self._states.setdefault(cap, _EventState())
            if p >= self._on:
                st.hot += 1
                st.cold = 0
            elif p < self._off:
                st.cold += 1
                st.hot = 0
                if st.active and st.cold >= self._clear_windows:
                    st.active = False
                continue
            else:
                continue  # in the hysteresis dead-band; hold state

            gated = speech_dominant and cap not in L.COEXISTS_WITH_SPEECH
            ready = st.hot >= self._min_hot and not st.active
            cooled = (now - st.last_emit) >= self._cooldown
            if ready and cooled and not gated:
                fired.append((p, cap))

        if not fired:
            return
        fired.sort(reverse=True)
        for p, cap in fired[: self._max_per_window]:
            st = self._states[cap]
            st.active = True
            st.last_emit = now
            self._emit(f"[{cap}]")

    def _emit(self, text: str) -> None:
        cb = self._caption_cb
        if cb is None:
            return
        start = max(0.0, self._clock - self._window / self._sr)
        cb(CaptionResult(text=text, start_time=start, end_time=self._clock,
                         backend="audioset"))

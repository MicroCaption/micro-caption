import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .vad import EnergyVAD
from ..io.base import AudioChunk
from ..monitor.latency import LatencyMonitor


@dataclass
class CaptionResult:
    text: str
    start_time: float
    end_time: float
    confidence: float = 1.0
    backend: str = 'unknown'


CaptionCallback = Callable[[CaptionResult], None]

_SAMPLE_RATE = 16000


class SharedASRBackend:
    """
    Thread-safe model wrapper loaded once and shared across per-session
    ASRPipeline instances.  A single threading.Lock serialises GPU access
    so multiple sessions can't race into the same inference call.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._backend = None
        self.name = 'unloaded'

    def load(self) -> None:
        primary_name = self._config.get('primary', 'parakeet')
        if primary_name == 'parakeet':
            try:
                from .parakeet_backend import ParakeetBackend
                b = ParakeetBackend(self._config.get('parakeet', {}))
                b.load()
                self._backend = b
                self.name = b.name
                return
            except Exception as exc:
                print(f'[ASR] Parakeet failed ({exc}), falling back to Whisper')
        from .whisper_backend import WhisperBackend
        b = WhisperBackend(self._config.get('whisper', {}))
        b.load()
        self._backend = b
        self.name = b.name

    def transcribe(self, samples: np.ndarray) -> str:
        with self._lock:
            if self._backend is None:
                raise RuntimeError('SharedASRBackend not loaded')
            return self._backend.transcribe(samples)

    def unload(self) -> None:
        with self._lock:
            if self._backend is not None:
                if hasattr(self._backend, 'unload'):
                    self._backend.unload()
                self._backend = None
        self.name = 'unloaded'


class ASRPipeline:
    """
    Sliding-window ASR pipeline.

    Audio arrives via on_audio() from the I/O adapter thread.
    A background worker accumulates samples, applies VAD, then runs
    inference on each chunk_duration window stepped by step_duration.
    Results are emitted via the caption callback on the worker thread.

    Fallback: if the primary backend raises, WhisperBackend is loaded
    lazily and tried once per chunk — no crash, no dropped frames.
    """

    def __init__(self, config: dict, shared_backend=None,
                 worker_name: str = 'asr-worker') -> None:
        self._config = config
        self._shared_backend: Optional[SharedASRBackend] = shared_backend
        self._worker_name = worker_name
        self._sr = _SAMPLE_RATE
        self._chunk_samples = int(config.get('chunk_duration', 2.0) * self._sr)
        self._step_samples = int(config.get('step_duration', 0.5) * self._sr)
        self._max_latency_ms: float = config.get('max_latency_ms', 2000.0)

        self._vad = EnergyVAD(
            rms_threshold=config.get('vad_rms_threshold', 1e-4),
            hangover_frames=config.get('vad_hangover_frames', 5),
        )
        self._latency = LatencyMonitor(window=config.get('latency_window', 50))

        self._audio_q: queue.Queue[AudioChunk] = queue.Queue(maxsize=100)
        self._caption_cb: Optional[CaptionCallback] = None

        self._primary = None
        self._fallback = None
        self._last_text = ''
        self._running = False
        self._worker: Optional[threading.Thread] = None

    # ── public API ──────────────────────────────────────────────────────────

    def set_caption_callback(self, cb: CaptionCallback) -> None:
        self._caption_cb = cb

    def start(self) -> None:
        if self._shared_backend is not None:
            self._primary = self._shared_backend
        else:
            self._load_primary()
        self._running = True
        self._worker = threading.Thread(
            target=self._loop, daemon=True, name=self._worker_name,
        )
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        if self._worker:
            self._worker.join(timeout=6.0)

    def on_audio(self, chunk: AudioChunk) -> None:
        """Called from GStreamer's main-loop thread — must be non-blocking."""
        try:
            self._audio_q.put_nowait(chunk)
        except queue.Full:
            pass  # prefer low latency over completeness; drop oldest indirectly

    @property
    def latency_monitor(self) -> LatencyMonitor:
        return self._latency

    # ── internal ────────────────────────────────────────────────────────────

    def _load_primary(self) -> None:
        primary_name = self._config.get('primary', 'parakeet')
        if primary_name == 'parakeet':
            try:
                from .parakeet_backend import ParakeetBackend
                self._primary = ParakeetBackend(self._config.get('parakeet', {}))
                self._primary.load()
                return
            except Exception as exc:
                print(f'[ASR] Parakeet failed to load ({exc}), switching to Whisper as primary')

        # Whisper as primary (either requested or Parakeet failed)
        from .whisper_backend import WhisperBackend
        self._primary = WhisperBackend(self._config.get('whisper', {}))
        self._primary.load()

    def _load_fallback(self) -> None:
        if self._fallback is not None:
            return
        try:
            from .whisper_backend import WhisperBackend
            self._fallback = WhisperBackend(self._config.get('whisper', {}))
            self._fallback.load()
            print('[ASR] Fallback (Whisper) loaded')
        except Exception as exc:
            print(f'[ASR] Fallback load failed: {exc}')

    def _loop(self) -> None:
        buf = np.array([], dtype=np.float32)
        last_chunk_ts = 0.0

        while self._running:
            try:
                chunk = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                continue

            buf = np.concatenate([buf, chunk.samples])
            last_chunk_ts = chunk.timestamp

            while len(buf) >= self._chunk_samples:
                window = buf[:self._chunk_samples]
                buf = buf[self._step_samples:]

                vad = self._vad.process(window)
                if not vad.is_speech:
                    continue

                t0 = time.monotonic()
                text = self._infer(window)
                latency_ms = (time.monotonic() - t0) * 1000.0
                self._latency.record(latency_ms)

                if latency_ms > self._max_latency_ms:
                    print(f'[ASR] ⚠ latency {latency_ms:.0f} ms > {self._max_latency_ms:.0f} ms target')

                text = text.strip()
                if not text or text == self._last_text:
                    continue

                self._last_text = text
                result = CaptionResult(
                    text=text,
                    start_time=last_chunk_ts - len(window) / self._sr,
                    end_time=last_chunk_ts,
                    backend=getattr(self._primary, 'name', 'unknown'),
                )
                if self._caption_cb:
                    self._caption_cb(result)

    def _infer(self, samples: np.ndarray) -> str:
        try:
            return self._primary.transcribe(samples)
        except Exception as exc:
            print(f'[ASR] Primary error: {exc}')
            if self._shared_backend is not None:
                # Shared backend already handles parakeet→whisper fallback at load time.
                # Don't try a per-session fallback — it would load a second model.
                return ''
            print('[ASR] Trying fallback')
            self._load_fallback()
            if self._fallback:
                try:
                    return self._fallback.transcribe(samples)
                except Exception as exc2:
                    print(f'[ASR] Fallback error: {exc2}')
            return ''

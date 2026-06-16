import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

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

# A timestamped word: (start, end, text) with absolute timeline seconds.
Word = Tuple[float, float, str]


def _norm_word(w: str) -> str:
    """Normalise a word for agreement matching (case/space-insensitive)."""
    return w.strip().lower()


class HypothesisBuffer:
    """
    LocalAgreement-2 hypothesis buffer (Macháček et al., "whisper_streaming").

    Streaming Whisper re-transcribes an overlapping audio buffer each step, so
    the tail of every hypothesis is unstable. LocalAgreement-2 commits a word
    only once *two consecutive* hypotheses agree on it; committed words are
    emitted exactly once and never revised — giving the append-only, jitter-free
    stream the client paginates and paces.

    Words are (start, end, text) tuples with absolute timestamps.
    """

    def __init__(self) -> None:
        self.committed_in_buffer: List[Word] = []
        self.buffer: List[Word] = []          # last hypothesis, not yet committed
        self.new: List[Word] = []
        self.last_committed_time: float = 0.0

    def insert(self, words: List[Tuple[str, float, float]], offset: float) -> None:
        """Feed a fresh hypothesis (text, start, end) relative to buffer start."""
        shifted: List[Word] = [(s + offset, e + offset, t) for (t, s, e) in words]
        # Drop anything at or before the commit point — it's already final or
        # is committed context still present in the audio buffer.
        self.new = [w for w in shifted if w[0] > self.last_committed_time - 0.1]
        if not self.new:
            return
        a0 = self.new[0][0]
        if self.committed_in_buffer and abs(a0 - self.last_committed_time) < 1.0:
            # n-gram guard: if the first new words repeat the last committed
            # words (boundary timestamp jitter), drop the duplicated prefix.
            cn, nn = len(self.committed_in_buffer), len(self.new)
            for i in range(1, min(cn, nn, 5) + 1):
                committed_tail = ' '.join(
                    _norm_word(self.committed_in_buffer[-j][2]) for j in range(i, 0, -1))
                new_head = ' '.join(_norm_word(self.new[j][2]) for j in range(i))
                if committed_tail == new_head:
                    del self.new[:i]
                    break

    def flush(self, commit_before: Optional[float] = None) -> List[Word]:
        """
        Commit the longest common prefix of the new and previous hypotheses.

        If commit_before is given, words ending after it are held back even when
        they already agree — leaving the most recent audio as look-ahead context
        so the model can still revise the tail before it's released for display.
        """
        commit: List[Word] = []
        while self.new and self.buffer:
            if commit_before is not None and self.new[0][1] > commit_before:
                break
            if _norm_word(self.new[0][2]) == _norm_word(self.buffer[0][2]):
                commit.append(self.new[0])
                self.last_committed_time = self.new[0][1]
                self.new.pop(0)
                self.buffer.pop(0)
            else:
                break
        self.buffer = self.new
        self.new = []
        self.committed_in_buffer.extend(commit)
        # Bound memory — only the last few committed words feed the n-gram guard.
        if len(self.committed_in_buffer) > 50:
            self.committed_in_buffer = self.committed_in_buffer[-50:]
        return commit

    def flush_final(self) -> List[Word]:
        """Force-commit the remaining uncommitted tail (utterance end / overflow)."""
        commit = list(self.buffer)
        if commit:
            self.last_committed_time = commit[-1][1]
            self.committed_in_buffer.extend(commit)
            if len(self.committed_in_buffer) > 50:
                self.committed_in_buffer = self.committed_in_buffer[-50:]
        self.buffer = []
        return commit


class SharedASRBackend:
    """
    Supervised dual-backend, loaded once and shared across per-session
    ASRPipeline instances. A single threading.Lock serialises GPU access so
    sessions can't race into the same inference call.

    Parakeet is the primary engine; Whisper is a hot standby (preloaded by
    default). If Parakeet raises, the call transparently fails over to Whisper
    — the SAME call is retried so no caption is dropped — and a background
    thread keeps trying to reload Parakeet, switching back as soon as it
    recovers. Inference calls NEVER raise (they return safe-empty on a double
    failure) so the ASR worker thread can't die mid-stream.

    Both backends expose word timestamps, so ``supports_words`` is always True
    and the pipeline stays in streaming mode across a failover.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._lock = threading.Lock()          # serialises GPU access
        self._state_lock = threading.Lock()    # guards _active pointer + flags
        self._primary_is_parakeet = config.get('primary', 'parakeet') == 'parakeet'
        self._preload_fallback = bool(config.get('preload_fallback', True))
        self._recovery_interval = float(
            config.get('parakeet', {}).get('recovery_interval_sec', 8.0))
        self._parakeet = None
        self._whisper = None
        self._active = None
        self._recovering = False
        self._running = True
        self.name = 'unloaded'

    # ── loading ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        # Preload Whisper so failover is instant (and it's the engine when
        # primary == whisper). Skipping the preload only delays the first
        # failover by one model load.
        if self._preload_fallback or not self._primary_is_parakeet:
            self._whisper = self._load_whisper()

        if self._primary_is_parakeet:
            try:
                self._parakeet = self._load_parakeet()
                self._active = self._parakeet
            except Exception as exc:
                print(f'[ASR] Parakeet failed to load ({exc}); using Whisper, will keep retrying')
                if self._whisper is None:
                    self._whisper = self._load_whisper()
                self._active = self._whisper
                self._start_recovery()
        else:
            self._active = self._whisper
        self.name = self._active.name

    def _load_parakeet(self):
        from .parakeet_backend import ParakeetBackend
        b = ParakeetBackend(self._config.get('parakeet', {}))
        b.load()
        return b

    def _load_whisper(self):
        from .whisper_backend import WhisperBackend
        b = WhisperBackend(self._config.get('whisper', {}))
        b.load()
        return b

    # ── public inference API ───────────────────────────────────────────────--

    @property
    def supports_words(self) -> bool:
        # Both backends support words; keep True so the pipeline never has to
        # switch inference mode when failing over.
        return True

    @property
    def active_name(self) -> str:
        a = self._active
        return getattr(a, 'name', 'unloaded')

    def transcribe(self, samples: np.ndarray) -> str:
        return self._call('transcribe', samples, '')

    def transcribe_words(self, samples: np.ndarray) -> list:
        """Word-level transcription, serialised through the shared GPU lock."""
        return self._call('transcribe_words', samples, [])

    def _call(self, method: str, samples: np.ndarray, empty):
        backend = self._active
        if backend is None:
            raise RuntimeError('SharedASRBackend not loaded')
        try:
            with self._lock:
                return getattr(backend, method)(samples)
        except Exception as exc:
            if backend is self._parakeet:
                # Primary failed — fail over to Whisper and retry this call so
                # the caption isn't lost, then start trying to recover Parakeet.
                print(f'[ASR] Parakeet error ({exc}); failing over to Whisper')
                self._failover()
                wb = self._whisper
                if wb is not None:
                    try:
                        with self._lock:
                            return getattr(wb, method)(samples)
                    except Exception as exc2:
                        print(f'[ASR] Whisper fallback error: {exc2}')
                return empty
            # Active is Whisper (or unknown) — don't crash the worker thread.
            print(f'[ASR] {getattr(backend, "name", "?")} error: {exc}')
            return empty

    # ── failover + recovery ──────────────────────────────────────────────────

    def _failover(self) -> None:
        with self._state_lock:
            if self._whisper is None:
                self._whisper = self._load_whisper()   # lazy (preload was off)
            self._active = self._whisper
            self.name = self._whisper.name
        self._start_recovery()

    def _start_recovery(self) -> None:
        with self._state_lock:
            if self._recovering:
                return
            self._recovering = True
        threading.Thread(target=self._recovery_loop, daemon=True,
                         name='asr-parakeet-recovery').start()

    def _recovery_loop(self) -> None:
        # Reload Parakeet out-of-band and switch back as soon as a smoke-test
        # inference succeeds. Loading happens outside the GPU lock (it's heavy);
        # only the smoke test holds the lock briefly.
        while self._running:
            time.sleep(self._recovery_interval)
            if not self._running:
                break
            try:
                fresh = self._load_parakeet()
                with self._lock:
                    fresh.transcribe_words(np.zeros(_SAMPLE_RATE // 2, dtype=np.float32))
            except Exception as exc:
                print(f'[ASR] Parakeet recovery attempt failed ({exc}); will retry')
                continue
            with self._state_lock:
                self._parakeet = fresh
                self._active = fresh
                self.name = fresh.name
                self._recovering = False
            print('[ASR] Parakeet recovered — switched back from Whisper')
            return

    def unload(self) -> None:
        self._running = False
        with self._lock:
            for b in (self._parakeet, self._whisper):
                if b is not None and hasattr(b, 'unload'):
                    b.unload()
            self._parakeet = self._whisper = self._active = None
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
        # Streaming (LocalAgreement) loop parameters — used when the backend
        # exposes word timestamps. chunk_duration is reused only for the VAD
        # silence window; the inference buffer grows and is trimmed dynamically.
        self._min_chunk_samples = int(config.get('min_chunk_duration', 1.0) * self._sr)
        self._max_buffer_samples = int(config.get('max_buffer_duration', 14.0) * self._sr)
        self._context_duration: float = config.get('context_duration', 2.0)
        # Accuracy-over-speed: hold the most recent review_delay seconds of audio
        # as look-ahead context only — words there are re-reviewed against the
        # incoming audio and not released for display until they age out of it.
        self._review_delay: float = config.get('review_delay', 2.5)
        self._last_word: str = ''   # for cross-fragment de-duplication

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
        """Dispatch to the streaming loop when word timestamps are available."""
        if getattr(self._primary, 'supports_words', False):
            self._loop_streaming()
        else:
            self._loop_legacy()

    def _emit(self, committed: List[Word]) -> None:
        from ..caption.corrector import correct_fragment, last_word
        raw = ''.join(w[2] for w in committed)
        text = correct_fragment(raw, self._last_word)
        if not text:
            return
        self._last_word = last_word(text)
        result = CaptionResult(
            text=text,
            start_time=committed[0][0],
            end_time=committed[-1][1],
            backend=getattr(self._primary, 'name', 'unknown'),
        )
        if self._caption_cb:
            self._caption_cb(result)

    def _loop_streaming(self) -> None:
        """
        Growing-buffer LocalAgreement-2 loop with a look-ahead review window.

        Each step, re-transcribe the (trimmed) audio buffer with word
        timestamps and commit only words that two consecutive hypotheses agree
        on AND that are older than review_delay seconds — so the most recent
        speech stays a revisable, look-ahead context until the model has heard
        enough to finalise it accurately. Emit each newly committed run as an
        append-only fragment (after a deterministic clean-up pass). Trim the
        buffer to a short left-context after every commit so GPU cost stays
        bounded; force-commit the tail on a VAD silence gap or buffer overflow.
        """
        sr = self._sr
        buf = np.array([], dtype=np.float32)
        abs_time = 0.0          # absolute stream time at the end of buf
        pending = 0             # new samples since the last inference
        had_speech = False      # speech seen since the last utterance flush
        hyp = HypothesisBuffer()

        while self._running:
            try:
                chunk = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                continue

            buf = np.concatenate([buf, chunk.samples])
            abs_time = chunk.timestamp
            pending += len(chunk.samples)

            if pending < self._step_samples or len(buf) < self._min_chunk_samples:
                continue
            pending = 0

            is_speech = self._vad.process(buf[-self._chunk_samples:]).is_speech
            if is_speech:
                had_speech = True

            if not had_speech:
                # Idle silence — don't burn GPU on it; keep the buffer short.
                if len(buf) > self._min_chunk_samples:
                    buf = buf[-self._min_chunk_samples:]
                continue

            # buf is always the most-recent samples ending at abs_time, so the
            # time at buf[0] is simply abs_time - len(buf)/sr.
            offset = abs_time - len(buf) / sr

            t0 = time.monotonic()
            words = self._primary.transcribe_words(buf)
            latency_ms = (time.monotonic() - t0) * 1000.0
            self._latency.record(latency_ms)
            if latency_ms > self._max_latency_ms:
                print(f'[ASR] ⚠ latency {latency_ms:.0f} ms > {self._max_latency_ms:.0f} ms target')

            hyp.insert(words, offset)
            # Hold the most recent review_delay seconds back as look-ahead
            # context so the tail keeps getting re-reviewed before display.
            committed = hyp.flush(commit_before=abs_time - self._review_delay)

            utterance_end = had_speech and not is_speech
            overflow = len(buf) >= self._max_buffer_samples
            if utterance_end or overflow:
                committed = committed + hyp.flush_final()

            if committed:
                self._emit(committed)

            if utterance_end:
                # Utterance finished — reset and drop the committed/silent audio.
                hyp = HypothesisBuffer()
                had_speech = False
                self._last_word = ''   # don't de-dup across an utterance boundary
                buf = buf[-self._min_chunk_samples:]
            else:
                # Keep only _context_duration seconds of committed audio as
                # left-context; discard the rest to bound inference cost.
                keep_from = hyp.last_committed_time - self._context_duration
                cut = int((keep_from - offset) * sr)
                if cut > 0:
                    buf = buf[cut:]

    def _loop_legacy(self) -> None:
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

"""
AccuracyVerifier — a second, slower ASR pass for quality measurement.

The live pipeline favours latency (single-pass decode, short look-ahead). This
verifier re-transcribes the *same* audio behind live at high accuracy (a wider
beam) and treats that as the reference transcript, then scores the live captions
against it (WER → accuracy %).

To keep the two sides comparable, the **live captions define each segment**: we
group consecutive live cues into ~segment_seconds spans, then slice the verifier's
retained audio to that exact span and transcribe it for the reference. Both
columns therefore cover the identical [t0, t1] and the identical set of live
words — no timestamp-drift phase offset between reference and live.

It shares the one loaded GPU model via SharedASRBackend (serialised by the same
lock as the live path), so no second model is loaded. It runs behind live and
never blocks the live pipeline.
"""

import threading
import time
from typing import Callable, List, Optional

import numpy as np

from ..io.base import AudioChunk
from ..text.wer import compute_wer_detailed, tokenize

_SAMPLE_RATE = 16000

# record dict, summary dict → None
RecordCallback = Callable[[dict, dict], None]


class AccuracyVerifier:
    def __init__(self, backend, config: dict,
                 get_live_cues: Callable[[], List[dict]],
                 on_record: Optional[RecordCallback] = None,
                 worker_name: str = 'verifier') -> None:
        self._backend = backend
        self._get_live_cues = get_live_cues
        self._on_record = on_record
        self._worker_name = worker_name
        self._sr = _SAMPLE_RATE

        vcfg = config.get('verifier', {})
        self._beam_size: int = int(vcfg.get('beam_size', 5))
        self._segment_seconds: float = float(vcfg.get('segment_seconds', 8.0))
        self._pad_seconds: float = float(vcfg.get('context_seconds', 1.0))
        # Headline accuracy aligns the whole reference vs the whole live transcript
        # over this rolling span — so any residual per-segment boundary effects
        # cancel out and the % reflects true word accuracy.
        self._headline_seconds: float = float(vcfg.get('headline_seconds', 600.0))
        # Keep enough audio buffered to re-transcribe a segment after live (which
        # lags by its review delay) has committed the cues that define it.
        self._retain_seconds: float = max(
            60.0, self._segment_seconds * 2 + 30.0)

        # Rolling audio buffer with an absolute-time index.
        self._audio_lock = threading.Lock()
        self._audio_buf = np.array([], dtype=np.float32)
        self._audio_start = 0.0          # absolute stream time at buf[0]
        self._audio_primed = False

        self._lock = threading.Lock()
        self._cue_cursor = 0             # index of the next un-scored live cue
        self._scored: list = []          # [{'start','end','reference'}], recent-pruned
        self._segments = 0               # lifetime count of scored segments
        self._latest_end = 0.0
        self._skipped_segments = 0       # segments whose audio had already been pruned

        self._running = False
        self._worker: Optional[threading.Thread] = None

    # ── public API ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._worker = threading.Thread(
            target=self._loop, daemon=True, name=self._worker_name)
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        if self._worker:
            self._worker.join(timeout=8.0)

    def on_audio(self, chunk: AudioChunk) -> None:
        """Append audio to the rolling buffer (cheap; called from the adapter
        thread). Old audio is pruned once it's well past any segment we'd still
        score, so memory stays bounded."""
        n = len(chunk.samples)
        with self._audio_lock:
            if not self._audio_primed:
                self._audio_start = chunk.timestamp - n / self._sr
                self._audio_primed = True
            self._audio_buf = np.concatenate([self._audio_buf, chunk.samples])
            # Prune the front beyond the retain window.
            excess = len(self._audio_buf) - int(self._retain_seconds * self._sr)
            if excess > 0:
                self._audio_buf = self._audio_buf[excess:]
                self._audio_start += excess / self._sr

    @property
    def summary(self) -> dict:
        return self._build_summary()

    # ── worker ───────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._score_ready_segments()
            except Exception as exc:
                print(f'[Verifier] scoring error: {exc}')
            time.sleep(0.3)
        # On shutdown, force-close any trailing segment.
        try:
            self._score_ready_segments(force=True)
        except Exception as exc:
            print(f'[Verifier] final scoring error: {exc}')

    def _score_ready_segments(self, force: bool = False) -> None:
        cues = self._get_live_cues() or []
        n = len(cues)
        audio_end = self._audio_end()

        while self._cue_cursor < n:
            i = self._cue_cursor
            t0 = cues[i].get('start', 0.0)
            # Grow the group until it spans ~segment_seconds.
            j = i
            while j < n and (cues[j].get('end', 0.0) - t0) < self._segment_seconds:
                j += 1
            # Need a following cue to confirm the boundary is final (live is
            # append-only by increasing start) — unless we're flushing on shutdown.
            if j >= n and not force:
                break
            group = cues[i:j] if j > i else cues[i:i + 1]
            t1 = max(c.get('end', 0.0) for c in group)

            # The audio defining this span must be buffered (and not yet pruned).
            if not force and audio_end < t1 + 0.1:
                break
            if t0 < self._audio_start - 0.05:
                # Audio already pruned (verifier fell far behind) — skip cleanly.
                with self._lock:
                    self._skipped_segments += 1
                self._cue_cursor = j if j > i else i + 1
                continue

            reference = self._reference_for(t0, t1)
            hypothesis = ' '.join(
                c['text'].replace('\n', ' ').strip() for c in group if c.get('text'))
            self._cue_cursor = j if j > i else i + 1
            if reference.strip():
                self._score_segment(reference, hypothesis, t0, t1)

    # ── reference transcription ───────────────────────────────────────────────

    def _reference_for(self, t0: float, t1: float) -> str:
        """High-accuracy transcription of the audio in [t0, t1].

        A little context padding is decoded on each side for quality, then the
        reference is trimmed back to words that begin within [t0, t1) so it lines
        up exactly with the live cues for the same span."""
        a = t0 - self._pad_seconds
        b = t1 + self._pad_seconds
        with self._audio_lock:
            start = self._audio_start
            buf_end = start + len(self._audio_buf) / self._sr
            a = max(a, start)
            b = min(b, buf_end)
            if b - a <= 0:
                return ''
            i0 = max(0, int((a - start) * self._sr))
            i1 = min(len(self._audio_buf), int((b - start) * self._sr))
            samples = self._audio_buf[i0:i1].copy()
        slice_start = a
        return self._transcribe_slice(samples, slice_start, t0, t1)

    def _transcribe_slice(self, samples: np.ndarray, slice_start: float,
                          t0: float, t1: float) -> str:
        try:
            if getattr(self._backend, 'supports_words', False):
                # transcribe_words → (word_text, start, end); text is [0], start [1].
                words = self._backend.transcribe_words(samples, beam_size=self._beam_size)
                kept = [w[0] for w in words if t0 <= slice_start + w[1] < t1]
                return ''.join(kept)
            return self._backend.transcribe(samples)
        except Exception as exc:
            print(f'[Verifier] reference transcription failed: {exc}')
            return ''

    # ── scoring ───────────────────────────────────────────────────────────────

    def _score_segment(self, reference: str, hypothesis: str,
                       t0: float, t1: float) -> None:
        reference = reference.strip()
        if not reference:
            return
        if hypothesis.strip():
            d = compute_wer_detailed(reference, hypothesis)
            local_acc = max(0.0, 1.0 - d['wer'])
        else:
            local_acc = 0.0
            d = {'wer': 1.0, 'substitutions': 0,
                 'deletions': len(tokenize(reference)), 'insertions': 0}

        with self._lock:
            self._segments += 1
            self._latest_end = max(self._latest_end, t1)
            self._scored.append({'start': t0, 'end': t1, 'reference': reference})
            cutoff = self._latest_end - self._headline_seconds * 1.5
            if self._scored and self._scored[0]['end'] < cutoff:
                self._scored = [s for s in self._scored if s['end'] >= cutoff]

        record = {
            'start': round(t0, 2),
            'end': round(t1, 2),
            'reference': reference,
            'hypothesis': hypothesis.strip(),
            'wer': round(d['wer'], 4),
            'accuracy': round(local_acc, 4),
            'substitutions': d['substitutions'],
            'deletions': d['deletions'],
            'insertions': d['insertions'],
        }
        if self._on_record:
            try:
                self._on_record(record, self._build_summary())
            except Exception as exc:
                print(f'[Verifier] record callback failed: {exc}')

    # ── helpers ───────────────────────────────────────────────────────────────

    def _audio_end(self) -> float:
        with self._audio_lock:
            if not self._audio_primed:
                return 0.0
            return self._audio_start + len(self._audio_buf) / self._sr

    def _build_summary(self) -> dict:
        """Headline accuracy from a single alignment of the whole reference vs the
        whole live transcript over a rolling span — boundary effects of the
        per-segment slices cancel out, so this reflects true word accuracy."""
        with self._lock:
            segments = self._segments
            latest = self._latest_end
            skipped = self._skipped_segments
            cutoff = latest - self._headline_seconds
            refs = [s['reference'] for s in self._scored if s['end'] >= cutoff]

        reference = ' '.join(refs).strip()
        try:
            cues = self._get_live_cues() or []
        except Exception:
            cues = []
        live = ' '.join(c['text'].replace('\n', ' ').strip()
                        for c in cues if c.get('start', 0) >= cutoff and c.get('text'))

        d = None
        if reference:
            try:
                d = compute_wer_detailed(reference, live)
            except ValueError:
                d = None

        if d is None:
            return {'segment_count': segments, 'ref_words': 0, 'substitutions': 0,
                    'deletions': 0, 'insertions': 0, 'wer': 0.0, 'accuracy': None,
                    'skipped_segments': skipped}

        wer = d['wer']
        return {
            'segment_count': segments,
            'ref_words': d['ref_words'],
            'substitutions': d['substitutions'],
            'deletions': d['deletions'],
            'insertions': d['insertions'],
            'wer': round(wer, 4),
            'accuracy': round(max(0.0, 1.0 - wer), 4),
            'skipped_segments': skipped,
        }

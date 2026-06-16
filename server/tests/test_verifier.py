"""
Unit tests for AccuracyVerifier (no GPU / models).

Exercises the pure scoring, the live-cue-driven segmentation, the audio-slice
reference trimming, and the global headline summary. Run with:
  python -m pytest tests/test_verifier.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest
import numpy as np

from microcaption.accuracy.verifier import AccuracyVerifier


class FakeBackend:
    """Text-only backend: returns a fixed transcript regardless of audio."""
    supports_words = False

    def __init__(self, text):
        self._text = text

    def transcribe(self, samples):
        return self._text


class FakeWordBackend:
    """Word-timestamp backend, mirroring WhisperBackend.transcribe_words:
    returns (word_text, start, end) tuples (leading spaces preserved), with one
    word per second starting at t=0."""
    supports_words = True

    def __init__(self, text):
        self._text = text

    def transcribe_words(self, samples, beam_size=1):
        words = self._text.split()
        return [(('' if i == 0 else ' ') + w, float(i), float(i) + 0.5)
                for i, w in enumerate(words)]


def _make(backend_text, cues):
    records = []
    v = AccuracyVerifier(
        FakeBackend(backend_text),
        {'verifier': {}},
        get_live_cues=lambda: cues,
        on_record=lambda rec, summ: records.append((rec, summ)),
    )
    return v, records


class TestScoring(unittest.TestCase):

    def test_perfect_match(self):
        v, recs = _make('x', [{'start': 0.0, 'end': 5.0, 'text': 'the quick brown fox'}])
        v._score_segment('the quick brown fox', 'the quick brown fox', 0.0, 5.0)
        self.assertEqual(len(recs), 1)
        rec, summ = recs[0]
        self.assertAlmostEqual(rec['accuracy'], 1.0)
        self.assertAlmostEqual(summ['accuracy'], 1.0)
        self.assertEqual(summ['segment_count'], 1)

    def test_live_miss_scores_zero(self):
        v, recs = _make('x', [])
        v._score_segment('the quick brown fox', '', 0.0, 5.0)
        rec, _ = recs[0]
        self.assertAlmostEqual(rec['accuracy'], 0.0)
        self.assertEqual(rec['deletions'], 4)

    def test_empty_reference_emits_no_record(self):
        v, recs = _make('x', [{'start': 0, 'end': 5, 'text': 'anything'}])
        v._score_segment('   ', 'anything', 0.0, 5.0)
        self.assertEqual(recs, [])

    def test_headline_aligns_whole_transcript(self):
        # Two segments; the live transcript matches the references exactly when
        # joined, so the headline is perfect even if a per-segment slice differed.
        cues = [{'start': 0.0, 'end': 5.0, 'text': 'the cat sat'},
                {'start': 5.0, 'end': 10.0, 'text': 'on the mat'}]
        v, recs = _make('x', cues)
        v._score_segment('the cat sat', 'the cat sat', 0.0, 5.0)
        v._score_segment('on the mat', 'on the mat', 5.0, 10.0)
        _, summ = recs[-1]
        self.assertEqual(summ['segment_count'], 2)
        self.assertEqual(summ['ref_words'], 6)
        self.assertAlmostEqual(summ['accuracy'], 1.0)


class TestSegmentation(unittest.TestCase):

    def test_segments_grouped_by_live_cues(self):
        # 3s cues, 8s target → groups of consecutive cues; each record's span and
        # hypothesis come straight from the grouped live cues (so REF/LIVE align).
        cues = [
            {'start': 0.0, 'end': 3.0, 'text': 'a a a'},
            {'start': 3.0, 'end': 6.0, 'text': 'b b b'},
            {'start': 6.0, 'end': 9.0, 'text': 'c c c'},
        ]
        v, recs = _make('x', cues)
        v._reference_for = lambda t0, t1: 'ref'      # stub out audio/transcription
        v._score_ready_segments(force=True)
        hyps = [r['hypothesis'] for r, _ in recs]
        spans = [(r['start'], r['end']) for r, _ in recs]
        self.assertEqual(hyps, ['a a a b b b', 'c c c'])
        self.assertEqual(spans, [(0.0, 6.0), (6.0, 9.0)])

    def test_open_final_segment_waits_for_successor(self):
        # Without a following cue, a not-yet-closed tail segment isn't scored
        # until force (shutdown).
        cues = [{'start': 0.0, 'end': 9.0, 'text': 'lonely segment here now'}]
        v, recs = _make('x', cues)
        v._reference_for = lambda t0, t1: 'ref'
        v._score_ready_segments(force=False)
        self.assertEqual(recs, [])
        v._score_ready_segments(force=True)
        self.assertEqual(len(recs), 1)


class TestReferenceSlice(unittest.TestCase):

    def test_trims_reference_words_to_span(self):
        # Words begin at t=0,1,2,3 (slice_start=0); keep those starting in [2,4).
        v = AccuracyVerifier(
            FakeWordBackend('the quick brown fox'), {'verifier': {}},
            get_live_cues=lambda: [], on_record=lambda r, s: None)
        full = v._transcribe_slice(np.zeros(64000, dtype=np.float32), 0.0, 0.0, 4.0)
        self.assertEqual(full.strip(), 'the quick brown fox')
        trimmed = v._transcribe_slice(np.zeros(64000, dtype=np.float32), 0.0, 2.0, 4.0)
        self.assertEqual(trimmed.strip(), 'brown fox')

    def test_reference_for_slices_buffered_audio(self):
        # Feed 6s of audio, request a reference for [2,4] with 0.5s pad → the
        # backend should receive the [1.5, 4.5] slice = 3s = 48000 samples.
        from microcaption.io.base import AudioChunk

        class RecordingBackend:
            supports_words = True
            def __init__(self): self.lengths = []
            def transcribe_words(self, samples, beam_size=1):
                self.lengths.append(len(samples)); return []

        be = RecordingBackend()
        v = AccuracyVerifier(
            be, {'verifier': {'context_seconds': 0.5}},
            get_live_cues=lambda: [], on_record=lambda r, s: None)
        v.on_audio(AudioChunk(samples=np.zeros(6 * 16000, dtype=np.float32), timestamp=6.0))
        v._reference_for(2.0, 4.0)
        self.assertEqual(be.lengths[-1], 3 * 16000)


if __name__ == '__main__':
    unittest.main(verbosity=2)

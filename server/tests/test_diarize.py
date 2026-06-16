"""
Unit tests for the diarization layer (no GPU / models).

Covers the pure pieces: cosine helpers, the live SpeakerChangeDetector, the
behind-live OnlineSpeakerClusterer, and the CEA-608/708 speaker-marker helpers.
A FakeEmbedder returns canned vectors keyed off the audio's value, so no ONNX
model or GPU is needed. Run with:
  python -m pytest tests/test_diarize.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest

import numpy as np

from microcaption.diarize.change import SpeakerChangeDetector
from microcaption.diarize.clusterer import OnlineSpeakerClusterer
from microcaption.diarize.embedder import cosine, cosine_distance
from microcaption.caption.normalizer import speaker_marker, apply_speaker_marker

_SR = 16000


class FakeEmbedder:
    """Returns a one-hot vector keyed by the median non-zero sample value, so a
    block of audio filled with value k embeds as 'speaker k'. Padding zeros are
    ignored, mirroring how real slices carry silence at the edges."""

    available = True

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.calls = 0

    def embed(self, samples):
        self.calls += 1
        if samples is None or len(samples) == 0:
            return None
        nz = samples[np.abs(samples) > 1e-6]
        if len(nz) == 0:
            return None
        sid = int(round(float(np.median(nz))))
        v = np.zeros(self.dim, dtype=np.float32)
        v[sid % self.dim] = 1.0
        return v


def utt(speaker: int, seconds: float = 1.0) -> np.ndarray:
    return np.full(int(seconds * _SR), float(speaker), dtype=np.float32)


class CosineTests(unittest.TestCase):
    def test_identical_and_orthogonal(self):
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([0.0, 1.0, 0.0])
        self.assertAlmostEqual(cosine(a, a), 1.0, places=6)
        self.assertAlmostEqual(cosine(a, b), 0.0, places=6)
        self.assertAlmostEqual(cosine_distance(a, a), 0.0, places=6)
        self.assertAlmostEqual(cosine_distance(a, b), 1.0, places=6)

    def test_zero_vector_is_safe(self):
        self.assertEqual(cosine(np.zeros(3), np.ones(3)), 0.0)


class ChangeDetectorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'min_utterance_seconds': 0.2, 'live': {'change_threshold': 0.5}}
        self.det = SpeakerChangeDetector(FakeEmbedder(), self.cfg)

    def test_first_utterance_is_not_a_change(self):
        self.assertFalse(self.det.is_change(utt(1)))

    def test_same_speaker_no_change(self):
        self.det.is_change(utt(1))
        self.assertFalse(self.det.is_change(utt(1)))

    def test_different_speaker_is_change(self):
        self.det.is_change(utt(1))
        self.assertTrue(self.det.is_change(utt(2)))

    def test_short_utterance_skipped_and_does_not_update_reference(self):
        self.det.is_change(utt(1))               # reference = speaker 1
        short = utt(2, seconds=0.05)             # below min_utterance_seconds
        self.assertFalse(self.det.is_change(short))
        # Reference must still be speaker 1, so speaker 1 again is not a change.
        self.assertFalse(self.det.is_change(utt(1)))

    def test_reset_clears_reference(self):
        self.det.is_change(utt(1))
        self.det.reset()
        # After reset the next utterance is "first" again → not a change.
        self.assertFalse(self.det.is_change(utt(2)))

    def test_unavailable_embedder_never_flags(self):
        class Dead:
            available = False
            def embed(self, s):
                return None
        det = SpeakerChangeDetector(Dead(), self.cfg)
        self.assertFalse(det.enabled)
        self.assertFalse(det.is_change(utt(1)))
        self.assertFalse(det.is_change(utt(2)))


class ClustererTests(unittest.TestCase):
    def setUp(self):
        self.c = OnlineSpeakerClusterer({'second_pass': {'cluster_threshold': 0.5}})

    @staticmethod
    def _vec(sid, dim=8):
        v = np.zeros(dim, dtype=np.float32)
        v[sid] = 1.0
        return v

    def test_assigns_and_reuses_stable_indices(self):
        self.assertEqual(self.c.assign(self._vec(0)), 0)   # SPEAKER 1
        self.assertEqual(self.c.assign(self._vec(0)), 0)   # same voice
        self.assertEqual(self.c.assign(self._vec(1)), 1)   # SPEAKER 2
        # A returning speaker keeps the original number (never renumbered).
        self.assertEqual(self.c.assign(self._vec(0)), 0)
        self.assertEqual(self.c.num_speakers, 2)

    def test_label_formatting(self):
        self.assertEqual(OnlineSpeakerClusterer.label(0), 'SPEAKER 1')
        self.assertEqual(OnlineSpeakerClusterer.label(2), 'SPEAKER 3')
        self.assertEqual(OnlineSpeakerClusterer.label(-1), '')

    def test_none_embedding_is_no_speaker(self):
        self.assertEqual(self.c.assign(None), -1)
        self.assertEqual(self.c.num_speakers, 0)

    def test_centering_separates_speakers_with_shared_channel(self):
        # Real embeddings carry a large shared component (channel/room/codec) plus
        # a small per-speaker component, so RAW cosine between *different*
        # speakers is ~1 and naive clustering merges everyone. The clusterer's
        # mean-normalisation must recover the two-speaker structure.
        common = np.zeros(16, dtype=np.float32); common[0] = 6.0

        def emb(spk):
            v = common.copy(); v[1 + spk] = 1.0
            return (v / np.linalg.norm(v)).astype(np.float32)

        # Sanity: raw cosine between the two speakers is dominated by `common`.
        self.assertGreater(cosine(emb(0), emb(1)), 0.95)

        c = OnlineSpeakerClusterer(
            {'second_pass': {'cluster_threshold': 0.1, 'center_warmup': 2}})
        seq = [c.assign(emb(s)) for s in [0, 0, 0, 1, 1, 1, 0, 0, 1, 1]]
        # Two speakers recovered despite the shared component (the pre-centering
        # bug produced exactly one).
        self.assertEqual(c.num_speakers, 2)
        # Post-warmup, each speaker maps to a single stable id.
        self.assertEqual(seq[-1], seq[4])      # speaker 1 windows agree
        self.assertNotEqual(seq[-1], seq[-3])  # speaker 0 vs speaker 1 differ


class SpeakerMarkerTests(unittest.TestCase):
    def test_marker_precedence(self):
        self.assertEqual(speaker_marker(), '')
        self.assertEqual(speaker_marker(speaker_change=True), '>> ')
        self.assertEqual(speaker_marker('SPEAKER 1'), '>> SPEAKER 1: ')
        # A resolved label wins over a bare change mark.
        self.assertEqual(speaker_marker('SPEAKER 2', speaker_change=True),
                         '>> SPEAKER 2: ')

    def test_apply_marker_only_first_line_and_non_mutating(self):
        lines = ['hello there', 'second line']
        out = apply_speaker_marker(lines, speaker_change=True)
        self.assertEqual(out, ['>> hello there', 'second line'])
        self.assertEqual(lines, ['hello there', 'second line'])  # input untouched

    def test_apply_marker_noop_when_nothing_to_mark(self):
        self.assertEqual(apply_speaker_marker(['a', 'b']), ['a', 'b'])
        self.assertEqual(apply_speaker_marker([], speaker_change=True), [])


if __name__ == '__main__':
    unittest.main()

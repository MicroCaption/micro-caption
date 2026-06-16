"""
Unit tests for DiarizationPass — the behind-live speaker-labelling pass.

Drives it synchronously (calls _label_ready directly instead of starting the
worker thread) with a FakeEmbedder and a controlled cue list + audio buffer, and
asserts that committed utterances get stable SPEAKER N labels via on_update. No
GPU / model. Run with:
  python -m pytest tests/test_diarization_pass.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest

import numpy as np

from microcaption.diarize.second_pass import DiarizationPass
from microcaption.io.base import AudioChunk
from tests.test_diarize import FakeEmbedder

_SR = 16000


def _config():
    return {
        'min_utterance_seconds': 0.5,
        'second_pass': {
            'cluster_threshold': 0.5,
            'settle_seconds': 5.0,
            'utterance_gap_seconds': 0.8,
            'context_seconds': 0.25,
        },
    }


class DiarizationPassTests(unittest.TestCase):
    def setUp(self):
        self.cues = []
        self.updates = []
        self.dp = DiarizationPass(
            FakeEmbedder(), _config(),
            get_live_cues=lambda: list(self.cues),
            on_update=self.updates.append,
        )

    def _feed(self, segments, total_seconds):
        """segments: list of (t0, t1, speaker_value). Build one audio buffer over
        [0, total_seconds] and push it as a single chunk."""
        buf = np.zeros(int(total_seconds * _SR), dtype=np.float32)
        for t0, t1, val in segments:
            buf[int(t0 * _SR):int(t1 * _SR)] = float(val)
        # AudioChunk.timestamp is the absolute time at the END of the chunk.
        self.dp.on_audio(AudioChunk(samples=buf, timestamp=total_seconds))

    def test_two_speakers_get_distinct_stable_labels(self):
        # Speaker 1 talks 0–2s, gap, speaker 2 talks 3–4.5s (gap 1.0s > 0.8 split).
        self.cues = [
            {'start': 0.0, 'end': 2.0, 'text': 'hello there'},
            {'start': 3.0, 'end': 4.5, 'text': 'good evening'},
        ]
        self._feed([(0.0, 2.0, 1), (3.0, 4.5, 2)], total_seconds=5.0)
        self.dp._label_ready(force=True)

        by_start = {u['start']: u['speaker'] for upd in self.updates for u in upd}
        self.assertEqual(by_start[0.0], 'SPEAKER 1')
        self.assertEqual(by_start[3.0], 'SPEAKER 2')
        self.assertEqual(self.dp.num_speakers, 2)

    def test_returning_speaker_keeps_first_number(self):
        self.cues = [
            {'start': 0.0, 'end': 2.0, 'text': 'one'},
            {'start': 3.0, 'end': 4.5, 'text': 'two'},
            {'start': 6.0, 'end': 7.5, 'text': 'three'},   # speaker 1 again
        ]
        self._feed([(0.0, 2.0, 1), (3.0, 4.5, 2), (6.0, 7.5, 1)], total_seconds=8.0)
        self.dp._label_ready(force=True)

        by_start = {u['start']: u['speaker'] for upd in self.updates for u in upd}
        self.assertEqual(by_start[0.0], 'SPEAKER 1')
        self.assertEqual(by_start[3.0], 'SPEAKER 2')
        self.assertEqual(by_start[6.0], 'SPEAKER 1')
        self.assertEqual(self.dp.num_speakers, 2)

    def test_cues_in_one_utterance_share_a_label(self):
        # Two cues 0.5s apart (< gap 0.8) → one utterance → one speaker label each.
        self.cues = [
            {'start': 0.0, 'end': 1.0, 'text': 'hello'},
            {'start': 1.3, 'end': 2.5, 'text': 'there friends'},
        ]
        self._feed([(0.0, 2.5, 1)], total_seconds=3.0)
        self.dp._label_ready(force=True)
        labelled = {u['start']: u['speaker'] for upd in self.updates for u in upd}
        self.assertEqual(labelled[0.0], 'SPEAKER 1')
        self.assertEqual(labelled[1.3], 'SPEAKER 1')
        self.assertEqual(self.dp.num_speakers, 1)

    def test_settle_delay_holds_back_unbuffered_spans(self):
        # Only ~3s of audio buffered, but the utterance ends at 2.0; with
        # settle_seconds=5 the span isn't ready yet (no force), so no labels.
        self.cues = [
            {'start': 0.0, 'end': 2.0, 'text': 'hello'},
            {'start': 3.0, 'end': 4.0, 'text': 'next'},
        ]
        self._feed([(0.0, 2.0, 1)], total_seconds=3.0)
        self.dp._label_ready(force=False)
        self.assertEqual(self.updates, [])

    def test_continuous_speech_is_capped_and_fully_labelled(self):
        # 24s of one speaker with NO pauses > the gap. Without a length cap this
        # would be one giant utterance that outgrows the audio buffer and gets
        # skipped; the cap must split it so every cue is labelled.
        cfg = _config()
        cfg['second_pass']['max_utterance_seconds'] = 5.0
        dp = DiarizationPass(FakeEmbedder(), cfg,
                             get_live_cues=lambda: list(self.cues),
                             on_update=self.updates.append)
        # Contiguous 0.8s cues, no gaps, 0..24s.
        self.cues = [{'start': k * 0.8, 'end': (k + 1) * 0.8, 'text': f'w{k}'}
                     for k in range(30)]
        self._feed_into(dp, [(0.0, 24.0, 1)], total_seconds=26.0)
        dp._label_ready(force=True)

        labelled = {u['start']: u['speaker'] for upd in self.updates for u in upd}
        # Every cue got a (single-speaker) label, and the span was split into
        # several capped utterances rather than one un-embeddable block.
        self.assertEqual(len(labelled), len(self.cues))
        self.assertTrue(all(v == 'SPEAKER 1' for v in labelled.values()))
        self.assertGreaterEqual(len(self.updates), 4)   # multiple capped spans

    def _feed_into(self, dp, segments, total_seconds):
        buf = np.zeros(int(total_seconds * _SR), dtype=np.float32)
        for t0, t1, val in segments:
            buf[int(t0 * _SR):int(t1 * _SR)] = float(val)
        dp.on_audio(AudioChunk(samples=buf, timestamp=total_seconds))

    def test_disabled_when_embedder_unavailable(self):
        class Dead:
            available = False
            def embed(self, s):
                return None
        dp = DiarizationPass(Dead(), _config(),
                             get_live_cues=lambda: list(self.cues),
                             on_update=self.updates.append)
        self.assertFalse(dp.enabled)
        dp.start()                       # no worker thread, no crash
        self.assertEqual(self.updates, [])


if __name__ == '__main__':
    unittest.main()

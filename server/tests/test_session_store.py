"""
Unit tests for SessionStore — per-stream caption-log persistence.

No GPU / models / network. Run with:
  python -m pytest tests/test_session_store.py -v
"""

import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest

from microcaption.accuracy.store import SessionStore


def _summary(i, created_at, status='ended', accuracy=0.99):
    return {
        'id': f'sess{i:02d}', 'url': f'http://example/{i}', 'video_id': f'vid{i}',
        'source_type': 'youtube', 'created_at': created_at, 'status': status,
        'accuracy': accuracy, 'segment_count': i, 'cue_count': i * 2,
    }


def _detail(i, created_at, **kw):
    d = _summary(i, created_at, **kw)
    d['records'] = [{'start': 0.0, 'end': 1.0, 'reference': f'ref {i}',
                     'hypothesis': f'hyp {i}', 'accuracy': 0.9, 'wer': 0.1,
                     'substitutions': 1, 'deletions': 0, 'insertions': 0, 'ref_words': 10}]
    d['cues'] = [{'start': 0.0, 'end': 1.0, 'text': f'cue {i}'}]
    return d


class TestSessionStore(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _store(self, retain=20, checkpoint=0.0):
        s = SessionStore({'log_dir': self.dir, 'retain_sessions': retain,
                          'checkpoint_seconds': checkpoint})
        s.open()
        return s

    def test_roundtrip(self):
        s = self._store()
        s.register(_summary(1, 100.0), _detail(1, 100.0))
        got = s.get_detail('sess01')
        self.assertIsNotNone(got)
        self.assertEqual(got['url'], 'http://example/1')
        self.assertEqual(got['cues'][0]['text'], 'cue 1')
        self.assertEqual(len(got['records']), 1)

    def test_index_lists_newest_first(self):
        s = self._store()
        s.register(_summary(1, 100.0), _detail(1, 100.0))
        s.register(_summary(2, 200.0), _detail(2, 200.0))
        ids = [e['id'] for e in s.list_summaries()]
        self.assertEqual(ids, ['sess02', 'sess01'])

    def test_retention_prunes_oldest(self):
        s = self._store(retain=20)
        for i in range(1, 23):                 # 22 streams
            s.register(_summary(i, float(i)), _detail(i, float(i)))
        summaries = s.list_summaries()
        self.assertEqual(len(summaries), 20)
        kept = {e['id'] for e in summaries}
        # Oldest two (sess01, sess02) pruned; their files gone.
        self.assertNotIn('sess01', kept)
        self.assertNotIn('sess02', kept)
        self.assertIn('sess22', kept)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'sess01.json')))
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'sess22.json')))

    def test_index_reloads_after_restart(self):
        s = self._store()
        for i in range(1, 4):
            s.register(_summary(i, float(i)), _detail(i, float(i)))
        # Fresh instance pointed at the same dir.
        s2 = self._store()
        ids = [e['id'] for e in s2.list_summaries()]
        self.assertEqual(ids, ['sess03', 'sess02', 'sess01'])
        self.assertIsNotNone(s2.get_detail('sess02'))

    def test_save_updates_status(self):
        s = self._store()
        s.register(_summary(1, 100.0, status='starting'), _detail(1, 100.0, status='starting'))
        s.save(_summary(1, 100.0, status='ended'), _detail(1, 100.0, status='ended'), final=True)
        self.assertEqual(s.list_summaries()[0]['status'], 'ended')
        self.assertEqual(s.get_detail('sess01')['status'], 'ended')

    def test_checkpoint_throttle_defers_detail_but_finals_write(self):
        s = self._store(checkpoint=999.0)   # effectively never auto-checkpoints
        s.register(_summary(1, 100.0), _detail(1, 100.0, status='starting'))
        # Non-final save within the throttle window: index refreshes, detail may lag.
        s.save(_summary(1, 100.0, accuracy=0.5), _detail(1, 100.0, accuracy=0.5))
        self.assertAlmostEqual(s.list_summaries()[0]['accuracy'], 0.5)
        # Final save always persists detail.
        s.save(_summary(1, 100.0, accuracy=0.42), _detail(1, 100.0, accuracy=0.42), final=True)
        self.assertAlmostEqual(s.get_detail('sess01')['accuracy'], 0.42)


if __name__ == '__main__':
    unittest.main(verbosity=2)

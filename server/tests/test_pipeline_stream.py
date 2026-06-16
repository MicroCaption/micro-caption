import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest
from microcaption.asr.pipeline import HypothesisBuffer


def _texts(words):
    """Extract just the word strings from a list of (start, end, text) tuples."""
    return [w[2] for w in words]


def _hyp(*pairs, dur=0.5):
    """Build a hypothesis [(text, start, end), ...] with sequential timestamps."""
    out = []
    t = 0.0
    for text in pairs:
        out.append((text, t, t + dur))
        t += dur
    return out


class TestHypothesisBuffer(unittest.TestCase):
    """LocalAgreement-2: commit a word only once two hypotheses agree on it."""

    def test_nothing_commits_on_first_hypothesis(self):
        h = HypothesisBuffer()
        h.insert(_hyp('Hello', 'there'), offset=0.0)
        self.assertEqual(_texts(h.flush()), [])  # no prior hypothesis to agree with

    def test_commits_agreed_prefix_only(self):
        h = HypothesisBuffer()
        h.insert(_hyp('Hello', 'there'), offset=0.0)
        h.flush()
        # Second hypothesis extends the first — the shared prefix is now stable.
        h.insert(_hyp('Hello', 'there', 'world'), offset=0.0)
        self.assertEqual(_texts(h.flush()), ['Hello', 'there'])
        # 'world' is still unconfirmed (only seen once).
        self.assertEqual(_texts(h.buffer), ['world'])

    def test_append_only_never_recommits(self):
        h = HypothesisBuffer()
        committed = []
        # Three growing hypotheses of the same utterance.
        for n in range(2, 5):
            words = _hyp(*['Hello', 'there', 'world', 'now'][:n])
            h.insert(words, offset=0.0)
            committed += _texts(h.flush())
        committed += _texts(h.flush_final())
        # Each word committed exactly once, in order, with no duplicates.
        self.assertEqual(committed, ['Hello', 'there', 'world', 'now'])

    def test_disagreement_holds_the_tail(self):
        h = HypothesisBuffer()
        h.insert(_hyp('the', 'cat'), offset=0.0)
        h.flush()
        # The model revised the last word (cat -> hat): only the stable 'the' commits.
        h.insert(_hyp('the', 'hat'), offset=0.0)
        self.assertEqual(_texts(h.flush()), ['the'])
        self.assertEqual(_texts(h.buffer), ['hat'])

    def test_commit_before_holds_recent_words(self):
        # Words at 0-0.5, 0.5-1.0, 1.0-1.5; agreed across two hypotheses.
        h = HypothesisBuffer()
        h.insert(_hyp('Hello', 'there', 'world'), offset=0.0)
        h.flush(commit_before=1.0)
        h.insert(_hyp('Hello', 'there', 'world'), offset=0.0)
        # With a cutoff at 1.0, 'world' (ends 1.5) is held back as look-ahead.
        self.assertEqual(_texts(h.flush(commit_before=1.0)), ['Hello', 'there'])
        self.assertEqual(_texts(h.buffer), ['world'])
        # Once the cutoff advances past it, 'world' is released.
        h.insert(_hyp('Hello', 'there', 'world'), offset=0.0)
        self.assertEqual(_texts(h.flush(commit_before=2.0)), ['world'])

    def test_flush_final_force_commits_tail(self):
        h = HypothesisBuffer()
        h.insert(_hyp('end', 'of', 'sentence'), offset=0.0)
        h.flush()  # nothing agreed yet -> all three sit in buffer
        forced = h.flush_final()
        self.assertEqual(_texts(forced), ['end', 'of', 'sentence'])
        self.assertEqual(h.buffer, [])

    def test_offset_makes_timestamps_absolute(self):
        h = HypothesisBuffer()
        h.insert(_hyp('Hello', 'there'), offset=10.0)
        h.flush()
        h.insert(_hyp('Hello', 'there', 'world'), offset=10.0)
        committed = h.flush()
        self.assertAlmostEqual(committed[0][0], 10.0)   # start shifted by offset
        self.assertAlmostEqual(committed[-1][1], 11.0)  # end of 'there'

    def test_committed_words_are_filtered_from_later_hypotheses(self):
        h = HypothesisBuffer()
        h.insert(_hyp('Hello', 'there'), offset=0.0)
        h.flush()
        h.insert(_hyp('Hello', 'there', 'world'), offset=0.0)
        h.flush()  # commits Hello, there -> last_committed_time = 1.0
        # A later hypothesis still re-emits the committed words (left-context);
        # they must be filtered so only the new word can commit.
        h.insert(_hyp('Hello', 'there', 'world', 'today'), offset=0.0)
        committed = h.flush()
        self.assertNotIn('Hello', _texts(committed))
        self.assertNotIn('there', _texts(committed))
        self.assertEqual(_texts(committed), ['world'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

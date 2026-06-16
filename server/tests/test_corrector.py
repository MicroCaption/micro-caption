import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest
from microcaption.caption.corrector import correct_fragment, last_word


class TestCorrector(unittest.TestCase):

    def test_collapses_triple_repeat(self):
        self.assertEqual(correct_fragment('the the the cat'), 'the cat')
        self.assertEqual(correct_fragment('go go go go now'), 'go now')

    def test_keeps_legitimate_double(self):
        # Doubles can be valid English ("had had") — never collapse them.
        self.assertEqual(correct_fragment('I had had enough'), 'I had had enough')

    def test_fixes_space_before_punctuation(self):
        self.assertEqual(correct_fragment('hello , world .'), 'hello, world.')

    def test_collapses_repeated_punctuation(self):
        self.assertEqual(correct_fragment('wait!!! stop..'), 'wait! stop.')

    def test_collapses_whitespace(self):
        self.assertEqual(correct_fragment('a   b\t c'), 'a b c')

    def test_drops_cross_fragment_duplicate(self):
        # Previous fragment ended in "going"; this one repeats it at the start.
        self.assertEqual(correct_fragment('going to the store', prev_word='going'),
                         'to the store')

    def test_keeps_word_when_not_a_duplicate(self):
        self.assertEqual(correct_fragment('to the store', prev_word='going'),
                         'to the store')

    def test_does_not_empty_a_pure_duplicate_fragment(self):
        # If the whole fragment is just the repeated word, leave it rather than
        # emit nothing (no second word to fall back to).
        self.assertEqual(correct_fragment('going', prev_word='going'), 'going')

    def test_last_word(self):
        self.assertEqual(last_word('to the store.'), 'store')
        self.assertEqual(last_word(''), '')

    def test_empty_input(self):
        self.assertEqual(correct_fragment(''), '')
        self.assertEqual(correct_fragment('   '), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest
from tools.validate_wer import compute_wer, tokenize


class TestWER(unittest.TestCase):

    def test_perfect_match(self):
        self.assertAlmostEqual(compute_wer('hello world', 'hello world'), 0.0)

    def test_complete_substitution(self):
        wer = compute_wer('hello world', 'foo bar')
        self.assertAlmostEqual(wer, 1.0)

    def test_one_deletion(self):
        # Reference: "hello world there" (3 words), hypothesis: "hello there" (missing "world")
        # 1 deletion / 3 reference words = 0.333...
        wer = compute_wer('hello world there', 'hello there')
        self.assertAlmostEqual(wer, 1/3, places=3)

    def test_one_insertion(self):
        # Reference: "hello world" (2), hypothesis: "hello big world" (1 insertion)
        # 1 insertion / 2 ref words = 0.5
        wer = compute_wer('hello world', 'hello big world')
        self.assertAlmostEqual(wer, 0.5)

    def test_case_insensitive(self):
        self.assertAlmostEqual(compute_wer('Hello World', 'hello world'), 0.0)

    def test_empty_hypothesis(self):
        wer = compute_wer('hello world', '')
        self.assertAlmostEqual(wer, 1.0)

    def test_empty_reference_raises(self):
        with self.assertRaises(ValueError):
            compute_wer('', 'hello')

    def test_tokenize(self):
        tokens = tokenize('Hello, world!')
        self.assertEqual(tokens, ['hello', 'world'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

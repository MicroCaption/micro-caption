import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest
from microcaption.caption.normalizer import CaptionNormalizer


class TestCaptionNormalizer(unittest.TestCase):

    def setUp(self):
        self.norm = CaptionNormalizer({'max_line_length': 32, 'max_lines': 2})

    def test_short_text_single_line(self):
        r = self.norm.normalize('Hello world')
        self.assertEqual(r.lines, ['Hello world'])

    def test_long_text_wraps(self):
        text = 'The quick brown fox jumps over the lazy dog today'
        r = self.norm.normalize(text)
        for line in r.lines:
            self.assertLessEqual(len(line), 32)

    def test_max_lines_enforced(self):
        text = 'word ' * 40  # forces many lines
        r = self.norm.normalize(text)
        self.assertLessEqual(len(r.lines), 2)

    def test_whitespace_collapsed(self):
        r = self.norm.normalize('hello   world\t\tthere')
        self.assertEqual(r.raw, 'hello world there')

    def test_empty_string(self):
        r = self.norm.normalize('')
        self.assertEqual(r.lines, [])

    def test_to_display_string(self):
        r = self.norm.normalize('Line one is long enough to wrap onto a second line here')
        display = self.norm.to_display_string(r)
        self.assertIn('\n', display)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""
Conservative, deterministic caption clean-up.

Runs on text the streaming pipeline has already finalised (after the
look-ahead review window). The goal is to fix mechanical ASR artefacts
*without* second-guessing the model's word choices — anything ambiguous is
left alone, since a wrong "correction" is itself an accuracy error.
"""

import re

# 3+ identical words in a row are virtually always a stutter/hallucination
# artefact (genuine English tops out at doubles like "had had"), so collapse
# them to one. Doubles are deliberately left untouched.
_TRIPLE_REPEAT = re.compile(r'\b(\w+)(?:\s+\1\b){2,}', re.IGNORECASE)
# Stray space before sentence punctuation: "word ." -> "word."
_SPACE_BEFORE_PUNCT = re.compile(r'\s+([,.!?;:])')
# Collapse runs of repeated punctuation that Whisper sometimes emits.
_REPEAT_PUNCT = re.compile(r'([,.!?;:])\1+')


def correct_fragment(text: str, prev_word: str = '') -> str:
    """
    Clean a finalised caption fragment.

    prev_word is the last word already shown (the tail of the previous
    fragment) so a duplicate spanning the fragment boundary can be dropped.
    """
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return ''
    if prev_word:
        # Drop a leading word that just repeats the previous fragment's tail.
        m = re.match(r'(\w+)\b\s*(.*)', text, re.DOTALL)
        if m and m.group(1).lower() == prev_word.lower() and m.group(2):
            text = m.group(2)
    text = _TRIPLE_REPEAT.sub(r'\1', text)
    text = _REPEAT_PUNCT.sub(r'\1', text)
    text = _SPACE_BEFORE_PUNCT.sub(r'\1', text)
    return text.strip()


def last_word(text: str) -> str:
    """Return the final word token of text (for cross-fragment de-duplication)."""
    words = re.findall(r'\w+', text)
    return words[-1] if words else ''

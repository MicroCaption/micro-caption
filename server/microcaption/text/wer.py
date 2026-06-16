"""
Word Error Rate (WER) scoring — reusable core.

Used by the accuracy verifier (real-time, per-stream scoring) and by the
offline tools/validate_wer.py CLI. No external dependencies.

WER = (S + D + I) / N
  S = substitutions, D = deletions, I = insertions, N = reference word count
"""

import re
from typing import List, Tuple


def tokenize(text: str) -> List[str]:
    """Lowercase, strip punctuation (keep apostrophes), return word tokens."""
    text = text.lower()
    text = re.sub(r"[^\w\s']", '', text)   # keep apostrophes for contractions
    return text.split()


def compute_wer(reference: str, hypothesis: str) -> float:
    """
    Word Error Rate as a float in [0.0, ∞).  Case-insensitive; punctuation
    stripped.  Values > 1.0 are possible when the hypothesis is longer than the
    reference.  Raises ValueError on an empty reference.
    """
    ref = tokenize(reference)
    hyp = tokenize(hypothesis)
    if not ref:
        raise ValueError('Reference transcript is empty')
    _, s, d, ins = _edit_distance(ref, hyp)
    return (s + d + ins) / len(ref)


def compute_wer_detailed(reference: str, hypothesis: str) -> dict:
    """Full breakdown: wer, substitutions, deletions, insertions, ref/hyp word counts."""
    ref = tokenize(reference)
    hyp = tokenize(hypothesis)
    if not ref:
        raise ValueError('Reference transcript is empty')
    _, s, d, ins = _edit_distance(ref, hyp)
    n = len(ref)
    return {
        'wer': (s + d + ins) / n,
        'substitutions': s,
        'deletions': d,
        'insertions': ins,
        'ref_words': n,
        'hyp_words': len(hyp),
    }


def _edit_distance(ref: List[str], hyp: List[str]) -> Tuple[int, int, int, int]:
    """
    Standard word-level Levenshtein distance.

    Returns (distance, substitutions, deletions, insertions).
    """
    n, m = len(ref), len(hyp)

    # dp[i][j] = (distance, S, D, I) to align ref[:i] with hyp[:j]
    INF = (10**9, 0, 0, 0)
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = (0, 0, 0, 0)

    for i in range(1, n + 1):
        dist, s, d, ins = dp[i - 1][0]
        dp[i][0] = (dist + 1, s, d + 1, ins)

    for j in range(1, m + 1):
        dist, s, d, ins = dp[0][j - 1]
        dp[0][j] = (dist + 1, s, d, ins + 1)

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            # Match / substitution
            if ref[i - 1] == hyp[j - 1]:
                cost_sub = dp[i - 1][j - 1]
            else:
                prev = dp[i - 1][j - 1]
                cost_sub = (prev[0] + 1, prev[1] + 1, prev[2], prev[3])

            # Deletion (skip ref word)
            prev_d = dp[i - 1][j]
            cost_del = (prev_d[0] + 1, prev_d[1], prev_d[2] + 1, prev_d[3])

            # Insertion (skip hyp word)
            prev_i = dp[i][j - 1]
            cost_ins = (prev_i[0] + 1, prev_i[1], prev_i[2], prev_i[3] + 1)

            dp[i][j] = min(cost_sub, cost_del, cost_ins)

    return dp[n][m]

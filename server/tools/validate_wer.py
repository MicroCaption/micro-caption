#!/usr/bin/env python3
"""
WER (Word Error Rate) validation tool.

Compares a hypothesis transcript file against a reference transcript and
reports S/D/I counts and WER.  No external dependencies required.

Usage:
  python tools/validate_wer.py reference.txt hypothesis.txt
  python tools/validate_wer.py reference.txt hypothesis.txt --json
  python tools/validate_wer.py reference.txt logs/cea608.jsonl --from-jsonl

File formats:
  Plain text: one caption per line (concatenated as the full transcript)
  JSONL:      one JSON object per line; reads the 'text' field from each

WER = (S + D + I) / N
  S = substitutions, D = deletions, I = insertions, N = reference word count
"""

import argparse
import json
import re
import sys
from typing import List, Tuple


def tokenize(text: str) -> List[str]:
    """Lowercase and strip punctuation, return word tokens."""
    text = text.lower()
    text = re.sub(r"[^\w\s']", '', text)   # keep apostrophes for contractions
    return text.split()


def compute_wer(reference: str, hypothesis: str) -> float:
    """
    Compute Word Error Rate using word-level Levenshtein distance.
    Case-insensitive; punctuation is stripped.

    Returns WER as a float in [0.0, ∞).  Values > 1.0 are possible when
    the hypothesis is longer than the reference.
    """
    ref = tokenize(reference)
    hyp = tokenize(hypothesis)

    if not ref:
        raise ValueError('Reference transcript is empty')

    _, s, d, ins = _edit_distance(ref, hyp)
    return (s + d + ins) / len(ref)


def compute_wer_detailed(reference: str, hypothesis: str) -> dict:
    """Return full breakdown: wer, substitutions, deletions, insertions, ref_len."""
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
    Standard word-level edit distance (Levenshtein).

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


def _read_transcript(path: str, from_jsonl: bool) -> str:
    with open(path) as f:
        if from_jsonl:
            parts = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                parts.append(obj.get('text', ''))
            return ' '.join(parts)
        else:
            return ' '.join(f.read().split())


def main() -> None:
    parser = argparse.ArgumentParser(description='Compute WER between reference and hypothesis transcripts')
    parser.add_argument('reference', help='Reference transcript file (plain text)')
    parser.add_argument('hypothesis', help='Hypothesis transcript file')
    parser.add_argument('--from-jsonl', action='store_true',
                        help='Read hypothesis from JSONL log (uses text field)')
    parser.add_argument('--json', action='store_true', help='Output result as JSON')
    args = parser.parse_args()

    ref_text = _read_transcript(args.reference, False)
    hyp_text = _read_transcript(args.hypothesis, args.from_jsonl)

    result = compute_wer_detailed(ref_text, hyp_text)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f'Reference words : {result["ref_words"]}')
        print(f'Hypothesis words: {result["hyp_words"]}')
        print(f'Substitutions   : {result["substitutions"]}')
        print(f'Deletions       : {result["deletions"]}')
        print(f'Insertions      : {result["insertions"]}')
        print(f'WER             : {result["wer"]:.2%}')


if __name__ == '__main__':
    main()

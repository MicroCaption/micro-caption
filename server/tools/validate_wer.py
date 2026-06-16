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
import os
import sys

# Reuse the shared WER core (server/microcaption/text/wer.py) so the CLI and the
# real-time accuracy verifier compute scores identically.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from microcaption.text.wer import compute_wer, compute_wer_detailed, tokenize  # noqa: E402,F401


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

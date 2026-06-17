#!/usr/bin/env python3
"""
Validate RollUp608Encoder by decoding/rendering its output through a CEA-608
roll-up state machine — the way a real decoder (YouTube/ccextractor) would.

Run from server/:  python3 scratch/test_rollup_608.py
"""
import os
import sys
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from microcaption.caption.rollup_608 import RollUp608Encoder


class RollUpDecoder:
    """Minimal CEA-608 roll-up renderer: applies RU2/3/4, PAC, CR and text the
    way a TV/YouTube decoder does, including ignoring doubled control codes."""

    def __init__(self):
        self.rows = deque([''], maxlen=2)
        self._last_ctrl = None

    def _is_ctrl(self, b1):
        return 0x10 <= b1 <= 0x1F

    def feed(self, pairs):
        for p0, p1 in pairs:
            b1, b2 = p0 & 0x7F, p1 & 0x7F
            if b1 == 0x00:
                continue                      # null padding
            if self._is_ctrl(b1):
                pair = (b1, b2)
                if pair == self._last_ctrl:   # doubled control → ignore repeat
                    self._last_ctrl = None
                    continue
                self._last_ctrl = pair
                self._ctrl(b1, b2)
            else:
                self._last_ctrl = None
                self._text(b1)
                if b2:
                    self._text(b2)

    def _ctrl(self, b1, b2):
        if 0x10 <= b1 <= 0x17 and 0x40 <= b2 <= 0x7F:
            return                            # PAC — base row set; ignore for render
        if b2 == 0x25:                        # RU2
            self.rows = deque(self.rows, maxlen=2)
        elif b2 == 0x26:                      # RU3
            self.rows = deque(self.rows, maxlen=3)
        elif b2 == 0x2D:                      # CR — roll up
            self.rows.append('')
        elif b2 == 0x2C:                      # EDM — erase
            self.rows = deque([''], maxlen=self.rows.maxlen)

    def _text(self, b):
        if 0x20 <= b <= 0x7F:
            self.rows[-1] += chr(b)

    def render(self):
        return [r for r in self.rows if r]


def main():
    # Simulate streaming ASR committed fragments.
    fragments = ('The city council meeting will now come to order . '
                 'Please rise for the pledge of allegiance . '
                 'Item one on the agenda is the budget review for fiscal '
                 'year twenty twenty six .').split()
    # feed a few words at a time, like the streaming backend commits
    enc = RollUp608Encoder(rows=2)
    dec = RollUpDecoder()
    i = 0
    while i < len(fragments):
        chunk = ' '.join(fragments[i:i + 3])
        i += 3
        dec.feed(enc.add(chunk))

    rows = dec.render()
    print('Final visible roll-up rows:')
    for r in rows:
        print(f'  |{r}|  (len {len(r)})')

    joined = ' '.join(rows)
    ok = all(w in joined for w in ['budget', 'review', 'fiscal']) \
        and all(len(r) <= 32 for r in rows) \
        and len(rows) <= 2
    print()
    print('RESULT:', 'PASS ✓ readable wrapped roll-up'
          if ok else 'FAIL ✗')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

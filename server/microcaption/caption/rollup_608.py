"""
Streaming CEA-608 roll-up caption encoder.

Turns an append-only stream of committed ASR text into a well-formed CEA-608
byte-pair stream that real decoders (YouTube, ccextractor, TV) render as clean
2-row roll-up captions.

What real decoders need that a naive "RU2 + CR + text" stream lacks:
  • a PAC (Preamble Address Code) to position the base row (row 15) — without
    it the cursor position is undefined and text scatters;
  • control codes transmitted **twice** — the decoder acts on the first and
    ignores the immediate duplicate (CEA-608-E §8.4); single codes get dropped;
  • carriage returns inserted only when a row fills (word-wrapped), not once per
    tiny ASR fragment — otherwise every fragment starts a new row and the
    display becomes an unreadable vertical dribble.

Reference: CEA-608-E / ANSI-CTA-608-E.
"""
from typing import List, Optional

from .packetizer_608 import add_parity, Frame608

WIDTH = 32          # CEA-608 columns
_NULL = add_parity(0x00)   # 0x80


def _ctrl(second: int) -> Frame608:
    """Channel-1 control pair (0x14 header), parity on both bytes."""
    return (add_parity(0x14), add_parity(second))


RU2 = _ctrl(0x25)   # roll-up, 2 rows
RU3 = _ctrl(0x26)   # roll-up, 3 rows
RU4 = _ctrl(0x27)   # roll-up, 4 rows
CR = _ctrl(0x2D)    # carriage return (roll the window up one row)
EDM = _ctrl(0x2C)   # erase displayed memory
# PAC: row 15, white, column 0  (byte1 0x14, byte2 0x60), parity applied.
PAC_ROW15 = (add_parity(0x14), add_parity(0x60))


class RollUp608Encoder:
    """
    Append committed caption text; get back the CEA-608 frame pairs to transmit.

    Stateful across calls: tracks whether the roll-up base row has been set up
    and the current column, so successive ASR fragments flow onto the same row
    and wrap to a new (rolled-up) row only when full.
    """

    # Re-transmit the roll-up setup (RU + PAC) every N rows so the decoder
    # re-syncs quickly if a caption byte is ever dropped in delivery — otherwise
    # a single dropped pair desyncs the stateful 608 stream permanently.
    _REINIT_ROWS = 3

    def __init__(self, rows: int = 2) -> None:
        self._ru = {2: RU2, 3: RU3, 4: RU4}.get(rows, RU2)
        self._initialized = False
        self._col = 0
        self._rows = 0

    def reset(self) -> List[Frame608]:
        self._initialized = False
        self._col = 0
        self._rows = 0
        return [EDM, EDM]

    def add(self, text: str) -> List[Frame608]:
        if not text or not text.strip():
            return []

        out: List[Frame608] = []
        pend: List[Optional[int]] = [None]   # half-filled text pair

        def put_char(byte: int) -> None:
            if pend[0] is None:
                pend[0] = byte
            else:
                out.append((pend[0], byte))
                pend[0] = None

        def flush() -> None:
            if pend[0] is not None:
                out.append((pend[0], _NULL))
                pend[0] = None

        def put_ctrl(pair: Frame608) -> None:
            flush()
            out.append(pair)
            out.append(pair)          # doubled — decoder ignores the repeat

        def put_setup() -> None:
            put_ctrl(self._ru)
            put_ctrl(PAC_ROW15)

        def roll() -> None:
            put_ctrl(CR)              # roll up; cursor returns to base row col 0
            self._col = 0
            self._rows += 1
            if self._rows % self._REINIT_ROWS == 0:
                put_setup()           # periodic re-sync at a row boundary

        if not self._initialized:
            put_setup()
            self._initialized = True
            self._col = 0
            self._rows = 0

        for word in text.split():
            word = word[:WIDTH]
            space = 1 if self._col > 0 else 0
            if self._col + space + len(word) > WIDTH:
                roll()
                space = 0
            if space:
                put_char(add_parity(0x20))
                self._col += 1
            for ch in word:
                code = ord(ch)
                put_char(add_parity(code if 0x20 <= code <= 0x7F else ord('?')))
                self._col += 1

        flush()
        return out

"""
CEA-608-E / EIA-608 caption packetizer.

Generates byte pairs suitable for insertion into Line 21 of an analog
video signal, or the cc_data payload of a CEA-708 DTVCC packet's
backward-compatible 608 channels.

Reference: CEA-608-E "Line 21 Data Services" (ANSI/CTA-608-E S-2019)

Parity: CEA-608 requires odd parity on every byte transmitted.
  add_parity() sets bit 7 so the total number of 1-bits is odd.

Control codes use Channel 1 (0x14 header) by default.
"""

from typing import List, Tuple

# A 608 frame is always a pair of bytes
Frame608 = Tuple[int, int]


# ── Parity ──────────────────────────────────────────────────────────────────

def add_parity(byte: int) -> int:
    """Add odd parity bit 7 to a 7-bit value (CEA-608-E §7.7)."""
    b = byte & 0x7F
    if bin(b).count('1') % 2 == 0:  # even count → set bit 7 to make it odd
        return b | 0x80
    return b


# ── Control code constants (pre-parity values) ───────────────────────────────
# First byte is the channel header; both bytes need parity applied.

_CH1 = 0x14  # Channel 1 non-printable control header

# Miscellaneous Control Codes (second byte, ch1 assumed)
_MCC_RU2 = 0x25   # Roll-Up Captions — 2 rows
_MCC_RU3 = 0x26   # Roll-Up Captions — 3 rows
_MCC_RU4 = 0x27   # Roll-Up Captions — 4 rows
_MCC_CR  = 0x2D   # Carriage Return
_MCC_EDM = 0x2C   # Erase Displayed Memory
_MCC_EOC = 0x2F   # End of Caption (pop-on flip)
_MCC_BS  = 0x21   # Backspace
_MCC_DER = 0x24   # Delete to End of Row

# Null byte (filler, with parity = 0x80)
_NULL = add_parity(0x00)


# ── Pre-built control frames ─────────────────────────────────────────────────

def _ctrl(mcc: int) -> Frame608:
    return (add_parity(_CH1), add_parity(mcc))


FRAME_RU2 = _ctrl(_MCC_RU2)   # (0x94, 0x25) — set roll-up 2-row mode
FRAME_RU3 = _ctrl(_MCC_RU3)   # (0x94, 0xA6) — set roll-up 3-row mode
FRAME_CR  = _ctrl(_MCC_CR)    # (0x94, 0xAD) — carriage return
FRAME_EDM = _ctrl(_MCC_EDM)   # (0x94, 0x2C) — erase displayed memory
FRAME_EOC = _ctrl(_MCC_EOC)   # (0x94, 0xAF) — end of caption (flip)
FRAME_NULL = (_NULL, _NULL)    # (0x80, 0x80) — null filler


# ── Text encoding ─────────────────────────────────────────────────────────────

def encode_text(text: str) -> List[Frame608]:
    """
    Encode a string into CEA-608 character frames.

    Characters outside printable ASCII (0x20–0x7F) are replaced with '?'.
    Two characters are packed per frame; an odd-length string is padded
    with a null byte in the second position of the last frame.
    """
    chars = []
    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7F:
            chars.append(add_parity(code))
        else:
            chars.append(add_parity(ord('?')))

    frames: List[Frame608] = []
    for i in range(0, len(chars), 2):
        b1 = chars[i]
        b2 = chars[i + 1] if i + 1 < len(chars) else _NULL
        frames.append((b1, b2))

    return frames


# ── Public packetizer ─────────────────────────────────────────────────────────

class CEA608Packetizer:
    """
    Converts normalised caption lines into a sequence of CEA-608 byte frames
    ready for stream insertion or logging.

    Mode: roll-up (default, rows=2).  Pop-on mode not implemented in this phase.
    """

    def __init__(self, config: dict) -> None:
        rows = config.get('rollup_rows', 2)
        self._mode_frame = {2: FRAME_RU2, 3: FRAME_RU3, 4: FRAME_RU2}.get(rows, FRAME_RU2)
        self._initialized = False

    def packetize(self, lines: List[str]) -> List[Frame608]:
        """
        Return the ordered list of 608 frames for the given caption lines.

        First call sends the roll-up initialisation command.
        Each line is preceded by a Carriage Return.
        """
        frames: List[Frame608] = []

        if not self._initialized:
            frames.append(self._mode_frame)
            self._initialized = True

        for line in lines:
            frames.append(FRAME_CR)
            frames.extend(encode_text(line))

        return frames

    def reset(self) -> None:
        frames = [FRAME_EDM]
        self._initialized = False
        return frames

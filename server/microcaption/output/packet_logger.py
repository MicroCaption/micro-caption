"""
Logs CEA-608 and CEA-708 packet structures to JSONL files and/or
hex-dump strings for offline validation (no SDI hardware required).
"""

import json
import os
import threading
import time
from typing import List, Optional, Tuple

from ..caption.packetizer_608 import Frame608
from ..caption.packetizer_708 import CCDataTuple


class PacketLogger:
    """
    Writes packet records to .jsonl files.

    Each record line contains:
      timestamp, type (608/708), hex bytes, decoded annotation
    """

    def __init__(self, config: dict) -> None:
        self._fmt: str = config.get('format', 'both')   # json | hex | both
        self._log_608 = config.get('log_608', 'logs/cea608.jsonl')
        self._log_708 = config.get('log_708', 'logs/cea708.jsonl')
        self._flush_interval: float = config.get('flush_interval', 1.0)
        self._lock = threading.Lock()
        self._f608: Optional[object] = None
        self._f708: Optional[object] = None

    def open(self) -> None:
        os.makedirs('logs', exist_ok=True)
        self._f608 = open(self._log_608, 'a', buffering=1)
        self._f708 = open(self._log_708, 'a', buffering=1)

    def close(self) -> None:
        for f in (self._f608, self._f708):
            if f:
                f.close()

    # ── CEA-608 ──────────────────────────────────────────────────────────────

    def log_608(self, frames: List[Frame608], caption_text: str = '') -> str:
        """Log 608 frames; returns hex dump string."""
        ts = time.time()
        hex_lines = []
        records = []

        for i, (b1, b2) in enumerate(frames):
            annotation = _annotate_608(b1, b2)
            hex_line = f'  {i:04d}: {b1:02X} {b2:02X}  [{annotation}]'
            hex_lines.append(hex_line)
            records.append({'b1': b1, 'b2': b2, 'note': annotation})

        dump = (
            f'CEA-608 [{_fmt_ts(ts)}] text={caption_text!r}\n'
            + '\n'.join(hex_lines)
        )

        if self._fmt in ('json', 'both') and self._f608:
            entry = json.dumps({
                'ts': ts, 'text': caption_text, 'frames': records
            })
            with self._lock:
                self._f608.write(entry + '\n')

        return dump

    # ── CEA-708 ──────────────────────────────────────────────────────────────

    def log_708(self, raw_packet: bytes, cc_data: List[CCDataTuple],
                caption_text: str = '') -> str:
        """Log 708 DTVCC packet; returns hex dump string."""
        ts = time.time()

        raw_hex = ' '.join(f'{b:02X}' for b in raw_packet)
        cc_hex_lines = []
        cc_records = []

        for i, (cc_type, d1, d2) in enumerate(cc_data):
            cc_name = 'DTVCC_START' if cc_type == 0x06 else 'DTVCC_DATA'
            line = f'  {i:04d}: {cc_type:02X} {d1:02X} {d2:02X}  [{cc_name}]'
            cc_hex_lines.append(line)
            cc_records.append({'cc_type': cc_type, 'd1': d1, 'd2': d2, 'type_name': cc_name})

        # Parse and annotate the DTVCC packet header
        if raw_packet:
            seq = (raw_packet[0] >> 6) & 0x03
            size_code = raw_packet[0] & 0x3F
            pkt_bytes = (size_code + 1) * 2
            header_note = f'seq={seq}, size_code={size_code} ({pkt_bytes} bytes)'
        else:
            header_note = '(empty)'

        dump = (
            f'DTVCC-708 [{_fmt_ts(ts)}] text={caption_text!r}\n'
            f'  Packet header: {header_note}\n'
            f'  Raw: {raw_hex}\n'
            f'  cc_data tuples:\n'
            + '\n'.join(cc_hex_lines)
        )

        if self._fmt in ('json', 'both') and self._f708:
            entry = json.dumps({
                'ts': ts, 'text': caption_text,
                'raw_hex': raw_hex, 'cc_data': cc_records,
            })
            with self._lock:
                self._f708.write(entry + '\n')

        return dump


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_ts(t: float) -> str:
    h = int(t // 3600) % 24
    m = int((t % 3600) // 60)
    s = t % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'


_608_ANNOTATIONS = {
    (0x94, 0x25): 'RU2 — Roll-Up 2 rows',
    (0x94, 0xA6): 'RU3 — Roll-Up 3 rows',
    (0x94, 0x27): 'RU4 — Roll-Up 4 rows',
    (0x94, 0xAD): 'CR  — Carriage Return',
    (0x94, 0x2C): 'EDM — Erase Displayed Memory',
    (0x94, 0xAF): 'EOC — End of Caption',
    (0x80, 0x80): 'NULL',
}


def _annotate_608(b1: int, b2: int) -> str:
    key = (b1, b2)
    if key in _608_ANNOTATIONS:
        return _608_ANNOTATIONS[key]
    # Try to decode as text (strip parity bit 7)
    c1, c2 = b1 & 0x7F, b2 & 0x7F
    if 0x20 <= c1 <= 0x7F and 0x20 <= c2 <= 0x7F:
        return f'text: {chr(c1)}{chr(c2)}'
    if 0x20 <= c1 <= 0x7F and c2 == 0x00:
        return f'text: {chr(c1)} + NULL'
    return f'raw: {b1:02X} {b2:02X}'

"""
CEA-708-E / CTA-708 DTVCC caption packetizer.

Generates Digital Television Closed Caption (DTVCC) packets as used in
MPEG-2 user_data and H.264/AVC SEI NAL units (pic_timing or
user_data_registered_itu_t_t35 for ATSC).

References:
  CEA-708-E "Digital Television (DTV) Closed Captioning"
  ATSC A/53 Part 4 "MPEG-2 Video Systems Characteristics"

Packet structure (CEA-708-E §4):
  Byte 0:  sequence_number[7:6] | packet_size_code[5:0]
           where actual byte count = (packet_size_code + 1) × 2

Service block header (§5):
  Byte 0:  service_number[7:5] | block_size[4:0]
           service 1 = primary language (English)
           block_size = number of data bytes following this header

C1 command codes (§7.1.6):
  0x80–0x87  CW0–CW7   SetCurrentWindow
  0x8F       RST       Reset
  0x98–0x9F  DF0–DF7   DefineWindow

G0 characters: 0x20–0x7F (printable ASCII, same code points as ASCII)

cc_data tuple format (for MPEG/H.264 embedding):
  byte 0: cc_valid(bit2) | cc_type(bits 1:0)
    0x06 = valid + DTVCC_packet_start
    0x07 = valid + DTVCC_packet_data
  byte 1, byte 2: DTVCC packet bytes (MSB first, pairs)
"""

from typing import List, Tuple

# cc_data tuple: (cc_type_byte, data_byte_1, data_byte_2)
CCDataTuple = Tuple[int, int, int]


# ── C1 command constants ─────────────────────────────────────────────────────

CW0 = 0x80   # SetCurrentWindow 0
RST = 0x8F   # Reset (clears all windows and pen state)
DF0 = 0x98   # DefineWindow 0

# cc_data cc_type bytes
_CC_DTVCC_START = 0x06   # valid=1, type=2 (DTVCC packet start)
_CC_DTVCC_DATA  = 0x07   # valid=1, type=3 (DTVCC packet continuation)


# ── DefineWindow helper ──────────────────────────────────────────────────────

def define_window(
    window_id: int = 0,
    visible: bool = True,
    priority: int = 0,
    anchor_v: int = 74,    # 0-74 (74 = bottom of safe-title area)
    anchor_h: int = 105,   # 0-209 (105 = horizontal center), stored in 7 bits (0-127 range)
    anchor_id: int = 7,    # 7 = BC (Bottom-Center anchor point)
    row_count: int = 2,    # number of visible rows
    col_count: int = 32,   # number of columns
    window_style: int = 1, # 1 = default opaque black background
    pen_style: int = 1,    # 1 = default white text
) -> bytes:
    """
    Build a DefineWindow command (CEA-708-E §7.1.6.10).

    anchor_h is clamped to 7 bits (0-127) for this field.
    For absolute positioning anchor_h ≤ 127 covers center-screen.
    """
    cmd = DF0 + (window_id & 0x07)
    b1 = ((priority & 0x07) << 5) | (0 << 4) | (0 << 3) | ((1 if visible else 0) << 2)
    b2 = anchor_v & 0xFF
    b3 = anchor_h & 0x7F   # relative_positioning=0 (bit 7 clear), horizontal in 7 bits
    b4 = ((anchor_id & 0x0F) << 4) | ((row_count - 1) & 0x0F)
    b5 = (col_count - 1) & 0x3F
    b6 = ((window_style & 0x07) << 3) | (pen_style & 0x07)
    return bytes([cmd, b1, b2, b3, b4, b5, b6])


# ── Service block builder ────────────────────────────────────────────────────

def build_service_block(service_number: int, data: bytes) -> bytes:
    """
    Build a service block (CEA-708-E §5.1).

    block_size is stored in 5 bits → max 31 bytes per block.
    Caller is responsible for splitting larger payloads.
    """
    if len(data) > 31:
        raise ValueError(f'Service block data too large: {len(data)} > 31 bytes')
    if service_number < 1 or service_number > 6:
        raise ValueError(f'service_number must be 1–6; got {service_number}')
    header = ((service_number & 0x07) << 5) | (len(data) & 0x1F)
    return bytes([header]) + data


# ── DTVCC packet builder ──────────────────────────────────────────────────────

class DTVCC708Packetizer:
    """
    Wraps 708 service-block payloads into DTVCC packets and
    formats them as cc_data tuples for MPEG embedding.

    Sequence number increments 0→1→2→3→0→… per packet.
    """

    def __init__(self, config: dict) -> None:
        self._service: int = config.get('service', 1)
        self._seq: int = 0
        self._initialized = False

    def packetize(self, lines: List[str]) -> Tuple[bytes, List[CCDataTuple]]:
        """
        Convert caption lines to a DTVCC packet.

        Returns:
          raw_packet  — the raw DTVCC packet bytes
          cc_data     — list of cc_data tuples for MPEG embedding
        """
        # Build 708 service block payload
        payload = bytearray()

        if not self._initialized:
            payload += bytes([RST])          # Reset all windows
            payload += define_window(        # Define window 0 at bottom-center
                window_id=0,
                visible=True,
                anchor_v=74,
                anchor_h=105,
                anchor_id=7,
                row_count=2,
                col_count=32,
            )
            payload += bytes([CW0])          # SetCurrentWindow 0
            self._initialized = True
        else:
            payload += bytes([CW0])          # SetCurrentWindow 0

        # Encode G0 text (0x20–0x7F; substitute '?' for out-of-range)
        for line in lines:
            payload += bytes(0x0D)           # CR within window
            for ch in line:
                code = ord(ch)
                payload.append(code if 0x20 <= code <= 0x7F else ord('?'))

        # Split into service blocks (max 31 bytes each)
        blocks = bytearray()
        for i in range(0, len(payload), 31):
            chunk = bytes(payload[i:i + 31])
            blocks += build_service_block(self._service, chunk)
        blocks += b'\x00'  # null block terminator

        # Pad to even byte count after the header
        if len(blocks) % 2 != 0:
            blocks += b'\x00'

        # Build DTVCC packet header
        size_code = len(blocks) // 2 - 1
        header = ((self._seq & 0x03) << 6) | (size_code & 0x3F)
        raw_packet = bytes([header]) + bytes(blocks)

        # Advance sequence counter
        self._seq = (self._seq + 1) & 0x03

        # Format as cc_data tuples
        cc_data = self._to_cc_data(raw_packet)

        return raw_packet, cc_data

    @staticmethod
    def _to_cc_data(packet: bytes) -> List[CCDataTuple]:
        """Pack raw packet bytes into cc_data 3-tuples (§6.4.3)."""
        tuples: List[CCDataTuple] = []
        for i in range(0, len(packet), 2):
            b1 = packet[i]
            b2 = packet[i + 1] if i + 1 < len(packet) else 0x00
            cc_type = _CC_DTVCC_START if i == 0 else _CC_DTVCC_DATA
            tuples.append((cc_type, b1, b2))
        return tuples

    def reset(self) -> None:
        self._seq = 0
        self._initialized = False

"""
Unit tests for CEA-608 and CEA-708 DTVCC byte structure.

These tests validate that the packetizers produce correct byte sequences
WITHOUT requiring any hardware or ASR models.  Run them with:

  python -m pytest tests/test_packetizer.py -v
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import unittest

from microcaption.caption.packetizer_608 import (
    add_parity, encode_text, CEA608Packetizer,
    FRAME_RU2, FRAME_CR, FRAME_EDM, FRAME_NULL,
)
from microcaption.caption.packetizer_708 import (
    DTVCC708Packetizer, define_window, build_service_block,
    CW0, RST, DF0,
)


class TestParity608(unittest.TestCase):

    def test_null_byte_has_parity(self):
        # 0x00 has 0 bits set (even) → bit 7 must be set → 0x80
        self.assertEqual(add_parity(0x00), 0x80)

    def test_ascii_H(self):
        # 'H' = 0x48 = 0b01001000, 2 bits set (even) → 0xC8
        self.assertEqual(add_parity(0x48), 0xC8)

    def test_ascii_space(self):
        # ' ' = 0x20 = 0b00100000, 1 bit set (odd) → stays 0x20
        self.assertEqual(add_parity(0x20), 0x20)

    def test_parity_bit_is_odd(self):
        for byte in range(0x00, 0x80):
            result = add_parity(byte)
            self.assertEqual(bin(result).count('1') % 2, 1,
                             f'Expected odd parity for input 0x{byte:02X}, got 0x{result:02X}')

    def test_parity_preserves_7_data_bits(self):
        for byte in range(0x00, 0x80):
            result = add_parity(byte)
            self.assertEqual(result & 0x7F, byte & 0x7F)


class TestControlFrames608(unittest.TestCase):

    def test_ru2_bytes(self):
        # CH1=0x14 → parity: 0b00010100 has 2 bits → 0x94
        # RU2=0x25 → parity: 0b00100101 has 3 bits (odd) → 0x25
        self.assertEqual(FRAME_RU2, (0x94, 0x25))

    def test_cr_bytes(self):
        # CR=0x2D → 0b00101101 has 4 bits (even) → 0xAD
        self.assertEqual(FRAME_CR, (0x94, 0xAD))

    def test_null_frame(self):
        self.assertEqual(FRAME_NULL, (0x80, 0x80))

    def test_all_control_bytes_have_odd_parity(self):
        for frame in (FRAME_RU2, FRAME_CR, FRAME_EDM, FRAME_NULL):
            for b in frame:
                self.assertEqual(bin(b).count('1') % 2, 1,
                                 f'Control byte 0x{b:02X} does not have odd parity')


class TestTextEncoding608(unittest.TestCase):

    def test_hello_world_length(self):
        frames = encode_text('Hello')
        # 5 chars → 3 frames (pair, pair, single+null)
        self.assertEqual(len(frames), 3)

    def test_even_length_no_null_pad(self):
        frames = encode_text('Hi')
        self.assertEqual(len(frames), 1)
        b1, b2 = frames[0]
        # 'H'=0x48 → 0b01001000, 2 bits (even) → 0xC8
        # 'i'=0x69 → 0b01101001, 4 bits (even) → 0xE9
        self.assertEqual(b1, 0xC8)
        self.assertEqual(b2, 0xE9)

    def test_text_bytes_have_odd_parity(self):
        frames = encode_text('Hello World')
        for b1, b2 in frames:
            for b in (b1, b2):
                self.assertEqual(bin(b).count('1') % 2, 1,
                                 f'Text byte 0x{b:02X} lacks odd parity')

    def test_non_ascii_replaced_with_question_mark(self):
        frames = encode_text('é')  # é (U+00E9, outside CEA-608 basic set)
        b1, _ = frames[0]
        self.assertEqual(b1 & 0x7F, ord('?'))


class TestCEA608Packetizer(unittest.TestCase):

    def test_first_call_includes_ru2(self):
        p = CEA608Packetizer({'rollup_rows': 2})
        frames = p.packetize(['Hello'])
        self.assertIn(FRAME_RU2, frames)

    def test_second_call_omits_ru2(self):
        p = CEA608Packetizer({'rollup_rows': 2})
        p.packetize(['Hello'])
        frames = p.packetize(['World'])
        self.assertNotIn(FRAME_RU2, frames)

    def test_cr_precedes_text(self):
        p = CEA608Packetizer({'rollup_rows': 2})
        frames = p.packetize(['Hi'])
        cr_idx = frames.index(FRAME_CR)
        # After CR there must be at least one text frame
        self.assertGreater(len(frames) - cr_idx, 1)

    def test_two_lines_two_crs(self):
        p = CEA608Packetizer({'rollup_rows': 2})
        frames = p.packetize(['Line one', 'Line two'])
        cr_count = sum(1 for f in frames if f == FRAME_CR)
        self.assertEqual(cr_count, 2)


class TestServiceBlock708(unittest.TestCase):

    def test_service_block_header_byte(self):
        data = b'\x80\x8F'  # CW0 + RST
        block = build_service_block(1, data)
        # service_number=1 → bits 7:5 = 0b001 = 0x20
        # block_size=2 → bits 4:0 = 0b00010 = 0x02
        self.assertEqual(block[0], 0x22)  # 0b00100010
        self.assertEqual(block[1:], data)

    def test_service_number_encoded_correctly(self):
        for svc in range(1, 7):
            block = build_service_block(svc, b'\x00')
            self.assertEqual((block[0] >> 5) & 0x07, svc)

    def test_block_size_encoded_correctly(self):
        for n in (1, 5, 10, 31):
            data = bytes(n)
            block = build_service_block(1, data)
            self.assertEqual(block[0] & 0x1F, n)

    def test_oversized_data_raises(self):
        with self.assertRaises(ValueError):
            build_service_block(1, bytes(32))


class TestDefineWindow708(unittest.TestCase):

    def test_command_byte_for_window_0(self):
        cmd = define_window(window_id=0)
        self.assertEqual(cmd[0], DF0)  # 0x98

    def test_command_byte_for_window_3(self):
        cmd = define_window(window_id=3)
        self.assertEqual(cmd[0], DF0 + 3)  # 0x9B

    def test_visible_flag_set(self):
        cmd = define_window(visible=True)
        # bit 2 of byte 1 should be 1
        self.assertTrue(cmd[1] & 0x04)

    def test_invisible_flag_clear(self):
        cmd = define_window(visible=False)
        self.assertFalse(cmd[1] & 0x04)

    def test_defines_window_length(self):
        cmd = define_window()
        # command byte + 6 param bytes = 7 total
        self.assertEqual(len(cmd), 7)

    def test_anchor_vertical_stored(self):
        cmd = define_window(anchor_v=74)
        self.assertEqual(cmd[2], 74)

    def test_row_count_minus_one(self):
        cmd = define_window(row_count=2)
        # byte 4 upper nibble = anchor_id (7), lower nibble = row_count-1 (1)
        self.assertEqual(cmd[4] & 0x0F, 1)  # 2-1=1

    def test_col_count_minus_one(self):
        cmd = define_window(col_count=32)
        self.assertEqual(cmd[5] & 0x3F, 31)  # 32-1=31


class TestDTVCC708Packetizer(unittest.TestCase):

    def setUp(self):
        self.p = DTVCC708Packetizer({'service': 1})

    def test_first_packet_contains_rst(self):
        raw, _ = self.p.packetize(['Hello'])
        # RST (0x8F) should be in the packet body (after header byte)
        self.assertIn(RST, raw[1:])

    def test_first_packet_contains_define_window(self):
        raw, _ = self.p.packetize(['Hello'])
        self.assertIn(DF0, raw[1:])

    def test_second_packet_no_rst(self):
        self.p.packetize(['Hello'])
        raw, _ = self.p.packetize(['World'])
        self.assertNotIn(RST, raw[1:])

    def test_packet_header_byte_structure(self):
        raw, _ = self.p.packetize(['Test'])
        # bits 7:6 = sequence_number (0 for first packet)
        # bits 5:0 = packet_size_code
        seq = (raw[0] >> 6) & 0x03
        size_code = raw[0] & 0x3F
        self.assertEqual(seq, 0)
        actual_bytes = (size_code + 1) * 2
        # Packet body length should match size_code
        self.assertEqual(len(raw) - 1, actual_bytes)

    def test_sequence_number_increments(self):
        seqs = []
        for _ in range(5):
            raw, _ = self.p.packetize(['x'])
            seqs.append((raw[0] >> 6) & 0x03)
        self.assertEqual(seqs, [0, 1, 2, 3, 0])

    def test_cc_data_start_type(self):
        _, cc = self.p.packetize(['Hi'])
        self.assertEqual(cc[0][0], 0x06)   # DTVCC_START

    def test_cc_data_continuation_type(self):
        _, cc = self.p.packetize(['Hi'])
        if len(cc) > 1:
            self.assertEqual(cc[1][0], 0x07)   # DTVCC_DATA

    def test_g0_text_in_packet(self):
        raw, _ = self.p.packetize(['AB'])
        # 'A'=0x41, 'B'=0x42 should appear in the packet body
        self.assertIn(0x41, raw)
        self.assertIn(0x42, raw)

    def test_reset_restores_initialization(self):
        self.p.packetize(['Hello'])
        self.p.reset()
        raw, _ = self.p.packetize(['World'])
        # After reset, RST and DF0 should reappear
        self.assertIn(RST, raw[1:])
        self.assertIn(DF0, raw[1:])


if __name__ == '__main__':
    unittest.main(verbosity=2)

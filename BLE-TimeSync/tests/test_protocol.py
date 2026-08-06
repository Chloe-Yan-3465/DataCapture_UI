from __future__ import annotations

import struct
import unittest

from app.protocol import (
    ProtocolError,
    ProtocolLengthError,
    pack_time_request,
    parse_status_response,
)


class ProtocolTests(unittest.TestCase):
    def test_request_is_exact_little_endian_payload(self) -> None:
        packet, timestamp_us = pack_time_request(1_700_000_000_123_456_789)
        self.assertEqual(len(packet), 12)
        self.assertEqual(struct.unpack("<QI", packet), (1_700_000_000, 123_456))
        self.assertEqual(timestamp_us, 1_700_000_000_123_456)

    def test_response_parses_signed_offset(self) -> None:
        packet = struct.pack("<QqQQ", 123, -456, 10_000, 10_250)
        status = parse_status_response(packet)
        self.assertEqual(status.phone_us, 123)
        self.assertEqual(status.offset_us, -456)
        self.assertEqual(status.esp_processing_us, 250)

    def test_response_rejects_short_and_oversized_packets(self) -> None:
        for packet in (bytes(31), bytes(33)):
            with self.subTest(length=len(packet)):
                with self.assertRaises(ProtocolLengthError):
                    parse_status_response(packet)

    def test_response_rejects_reversed_esp_timestamps(self) -> None:
        packet = struct.pack("<QqQQ", 123, 0, 20, 10)
        with self.assertRaises(ProtocolError):
            parse_status_response(packet)


if __name__ == "__main__":
    unittest.main()

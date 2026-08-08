from __future__ import annotations

import unittest

from app.coordinator_protocol import (
    CoordinatorProtocolError,
    TimeAccept,
    TimeError,
    TimeReply,
    encode_start,
    encode_stop,
    encode_time_query,
    encode_time_set,
    parse_coordinator_line,
)
from app.serial_client import TimeExchange


class CoordinatorProtocolTests(unittest.TestCase):
    def test_encodes_exact_current_firmware_commands(self) -> None:
        self.assertEqual(encode_time_query(7), b"TIME_QUERY 7\n")
        self.assertEqual(
            encode_time_set(7, 123456, 1_700_000_000_000_000_000, 250),
            b"TIME_SET 7 123456 1700000000000000000 250\n",
        )
        self.assertEqual(encode_start(), b"START\n")
        self.assertEqual(encode_stop(), b"STOP\n")

    def test_parses_reply_accept_and_error(self) -> None:
        reply = parse_coordinator_line("TIME_REPLY 7 1000 1010")
        accepted = parse_coordinator_line("TIME_ACCEPT seq=7 nodes=3 uncertainty_us=250")
        error = parse_coordinator_line("TIME_ERROR seq=7 node=2 BLE write failed")
        self.assertEqual(reply, TimeReply(7, 1000, 1010))
        self.assertEqual(accepted, TimeAccept(7, 3, 250))
        self.assertIsInstance(error, TimeError)
        self.assertEqual(error.sequence, 7)  # type: ignore[union-attr]

    def test_rejects_reversed_coordinator_timestamps(self) -> None:
        with self.assertRaises(CoordinatorProtocolError):
            parse_coordinator_line("TIME_REPLY 1 2000 1000")

    def test_exchange_computes_midpoint_mapping_and_uncertainty(self) -> None:
        exchange = TimeExchange(
            sequence=1,
            coordinator_receive_us=10_000,
            coordinator_transmit_us=10_200,
            t1_wall_ns=1_700_000_000_000_000_000,
            t1_monotonic_ns=1_000_000_000,
            t4_monotonic_ns=1_006_000_000,
        )
        self.assertEqual(exchange.coordinator_ref_us, 10_100)
        self.assertEqual(exchange.utc_ref_ns, 1_700_000_000_003_000_000)
        self.assertEqual(exchange.uncertainty_us, 2_900)


if __name__ == "__main__":
    unittest.main()

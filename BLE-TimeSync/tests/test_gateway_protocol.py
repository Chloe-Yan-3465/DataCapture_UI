from __future__ import annotations

import unittest

from app.gateway_protocol import (
    GatewayProtocolError,
    encode_start,
    encode_status,
    encode_stop,
    parse_gateway_message,
)


class GatewayProtocolTests(unittest.TestCase):
    def test_encodes_control_commands_without_terminators(self) -> None:
        self.assertEqual(encode_start(15), b"START+15")
        self.assertEqual(encode_stop(15), b"STOP+15")
        self.assertEqual(encode_status(), b"STATUS")

    def test_rejects_invalid_session_ids(self) -> None:
        for value in (-1, 0x1_0000_0000, True, "15"):
            with self.subTest(value=value):
                with self.assertRaises(GatewayProtocolError):
                    encode_start(value)  # type: ignore[arg-type]

    def test_parses_gateway_identity_state(self) -> None:
        message = parse_gateway_message(
            b"GW+DEVICE=68+STATE=RUNNING+SESSION=15+SYNC=YES+END"
        )
        self.assertEqual(message.kind, "gateway_state")
        self.assertEqual(message.device_id, "68")
        self.assertEqual(message.state, "RUNNING")
        self.assertEqual(message.session_id, 15)
        self.assertTrue(message.sync)

    def test_parses_forwarded_and_linux_ack(self) -> None:
        forwarded = parse_gateway_message("GW+START+SESSION=15+FORWARDED+END")
        ack = parse_gateway_message("ACK+STOP+SESSION=15+OK+FRAMES=12345+END")
        self.assertEqual(forwarded.kind, "forwarded")
        self.assertTrue(forwarded.success)
        self.assertEqual(ack.kind, "linux_ack")
        self.assertTrue(ack.success)
        self.assertEqual(ack.frames, 12345)

    def test_rejects_truncated_or_unknown_frames(self) -> None:
        for value in (b"GW+ERROR+BUSY", b"nonsense+END"):
            with self.subTest(value=value):
                with self.assertRaises(GatewayProtocolError):
                    parse_gateway_message(value)


if __name__ == "__main__":
    unittest.main()

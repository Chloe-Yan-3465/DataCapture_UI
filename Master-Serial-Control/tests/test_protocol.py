"""Master 串口协议编解码的离线单元测试。"""

from __future__ import annotations

import unittest

from app.protocol import (
    MasterProtocolError,
    disconnected_slaves,
    encode_start,
    encode_status,
    encode_stop,
    parse_master_message,
    slaves_connected,
)


class ProtocolTests(unittest.TestCase):
    def test_encodes_exact_serial_commands(self) -> None:
        self.assertEqual(encode_start(15), b"START+15\r\n")
        self.assertEqual(encode_stop(15), b"STOP+15\r\n")
        self.assertEqual(encode_status(), b"STATUS\r\n")

    def test_rejects_invalid_session_ids(self) -> None:
        for value in (-1, 0x1_0000_0000, True, "15"):
            with self.subTest(value=value):
                with self.assertRaises(MasterProtocolError):
                    encode_start(value)  # type: ignore[arg-type]

    def test_parses_start_ack(self) -> None:
        message = parse_master_message(
            "ACK+START+SESSION=15+OK+RATE=30+WIDTH_US=5000+END\r\n"
        )
        self.assertEqual(message.kind, "ack")
        self.assertEqual(message.command, "START")
        self.assertEqual(message.session_id, 15)
        self.assertTrue(message.success)
        self.assertEqual(message.fields["RATE"], "30")
        self.assertEqual(message.fields["WIDTH_US"], "5000")

    def test_parses_start_not_ready_error(self) -> None:
        message = parse_master_message(
            "ACK+START+SESSION=15+ERROR+SLAVES_NOT_READY+END"
        )
        self.assertFalse(message.success)
        self.assertEqual(message.error, "SLAVES_NOT_READY")

    def test_parses_stop_ack_and_already_stopped(self) -> None:
        normal = parse_master_message(
            "ACK+STOP+SESSION=15+OK+PULSES=1234+REASON=WINDOWS+END"
        )
        already = parse_master_message(
            "ACK+STOP+SESSION=15+OK+ALREADY_STOPPED+END"
        )
        self.assertTrue(normal.success)
        self.assertEqual(normal.pulses, 1234)
        self.assertEqual(normal.reason, "WINDOWS")
        self.assertEqual(already.status, "ALREADY_STOPPED")

    def test_parses_session_mismatch(self) -> None:
        message = parse_master_message(
            "ACK+STOP+SESSION=16+ERROR+SESSION_MISMATCH+CURRENT=15+END"
        )
        self.assertFalse(message.success)
        self.assertEqual(message.error, "SESSION_MISMATCH")
        self.assertEqual(message.fields["CURRENT"], "15")

    def test_parses_status_and_slave_readiness(self) -> None:
        message = parse_master_message(
            "STATUS+STATE=RUNNING+SESSION=15+RATE=30+WIDTH_US=5000+"
            "PULSES=1234+QUEUE_DROP=0+S68=CONNECTED+S68_TX=1235+S68_SKIP=0+"
            "S69=CONNECTED+S69_TX=1235+S69_SKIP=0+"
            "S70=CONNECTED+S70_TX=1235+S70_SKIP=0+END"
        )
        self.assertTrue(slaves_connected(message))
        self.assertEqual(disconnected_slaves(message), ())
        self.assertEqual(message.state, "RUNNING")
        self.assertEqual(message.session_id, 15)
        self.assertEqual(message.pulses, 1234)

    def test_reports_missing_slave(self) -> None:
        message = parse_master_message(
            "STATUS+STATE=IDLE+S68=CONNECTED+S69=DISCONNECTED+S70=CONNECTED+END"
        )
        self.assertFalse(slaves_connected(message))
        self.assertEqual(disconnected_slaves(message), ("69",))

    def test_parses_slave_events_and_generic_errors(self) -> None:
        connected = parse_master_message(
            "EVENT+SLAVE+DEVICE=68+CONNECTED+MTU=64+END"
        )
        disconnected = parse_master_message(
            "EVENT+SLAVE+DEVICE=69+DISCONNECTED+END"
        )
        error = parse_master_message("ACK+ERROR+INVALID_COMMAND+END")

        self.assertEqual(connected.event, "CONNECTED")
        self.assertEqual(connected.device_id, "68")
        self.assertEqual(disconnected.event, "DISCONNECTED")
        self.assertEqual(error.kind, "error")
        self.assertEqual(error.error, "INVALID_COMMAND")

    def test_rejects_truncated_and_unknown_frames(self) -> None:
        for value in (
            "ACK+START+SESSION=15+OK",
            "ACK+START+OK+END",
            "STATUS+S68=CONNECTED+END",
            "EVENT+SLAVE+DEVICE=71+CONNECTED+END",
            "nonsense+END",
        ):
            with self.subTest(value=value):
                with self.assertRaises(MasterProtocolError):
                    parse_master_message(value)


if __name__ == "__main__":
    unittest.main()

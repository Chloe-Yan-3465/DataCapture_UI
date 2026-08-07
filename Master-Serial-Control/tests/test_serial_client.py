"""Master 串口通信状态机的离线测试，使用内存假串口，不连接真实硬件。"""

from __future__ import annotations

from queue import Empty, Queue
import logging
import threading
import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH
from app.serial_client import (
    MasterNotReadyError,
    MasterSerialClient,
    SerialPortAmbiguousError,
    SerialPortInfo,
    resolve_serial_port,
)


class _FakeSerial:
    def __init__(self, on_write, **kwargs: object) -> None:
        self.on_write = on_write
        self.kwargs = kwargs
        self.is_open = True
        self.writes: list[bytes] = []
        self.rx: Queue[bytes] = Queue()
        self._cancel = threading.Event()

    def readline(self, size: int = -1) -> bytes:
        try:
            return self.rx.get(timeout=0.05)
        except Empty:
            return b""

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        self.on_write(self, bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def cancel_read(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        self.is_open = False


class SerialClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.logger = logging.getLogger("test.master-serial")
        self.fake: _FakeSerial | None = None

    def _client(self, on_write) -> MasterSerialClient:
        def factory(**kwargs: object) -> _FakeSerial:
            self.fake = _FakeSerial(on_write, **kwargs)
            return self.fake

        return MasterSerialClient(
            self.config,
            self.logger,
            serial_factory=factory,
            port_provider=lambda: [
                SerialPortInfo("COM9", "Fake Master", "FAKE")
            ],
        )

    def test_open_uses_fixed_115200_8n1(self) -> None:
        client = self._client(lambda _serial, _data: None)
        try:
            self.assertEqual(client.open(), "COM9")
            assert self.fake is not None
            self.assertEqual(self.fake.kwargs["baudrate"], 115200)
            self.assertEqual(self.fake.kwargs["bytesize"], 8)
            self.assertEqual(self.fake.kwargs["parity"], "N")
            self.assertEqual(self.fake.kwargs["stopbits"], 1)
        finally:
            client.close()

    def test_start_and_stop_use_one_session(self) -> None:
        def on_write(fake: _FakeSerial, data: bytes) -> None:
            if data == b"STATUS\r\n":
                fake.rx.put(
                    b"STATUS+STATE=IDLE+SESSION=0+RATE=30+WIDTH_US=5000+"
                    b"PULSES=0+QUEUE_DROP=0+S68=CONNECTED+S68_TX=0+S68_SKIP=0+"
                    b"S69=CONNECTED+S69_TX=0+S69_SKIP=0+"
                    b"S70=CONNECTED+S70_TX=0+S70_SKIP=0+END\r\n"
                )
            elif data.startswith(b"START+"):
                session = data.decode("ascii").strip().split("+", 1)[1]
                fake.rx.put(
                    f"ACK+START+SESSION={session}+OK+RATE=30+WIDTH_US=5000+END\r\n".encode()
                )
            elif data.startswith(b"STOP+"):
                session = data.decode("ascii").strip().split("+", 1)[1]
                fake.rx.put(
                    f"ACK+STOP+SESSION={session}+OK+PULSES=12+REASON=WINDOWS+END\r\n".encode()
                )

        client = self._client(on_write)
        try:
            client.open()
            start = client.start_capture(session_id=15)
            self.assertEqual(start.session_id, 15)
            self.assertEqual(client.active_session_id, 15)

            stop = client.stop_capture()
            self.assertEqual(stop.session_id, 15)
            self.assertIsNone(client.active_session_id)

            assert self.fake is not None
            self.assertEqual(
                self.fake.writes,
                [b"STATUS\r\n", b"START+15\r\n", b"STOP+15\r\n"],
            )
        finally:
            client.close()

    def test_unsolicited_status_does_not_replace_start_ack(self) -> None:
        def on_write(fake: _FakeSerial, data: bytes) -> None:
            if data == b"STATUS\r\n":
                fake.rx.put(
                    b"STATUS+STATE=IDLE+S68=CONNECTED+S69=CONNECTED+S70=CONNECTED+END\r\n"
                )
            elif data.startswith(b"START+"):
                session = data.decode("ascii").strip().split("+", 1)[1]
                fake.rx.put(
                    b"STATUS+STATE=IDLE+S68=CONNECTED+S69=CONNECTED+S70=CONNECTED+END\r\n"
                )
                fake.rx.put(
                    f"ACK+START+SESSION={session}+OK+RATE=30+WIDTH_US=5000+END\r\n".encode()
                )

        client = self._client(on_write)
        try:
            client.open()
            ack = client.start_capture(session_id=77)
            self.assertTrue(ack.success)
            self.assertEqual(ack.session_id, 77)
        finally:
            client.close()

    def test_start_rejects_missing_slave(self) -> None:
        def on_write(fake: _FakeSerial, data: bytes) -> None:
            if data == b"STATUS\r\n":
                fake.rx.put(
                    b"STATUS+STATE=IDLE+S68=CONNECTED+S69=DISCONNECTED+S70=CONNECTED+END\r\n"
                )

        client = self._client(on_write)
        try:
            client.open()
            with self.assertRaises(MasterNotReadyError):
                client.start_capture(session_id=15)
            assert self.fake is not None
            self.assertEqual(self.fake.writes, [b"STATUS\r\n"])
        finally:
            client.close()

    def test_auto_port_rejects_multiple_candidates(self) -> None:
        with self.assertRaises(SerialPortAmbiguousError):
            resolve_serial_port(
                "AUTO",
                lambda: [
                    SerialPortInfo("COM3", "A", "A"),
                    SerialPortInfo("COM4", "B", "B"),
                ],
            )


if __name__ == "__main__":
    unittest.main()

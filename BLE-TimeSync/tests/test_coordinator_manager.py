from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import patch

from app.config import AppConfig, DEFAULT_CONFIG_PATH
from app.coordinator_manager import CoordinatorManager


class _MemoryLog:
    def write(self, _record: object) -> None:
        pass


class _FakeClient:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.payloads: list[bytes] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def drain_lines(self) -> None:
        while not self.lines.empty():
            self.lines.get_nowait()

    async def send(self, payload: bytes) -> None:
        self.payloads.append(payload)
        if payload.startswith(b"START"):
            self.lines.put_nowait("session 123 planned; common epoch=1us, waiting for 2 Neo ACK(s)")
            self.lines.put_nowait("session 123 ARMED on connected nodes")
        elif payload == b"STOP\n":
            self.lines.put_nowait("STOP scheduled at coordinator=200us")

    async def wait_for_line(self, predicate: object, timeout: float) -> str:
        callback = predicate
        while True:
            line = await asyncio.wait_for(self.lines.get(), timeout)
            if callback(line):  # type: ignore[operator]
                return line


class _FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def synchronize(self, *, include_warmup: bool = False) -> None:
        self.calls += 1


class _FailingEngine:
    async def synchronize(self, *, include_warmup: bool = False) -> None:
        raise RuntimeError("sync failed")


class CoordinatorManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_does_not_wait_for_a_wearable_before_ready(self) -> None:
        manager = CoordinatorManager(
            AppConfig.load(DEFAULT_CONFIG_PATH),
            _MemoryLog(),  # type: ignore[arg-type]
            logging.getLogger("test.coordinator-manager.initialize"),
        )
        fake = _FakeClient()
        manager.client = fake  # type: ignore[assignment]
        manager.engine = _FailingEngine()  # type: ignore[assignment]

        await manager.initialize()
        self.assertTrue(fake.connected)
        self.assertEqual(fake.payloads, [b"STATUS\n"])
        self.assertIsNotNone(manager._sync_task)
        await manager.close()
        self.assertFalse(fake.connected)

    async def test_scan_start_and_stop_send_capture_metadata_without_session_ids(self) -> None:
        manager = CoordinatorManager(
            AppConfig.load(DEFAULT_CONFIG_PATH),
            _MemoryLog(),  # type: ignore[arg-type]
            logging.getLogger("test.coordinator-manager"),
        )
        fake = _FakeClient()
        fake_engine = _FakeEngine()
        manager.client = fake  # type: ignore[assignment]
        manager.engine = fake_engine  # type: ignore[assignment]

        with patch(
            "app.coordinator_manager.PRE_START_TIME_APPLY_GUARD_SECONDS", 0.0
        ):
            await manager.scan()
            self.assertTrue(await manager.start_all("limited_space", "L1"))
        self.assertEqual(manager.control_state, "RUNNING")
        self.assertTrue(await manager.stop_all())
        self.assertEqual(manager.control_state, "IDLE")
        self.assertEqual(
            fake.payloads,
            [b"SCAN\n", b"START TASK=limited_space LEVEL=L1\n", b"STOP\n"],
        )
        self.assertEqual(fake_engine.calls, 1)

    async def test_failed_pre_start_sync_never_sends_start(self) -> None:
        manager = CoordinatorManager(
            AppConfig.load(DEFAULT_CONFIG_PATH),
            _MemoryLog(),  # type: ignore[arg-type]
            logging.getLogger("test.coordinator-manager.failure"),
        )
        fake = _FakeClient()
        manager.client = fake  # type: ignore[assignment]
        manager.engine = _FailingEngine()  # type: ignore[assignment]

        with self.assertLogs("test.coordinator-manager.failure", level="ERROR"):
            self.assertFalse(await manager.start_all())
        self.assertEqual(manager.control_state, "ERROR")
        self.assertEqual(fake.payloads, [])


if __name__ == "__main__":
    unittest.main()

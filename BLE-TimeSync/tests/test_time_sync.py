from __future__ import annotations

import dataclasses
import logging
import unittest

from app.ble_client import ExchangeResult
from app.config import AppConfig, DEFAULT_CONFIG_PATH
from app.protocol import TimeStatus
from app.time_sync import TimeSyncEngine


class _MemorySessionLog:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(dict(record))


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    async def request_time(self, compensation_ms: float = 0.0) -> ExchangeResult:
        self.calls += 1
        rtt_ms = 8 + 2 * self.calls
        t1 = 1_000_000_000
        return ExchangeResult(
            status=TimeStatus(
                phone_us=123 + self.calls,
                offset_us=-250,
                esp_receive_us=5_000,
                esp_process_us=7_000,
            ),
            sent_phone_us=123 + self.calls,
            t1_wall_ns=1_700_000_000_000_000_000,
            t1_monotonic_ns=t1,
            t4_monotonic_ns=t1 + rtt_ms * 1_000_000,
            response_source="notify",
        )


class TimeSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_calibration_computes_both_methods_and_selects_median(self) -> None:
        config = dataclasses.replace(
            AppConfig.load(DEFAULT_CONFIG_PATH),
            calibration_warmup_samples=0,
            calibration_samples=3,
            calibration_interval_ms=1,
        )
        session = _MemorySessionLog()
        engine = TimeSyncEngine(_FakeClient(), config, session, logging.getLogger("test"))  # type: ignore[arg-type]

        selected = await engine.calibrate()

        self.assertAlmostEqual(engine.compatibility_compensation_ms, 6.0)
        self.assertAlmostEqual(engine.recommended_compensation_ms, 5.0)
        self.assertAlmostEqual(selected, 5.0)
        self.assertEqual(len(session.records), 3)
        self.assertTrue(all(record["success"] for record in session.records))

    async def test_warmup_samples_are_excluded_from_compensation(self) -> None:
        config = dataclasses.replace(
            AppConfig.load(DEFAULT_CONFIG_PATH),
            calibration_warmup_samples=2,
            calibration_samples=3,
            calibration_interval_ms=1,
        )
        session = _MemorySessionLog()
        client = _FakeClient()
        engine = TimeSyncEngine(client, config, session, logging.getLogger("test"))  # type: ignore[arg-type]

        selected = await engine.calibrate()

        self.assertEqual(client.calls, 5)
        self.assertEqual(len(session.records), 5)
        self.assertAlmostEqual(engine.recommended_compensation_ms, 7.0)
        self.assertAlmostEqual(selected, 7.0)


if __name__ == "__main__":
    unittest.main()

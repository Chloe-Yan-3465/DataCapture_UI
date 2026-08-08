from __future__ import annotations

import dataclasses
import logging
import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH
from app.coordinator_protocol import TimeAccept
from app.coordinator_time_sync import CoordinatorTimeSyncEngine
from app.serial_client import TimeExchange


class _MemorySessionLog:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(dict(record))


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.applied: TimeExchange | None = None

    async def request_time(self) -> TimeExchange:
        self.calls += 1
        rtt_us = {1: 9_000, 2: 5_000, 3: 7_000}[self.calls]
        return TimeExchange(
            sequence=self.calls,
            coordinator_receive_us=10_000 + self.calls * 100,
            coordinator_transmit_us=10_100 + self.calls * 100,
            t1_wall_ns=1_700_000_000_000_000_000,
            t1_monotonic_ns=1_000_000_000,
            t4_monotonic_ns=1_000_000_000 + rtt_us * 1_000,
        )

    async def apply_time(self, sample: TimeExchange) -> TimeAccept:
        self.applied = sample
        return TimeAccept(sample.sequence, 3, sample.uncertainty_us)


class CoordinatorTimeSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_lowest_net_rtt_sample_and_applies_it(self) -> None:
        config = dataclasses.replace(
            AppConfig.load(DEFAULT_CONFIG_PATH),
            calibration_warmup_samples=0,
            calibration_samples=3,
            calibration_interval_ms=1,
        )
        session = _MemorySessionLog()
        client = _FakeClient()
        engine = CoordinatorTimeSyncEngine(
            client,  # type: ignore[arg-type]
            config,
            session,  # type: ignore[arg-type]
            logging.getLogger("test.coordinator-time-sync"),
        )

        selected = await engine.synchronize()

        self.assertEqual(selected.sequence, 2)
        self.assertIs(client.applied, selected)
        self.assertEqual(len(session.records), 3)
        self.assertEqual(sum(bool(row["selected"]) for row in session.records), 1)
        selected_row = next(row for row in session.records if row["selected"])
        self.assertTrue(selected_row["applied"])
        self.assertEqual(selected_row["accepted_nodes"], 3)


if __name__ == "__main__":
    unittest.main()

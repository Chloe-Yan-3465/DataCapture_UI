"""NTP-style host/coordinator time mapping over the coordinator serial link."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

from .config import AppConfig
from .logging_utils import SessionLog
from .serial_client import CoordinatorSerialClient, TimeExchange


class CoordinatorTimeSyncEngine:
    def __init__(
        self,
        client: CoordinatorSerialClient,
        config: AppConfig,
        session_log: SessionLog,
        logger: logging.Logger,
    ) -> None:
        self.client = client
        self.config = config
        self.session_log = session_log
        self.logger = logger
        self.success_count = 0
        self.failure_count = 0

    async def synchronize(self, *, include_warmup: bool = False) -> TimeExchange:
        if include_warmup:
            for index in range(self.config.calibration_warmup_samples):
                await self.client.request_time()
                if index + 1 < self.config.calibration_warmup_samples:
                    await asyncio.sleep(self.config.calibration_interval_ms / 1_000)

        samples: list[TimeExchange] = []
        try:
            for index in range(self.config.calibration_samples):
                samples.append(await self.client.request_time())
                if index + 1 < self.config.calibration_samples:
                    await asyncio.sleep(self.config.calibration_interval_ms / 1_000)

            selected = min(samples, key=lambda item: item.net_rtt_us)
            accepted = await self.client.apply_time(selected)
            self.success_count += 1
            self.logger.info(
                "Time sync accepted: seq=%d nodes=%d RTT=%.3f ms net_RTT=%.3f ms "
                "uncertainty=%d us success/failure=%d/%d",
                selected.sequence,
                accepted.nodes,
                selected.total_rtt_us / 1_000,
                selected.net_rtt_us / 1_000,
                accepted.uncertainty_us,
                self.success_count,
                self.failure_count,
            )
            self._write_samples(samples, selected, accepted.nodes, True, "")
            return selected
        except Exception as exc:
            self.failure_count += 1
            if samples:
                selected = min(samples, key=lambda item: item.net_rtt_us)
                self._write_samples(
                    samples,
                    selected,
                    None,
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            raise

    def _write_samples(
        self,
        samples: list[TimeExchange],
        selected: TimeExchange,
        accepted_nodes: int | None,
        applied: bool,
        error: str,
    ) -> None:
        for sample in samples:
            is_selected = sample is selected
            record: dict[str, Any] = {
                "coordinator": self.config.coordinator_name,
                "sequence": sample.sequence,
                "windows_utc": datetime.fromtimestamp(
                    sample.t1_wall_ns / 1_000_000_000, tz=timezone.utc
                )
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                "t1_wall_ns": sample.t1_wall_ns,
                "t1_monotonic_ns": sample.t1_monotonic_ns,
                "t4_monotonic_ns": sample.t4_monotonic_ns,
                "coordinator_receive_us": sample.coordinator_receive_us,
                "coordinator_transmit_us": sample.coordinator_transmit_us,
                "rtt_ms": sample.total_rtt_us / 1_000,
                "coordinator_processing_ms": sample.coordinator_processing_us / 1_000,
                "net_rtt_ms": sample.net_rtt_us / 1_000,
                "coordinator_ref_us": sample.coordinator_ref_us,
                "utc_ref_ns": sample.utc_ref_ns,
                "uncertainty_us": sample.uncertainty_us,
                "selected": is_selected,
                "applied": applied if is_selected else False,
                "accepted_nodes": accepted_nodes if is_selected else "",
                "success": applied if is_selected else True,
                "error": error if is_selected else "",
            }
            self.session_log.write(record)

"""Windows BLE 授时过程中的延迟测量、补偿计算与日志记录模块。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from statistics import median
from typing import Any

from .ble_client import BleTimeClient
from .config import AppConfig
from .logging_utils import SessionLog


class TimeSyncEngine:
    def __init__(
        self,
        client: BleTimeClient,
        config: AppConfig,
        session_log: SessionLog,
        logger: logging.Logger,
        device_id: str = "",
    ) -> None:
        self.client = client
        self.config = config
        self.session_log = session_log
        self.logger = logger
        self.device_id = device_id
        self.sequence = 0
        self.success_count = 0
        self.failure_count = 0
        self.compatibility_compensation_ms = 0.0
        self.recommended_compensation_ms = 0.0

    async def perform_sync(self, compensation_ms: float) -> dict[str, Any]:
        self.sequence += 1
        record: dict[str, Any] = {
            "device_id": self.device_id,
            "sequence": self.sequence,
            "windows_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "sent_phone_us": "",
            "t1_wall_ns": "",
            "t1_monotonic_ns": "",
            "t4_monotonic_ns": "",
            "rtt_ms": "",
            "esp_processing_ms": "",
            "net_rtt_ms": "",
            "estimated_one_way_ms": "",
            "compensation_ms": compensation_ms,
            "esp_offset_us": "",
            "success": False,
            "error": "",
        }
        try:
            exchange = await self.client.request_time(compensation_ms)
            status = exchange.status
            rtt_ms = (exchange.t4_monotonic_ns - exchange.t1_monotonic_ns) / 1_000_000
            esp_processing_ms = status.esp_processing_us / 1_000
            net_rtt_ms = rtt_ms - esp_processing_ms
            one_way_ms = max(net_rtt_ms / 2, 0.0)
            record.update(
                {
                    "windows_utc": datetime.fromtimestamp(
                        exchange.t1_wall_ns / 1_000_000_000, tz=timezone.utc
                    )
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                    "sent_phone_us": exchange.sent_phone_us,
                    "t1_wall_ns": exchange.t1_wall_ns,
                    "t1_monotonic_ns": exchange.t1_monotonic_ns,
                    "t4_monotonic_ns": exchange.t4_monotonic_ns,
                    "rtt_ms": rtt_ms,
                    "esp_processing_ms": esp_processing_ms,
                    "net_rtt_ms": net_rtt_ms,
                    "estimated_one_way_ms": one_way_ms,
                    "esp_offset_us": status.offset_us,
                    "success": True,
                    "response_source": exchange.response_source,
                }
            )
            self.success_count += 1
            self.logger.info(
                "[%s] sync #%d OK: RTT=%.3f ms, one-way=%.3f ms, compensation=%.3f ms, "
                "ESP offset=%d us, success/failure=%d/%d",
                self.device_id or "single",
                self.sequence,
                rtt_ms,
                one_way_ms,
                compensation_ms,
                status.offset_us,
                self.success_count,
                self.failure_count,
            )
        except Exception as exc:
            self.failure_count += 1
            record["error"] = f"{type(exc).__name__}: {exc}"
            self.logger.exception(
                "[%s] sync #%d FAILED: compensation=%.3f ms, success/failure=%d/%d",
                self.device_id or "single",
                self.sequence,
                compensation_ms,
                self.success_count,
                self.failure_count,
            )
        self.session_log.write(record)
        return record

    async def calibrate(self) -> float:
        self.logger.info(
            "[%s] Starting calibration: %d warm-up samples plus %d measured samples, %d ms interval",
            self.device_id or "single",
            self.config.calibration_warmup_samples,
            self.config.calibration_samples,
            self.config.calibration_interval_ms,
        )

        for index in range(self.config.calibration_warmup_samples):
            await self.perform_sync(0.0)
            if index + 1 < self.config.calibration_warmup_samples or self.config.calibration_samples:
                await asyncio.sleep(self.config.calibration_interval_ms / 1_000)

        if self.config.calibration_warmup_samples:
            self.logger.info(
                "[%s] Warm-up completed; the preceding %d result(s) are excluded from compensation statistics",
                self.device_id or "single",
                self.config.calibration_warmup_samples,
            )

        successful: list[dict[str, Any]] = []
        for index in range(self.config.calibration_samples):
            record = await self.perform_sync(0.0)
            if record["success"]:
                successful.append(record)
            if index + 1 < self.config.calibration_samples:
                await asyncio.sleep(self.config.calibration_interval_ms / 1_000)

        if not successful:
            raise RuntimeError("Calibration failed: no valid responses were received")
        total_rtts = [float(record["rtt_ms"]) for record in successful]
        one_way_delays = [float(record["estimated_one_way_ms"]) for record in successful]
        self.compatibility_compensation_ms = sum(total_rtts) / len(total_rtts) / 2
        self.recommended_compensation_ms = median(one_way_delays)
        selected = (
            self.recommended_compensation_ms
            if self.config.compensation_method == "net_rtt_median"
            else self.compatibility_compensation_ms
        )
        self.logger.info(
            "[%s] Calibration completed with %d/%d valid samples: compatible mean RTT/2=%.3f ms, "
            "recommended median net one-way=%.3f ms, selected=%.3f ms (%s)",
            self.device_id or "single",
            len(successful),
            self.config.calibration_samples,
            self.compatibility_compensation_ms,
            self.recommended_compensation_ms,
            selected,
            self.config.compensation_method,
        )
        return selected

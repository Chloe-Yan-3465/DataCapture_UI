"""Lifecycle, periodic UTC synchronization, and START/STOP control."""

from __future__ import annotations

import asyncio
import logging

from .config import AppConfig
from .coordinator_protocol import (
    encode_abort,
    encode_scan,
    encode_start,
    encode_status,
    encode_stop,
)
from .coordinator_time_sync import CoordinatorTimeSyncEngine
from .logging_utils import SessionLog
from .serial_client import CoordinatorSerialClient


# ESP32 kTimePlanLeadUs is 1.5 seconds. This guard lets the wearable deliver
# the scheduled UART TIMESYNC while all NanoPi capture processes are still IDLE.
PRE_START_TIME_APPLY_GUARD_SECONDS = 2.0


def _is_start_success_line(value: str) -> bool:
    """Accept the coordinator's final ARMED line for any connected node count."""
    fields = value.strip().split()
    return (
        len(fields) >= 3
        and fields[0].casefold() == "session"
        and any(field.upper() == "ARMED" for field in fields[2:])
    )


class CoordinatorManager:
    def __init__(
        self,
        config: AppConfig,
        session_log: SessionLog,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.logger = logger
        self.client = CoordinatorSerialClient(config, logger)
        self.engine = CoordinatorTimeSyncEngine(
            self.client, config, session_log, logger
        )
        self.control_state = "IDLE"
        self._control_lock = asyncio.Lock()
        self._time_sync_lock = asyncio.Lock()
        self._sync_task: asyncio.Task[None] | None = None
        self._closing = False

    async def _synchronize(self, *, include_warmup: bool = False) -> None:
        async with self._time_sync_lock:
            await self.engine.synchronize(include_warmup=include_warmup)

    async def initialize(self) -> None:
        await self.client.connect()
        await self.client.send(encode_status())
        if self._closing:
            return
        self._sync_task = asyncio.create_task(
            self._periodic_sync(), name="mode2-periodic-timesync"
        )
        print(
            f"\n[READY] {self.config.coordinator_name} via "
            f"{self.config.serial_port}@{self.config.baud_rate}"
        )
        print(
            "Press S to SCAN, 1 to START, 0 to STOP, Ctrl+C to exit. "
            "No Enter is required.\n"
        )

    async def _periodic_sync(self) -> None:
        first_attempt = True
        while not self._closing:
            try:
                await self._synchronize(include_warmup=first_attempt)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception(
                    "Initial coordinator time sync failed"
                    if first_attempt
                    else "Periodic coordinator time sync failed"
                )
            first_attempt = False
            await asyncio.sleep(self.config.sync_interval_seconds)

    async def scan(self) -> None:
        await self.client.send(encode_scan())
        print("\n[SCAN] Wearable discovery requested.\n", flush=True)

    async def start_all(
        self,
        task_name: str | None = None,
        complex_level: str | None = None,
    ) -> bool:
        async with self._control_lock:
            if self.control_state in {"SYNCING", "STARTING", "RUNNING"}:
                self.logger.warning("START ignored while state=%s", self.control_state)
                return False
            self.control_state = "SYNCING"
            print("\n[START] Refreshing NanoPi UTC before this episode...")
            try:
                await self._synchronize()
                await asyncio.sleep(PRE_START_TIME_APPLY_GUARD_SECONDS)
            except Exception:
                self.control_state = "ERROR"
                self.logger.exception("START rejected because pre-start time sync failed")
                return False
            self.control_state = "STARTING"
            self.client.drain_lines()
            await self.client.send(encode_start(task_name, complex_level))
            try:
                line = await self.client.wait_for_line(
                    lambda value: (
                        _is_start_success_line(value)
                        or value.startswith("START rejected:")
                        or value.startswith("START aborted:")
                    ),
                    self.config.control_ack_timeout_seconds,
                )
            except Exception:
                self.control_state = "ERROR"
                self.logger.exception("START did not receive a final coordinator result")
                return False
            if _is_start_success_line(line):
                self.control_state = "RUNNING"
                print(
                    "\n[START] Connected wearable nodes armed; "
                    "synchronized capture scheduled.\n"
                )
                return True
            self.control_state = "ERROR"
            print(f"\n[START FAILED] {line}\n")
            return False

    async def stop_all(self) -> bool:
        async with self._control_lock:
            self.client.drain_lines()
            await self.client.send(encode_stop())
            try:
                line = await self.client.wait_for_line(
                    lambda value: (
                        value.startswith("STOP scheduled at coordinator=")
                        or value == "no active session"
                    ),
                    self.config.control_ack_timeout_seconds,
                )
            except Exception:
                self.control_state = "ERROR"
                self.logger.exception("STOP did not receive a coordinator result")
                return False
            if line.startswith("STOP scheduled at coordinator="):
                self.control_state = "IDLE"
                print(f"\n[STOP] {line}\n")
                return True
            self.control_state = "IDLE"
            print("\n[STOP] Coordinator reports no active session.\n")
            return True

    async def abort_all(self) -> None:
        await self.client.send(encode_abort())
        self.control_state = "IDLE"

    async def close(self) -> None:
        self._closing = True
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
        self._sync_task = None
        await self.client.disconnect()

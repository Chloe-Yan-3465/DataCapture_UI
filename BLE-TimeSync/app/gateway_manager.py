"""管理 Windows 到 68/69/70 三台 Slave 的 BLE 连接、首次授时、周期授时与断线重连。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from .ble_client import BleTimeClient, ScanResult, TargetNotFoundError
from .config import AppConfig
from .logging_utils import SessionLog
from .time_sync import TimeSyncEngine


@dataclass(slots=True)
class GatewayRuntime:
    device_id: str
    device_name: str
    client: BleTimeClient
    engine: TimeSyncEngine
    compensation_ms: float = 0.0
    synchronized: bool = False
    sync_task: asyncio.Task[None] | None = None
    reconnect_lock: asyncio.Lock | None = None


class GatewayManager:
    def __init__(
        self,
        config: AppConfig,
        session_log: SessionLog,
        logger: logging.Logger,
        *,
        no_compensation: bool = False,
    ) -> None:
        self.config = config
        self.session_log = session_log
        self.logger = logger
        self.no_compensation = no_compensation
        self.gateways: dict[str, GatewayRuntime] = {}
        self._scan_lock = asyncio.Lock()
        self._closing = False
        self._missing_gateway_task: asyncio.Task[None] | None = None
        self._ready_announced = False

    @property
    def ready_gateways(self) -> dict[str, GatewayRuntime]:
        return {
            device_id: runtime
            for device_id, runtime in self.gateways.items()
            if runtime.client.is_connected and runtime.synchronized
        }

    @property
    def offline_gateway_ids(self) -> list[str]:
        return sorted(set(self.config.gateways) - set(self.ready_gateways))

    @property
    def all_gateways_ready(self) -> bool:
        return not self.offline_gateway_ids

    async def _scan_gateway_devices(self) -> dict[str, ScanResult]:
        async with self._scan_lock:
            results = await BleTimeClient.scan(self.config, self.logger)

        devices: dict[str, ScanResult] = {}
        for device_id, expected_name in self.config.gateways.items():
            match = next(
                (
                    item
                    for item in results
                    if item.name.casefold() == expected_name.casefold()
                ),
                None,
            )
            if match is not None:
                devices[device_id] = match

        if not devices:
            raise TargetNotFoundError(
                "No configured Slave was found. Expected BLE names: 68, 69, 70"
            )
        return devices

    async def _scan_gateway_device(self, device_id: str) -> ScanResult:
        expected_name = self.config.gateways[device_id]
        async with self._scan_lock:
            results = await BleTimeClient.scan(self.config, self.logger)

        match = next(
            (
                item
                for item in results
                if item.name.casefold() == expected_name.casefold()
            ),
            None,
        )
        if match is None:
            raise TargetNotFoundError(f"Required Slave not found: {expected_name}")
        return match

    async def initialize(self) -> None:
        await self.disconnect_all()
        self._closing = False
        self._ready_announced = False

        devices = await self._scan_gateway_devices()
        for device_id in sorted(devices):
            try:
                await self._connect_gateway(device_id, devices[device_id])
            except Exception:
                self.logger.exception("[%s] initial connection failed; continuing", device_id)
                await self._remove_gateway(device_id)

        connected_ids = sorted(self.gateways)
        for index, device_id in enumerate(connected_ids):
            try:
                await self._calibrate_gateway(self.gateways[device_id])
            except Exception:
                self.logger.exception("[%s] initial time sync failed; continuing", device_id)
                await self._remove_gateway(device_id)
            if index + 1 < len(connected_ids):
                await asyncio.sleep(self.config.sync_stagger_ms / 1_000)

        if not self.ready_gateways:
            raise RuntimeError("No Slave completed BLE connection and initial time sync")

        self._start_periodic_sync_tasks()
        self._start_missing_gateway_monitor()
        self._report_readiness()

    async def _connect_gateway(
        self,
        device_id: str,
        scan_result: ScanResult,
    ) -> GatewayRuntime:
        expected_name = self.config.gateways[device_id]
        client = BleTimeClient(self.config, self.logger, gateway_id=device_id)
        engine = TimeSyncEngine(
            client,
            self.config,
            self.session_log,
            self.logger,
            device_id=device_id,
        )
        runtime = GatewayRuntime(
            device_id=device_id,
            device_name=expected_name,
            client=client,
            engine=engine,
            reconnect_lock=asyncio.Lock(),
        )
        self.gateways[device_id] = runtime
        await client.connect(scan_result.device)
        self.logger.info(
            "[%s] connected for Windows UTC time sync at address=%s",
            device_id,
            scan_result.address,
        )
        return runtime

    async def _calibrate_gateway(self, runtime: GatewayRuntime) -> None:
        runtime.synchronized = False
        measured = await runtime.engine.calibrate()
        runtime.compensation_ms = 0.0 if self.no_compensation else measured
        runtime.synchronized = True

        if self.no_compensation:
            self.logger.info(
                "[%s] measured compensation will not be applied", runtime.device_id
            )

        self.logger.info(
            "[%s] initial time sync completed: compensation=%.3f ms",
            runtime.device_id,
            runtime.compensation_ms,
        )

    def _start_periodic_sync_tasks(self) -> None:
        ordered_ids = sorted(self.config.gateways)
        for device_id in sorted(self.gateways):
            runtime = self.gateways[device_id]
            if runtime.sync_task is None or runtime.sync_task.done():
                index = ordered_ids.index(device_id)
                initial_delay = (
                    self.config.sync_interval_seconds
                    + index * self.config.sync_stagger_ms / 1_000
                )
                runtime.sync_task = asyncio.create_task(
                    self._periodic_sync(runtime, initial_delay),
                    name=f"gateway-{device_id}-timesync",
                )

    def _start_missing_gateway_monitor(self) -> None:
        if self._missing_gateway_task is None or self._missing_gateway_task.done():
            self._missing_gateway_task = asyncio.create_task(
                self._monitor_missing_gateways(),
                name="missing-gateway-monitor",
            )

    def _report_readiness(self) -> None:
        online = ", ".join(sorted(self.ready_gateways)) or "none"
        offline = ", ".join(self.offline_gateway_ids) or "none"

        if self.all_gateways_ready:
            self.logger.info(
                "BLE time-sync ready: online=%s offline=%s",
                online,
                offline,
            )
            if not self._ready_announced:
                print(f"\n[READY] Online gateways: {online}", flush=True)
                print(f"[OFFLINE] Gateways: {offline}", flush=True)
                print(
                    "Windows BLE now performs UTC time sync only; START/STOP is handled by Master-Serial-Control.\n",
                    flush=True,
                )
                self._ready_announced = True
            return

        self._ready_announced = False
        self.logger.info(
            "BLE time-sync waiting for missing Slave(s): online=%s offline=%s",
            online,
            offline,
        )
        print(f"\n[WAIT] Online gateways: {online}", flush=True)
        print(f"[OFFLINE] Gateways: {offline}\n", flush=True)

    async def _monitor_missing_gateways(self) -> None:
        while not self._closing:
            missing = sorted(set(self.config.gateways) - set(self.gateways))
            if not missing:
                await asyncio.sleep(self.config.reconnect_delay_seconds)
                continue

            self.logger.info(
                "Background discovery for offline Slave(s): %s",
                ", ".join(missing),
            )
            try:
                async with self._scan_lock:
                    results = await BleTimeClient.scan(self.config, self.logger)

                for device_id in missing:
                    expected_name = self.config.gateways[device_id]
                    match = next(
                        (
                            item
                            for item in results
                            if item.name.casefold() == expected_name.casefold()
                        ),
                        None,
                    )
                    if match is None or device_id in self.gateways:
                        continue

                    try:
                        runtime = await self._connect_gateway(device_id, match)
                        await self._calibrate_gateway(runtime)
                        self._start_periodic_sync_tasks()
                        self.logger.info(
                            "[%s] came online and joined Windows UTC time sync",
                            device_id,
                        )
                        print(
                            f"\n[ONLINE] Slave {device_id} connected and synchronized",
                            flush=True,
                        )
                        self._report_readiness()
                    except Exception:
                        self.logger.exception(
                            "[%s] background connection/time-sync failed", device_id
                        )
                        await self._remove_gateway(device_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Background Slave discovery failed")

            await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _periodic_sync(
        self,
        runtime: GatewayRuntime,
        initial_delay: float,
    ) -> None:
        await asyncio.sleep(initial_delay)
        while not self._closing:
            if not runtime.client.is_connected:
                runtime.synchronized = False
                self._ready_announced = False
                await self._recover_gateway(runtime)
                continue

            await runtime.engine.perform_sync(runtime.compensation_ms)
            try:
                await asyncio.wait_for(
                    runtime.client.disconnected_event.wait(),
                    timeout=self.config.sync_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _recover_gateway(self, runtime: GatewayRuntime) -> None:
        if runtime.reconnect_lock is None:
            runtime.reconnect_lock = asyncio.Lock()

        async with runtime.reconnect_lock:
            while not self._closing and not runtime.client.is_connected:
                runtime.synchronized = False
                self.logger.info(
                    "[%s] reconnecting in %.1f seconds",
                    runtime.device_id,
                    self.config.reconnect_delay_seconds,
                )
                await asyncio.sleep(self.config.reconnect_delay_seconds)
                try:
                    await runtime.client.disconnect()
                    device = await self._scan_gateway_device(runtime.device_id)
                    await runtime.client.connect(device.device)
                    await self._calibrate_gateway(runtime)
                    self.logger.info(
                        "[%s] reconnected and UTC time sync restored",
                        runtime.device_id,
                    )
                    self._report_readiness()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("[%s] reconnect failed", runtime.device_id)

    async def _remove_gateway(self, device_id: str) -> None:
        runtime = self.gateways.pop(device_id, None)
        if runtime is None:
            return

        current = asyncio.current_task()
        if (
            runtime.sync_task is not None
            and runtime.sync_task is not current
            and not runtime.sync_task.done()
        ):
            runtime.sync_task.cancel()
            await asyncio.gather(runtime.sync_task, return_exceptions=True)

        runtime.synchronized = False
        await runtime.client.disconnect()
        self._ready_announced = False

    async def disconnect_all(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        if self._missing_gateway_task is not None and not self._missing_gateway_task.done():
            self._missing_gateway_task.cancel()
            tasks.append(self._missing_gateway_task)
        self._missing_gateway_task = None

        for runtime in self.gateways.values():
            if runtime.sync_task is not None and not runtime.sync_task.done():
                runtime.sync_task.cancel()
                tasks.append(runtime.sync_task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await asyncio.gather(
            *(runtime.client.disconnect() for runtime in self.gateways.values()),
            return_exceptions=True,
        )
        self.gateways.clear()
        self._ready_announced = False

    async def close(self) -> None:
        self._closing = True
        await self.disconnect_all()

"""Three-gateway lifecycle, time synchronization, and camera control."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any

from .ble_client import BleTimeClient, ScanResult, TargetNotFoundError
from .config import AppConfig
from .gateway_protocol import (
    GatewayMessage,
    encode_start,
    encode_status,
    encode_stop,
)
from .logging_utils import SessionLog
from .time_sync import TimeSyncEngine


@dataclass(slots=True)
class GatewayRuntime:
    device_id: str
    device_name: str
    client: BleTimeClient
    engine: TimeSyncEngine
    compensation_ms: float = 0.0
    identity: GatewayMessage | None = None
    sync_task: asyncio.Task[None] | None = None
    reconnect_lock: asyncio.Lock | None = None


@dataclass(frozen=True, slots=True)
class ControlResult:
    device_id: str
    command: str
    session_id: int
    forwarded: bool
    success: bool
    frames: int | None = None
    error: str | None = None


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
        self.control_state = "IDLE"
        self.current_session_id: int | None = None
        self._control_lock = asyncio.Lock()
        self._scan_lock = asyncio.Lock()
        self._closing = False
        self._missing_gateway_task: asyncio.Task[None] | None = None

    @property
    def ready_gateways(self) -> dict[str, GatewayRuntime]:
        return {
            device_id: runtime
            for device_id, runtime in self.gateways.items()
            if runtime.client.is_connected
            and runtime.identity is not None
            and runtime.identity.sync is True
        }

    @property
    def offline_gateway_ids(self) -> list[str]:
        return sorted(set(self.config.gateways) - set(self.ready_gateways))

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
            raise TargetNotFoundError("No configured ESP32S3 gateway was found")
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
            raise TargetNotFoundError(f"Required gateway not found: {expected_name}")
        return match

    async def initialize(self) -> None:
        await self.disconnect_all()
        devices = await self._scan_gateway_devices()
        for device_id in sorted(devices):
            try:
                await self._connect_gateway(device_id, devices[device_id])
            except Exception:
                self.logger.exception("[%s] initial connection failed; continuing", device_id)
                await self._remove_gateway(device_id)
        for device_id in sorted(list(self.gateways)):
            try:
                await self._calibrate_gateway(self.gateways[device_id])
            except Exception:
                self.logger.exception("[%s] initial calibration failed; continuing", device_id)
                await self._remove_gateway(device_id)
        if not self.ready_gateways:
            raise RuntimeError("No gateway completed connection and calibration")
        self._start_periodic_sync_tasks()
        self._start_missing_gateway_monitor()
        online = ", ".join(sorted(self.ready_gateways))
        offline = ", ".join(self.offline_gateway_ids) or "none"
        self.logger.info(
            "Gateway controller ready: online=%s offline=%s. Press 1 to START, 0 to STOP, Ctrl+C to exit.",
            online,
            offline,
        )
        print(f"\n[READY] Online gateways: {online}")
        print(f"[OFFLINE] Gateways: {offline}")
        print("Press 1 to START, 0 to STOP, Ctrl+C to exit. No Enter is required.\n")

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
        runtime.identity = await self._verify_identity(runtime)
        self.logger.info(
            "[%s] connected and identified at address=%s",
            device_id,
            scan_result.address,
        )
        return runtime

    async def _calibrate_gateway(self, runtime: GatewayRuntime) -> None:
        measured = await runtime.engine.calibrate()
        runtime.compensation_ms = 0.0 if self.no_compensation else measured
        if self.no_compensation:
            self.logger.info(
                "[%s] measured compensation will not be applied", runtime.device_id
            )
        runtime.identity = await self._verify_identity(runtime, force_query=True)
        self.logger.info(
            "[%s] calibrated and control-ready: compensation=%.3f ms",
            runtime.device_id,
            runtime.compensation_ms,
        )

    async def _verify_identity(
        self,
        runtime: GatewayRuntime,
        *,
        force_query: bool = False,
    ) -> GatewayMessage:
        client = runtime.client
        message: GatewayMessage | None = None
        if not force_query:
            try:
                candidate = await client.read_gateway_status()
                if candidate.kind == "gateway_state":
                    message = candidate
            except Exception:
                self.logger.info(
                    "[%s] Gateway status read unavailable; querying STATUS",
                    runtime.device_id,
                )
        if message is None:
            client.drain_gateway_messages()
            await client.send_control(encode_status())
            message = await client.wait_for_gateway_message(
                lambda item: item.kind == "gateway_state",
                self.config.control_ack_timeout_seconds,
            )
        if message.device_id != runtime.device_id:
            raise RuntimeError(
                f"Gateway identity mismatch: expected {runtime.device_id}, got {message.device_id}"
            )
        if message.session_id is not None and message.session_id > 0:
            if self.current_session_id is None:
                self.current_session_id = message.session_id
            elif self.current_session_id != message.session_id:
                self.logger.warning(
                    "Gateway sessions differ: current=%s [%s]=%s",
                    self.current_session_id,
                    runtime.device_id,
                    message.session_id,
                )
        return message

    def _start_periodic_sync_tasks(self) -> None:
        ordered_ids = sorted(self.config.gateways)
        for device_id in sorted(self.gateways):
            runtime = self.gateways[device_id]
            if runtime.sync_task is None or runtime.sync_task.done():
                index = ordered_ids.index(device_id)
                initial_delay = index * self.config.sync_stagger_ms / 1_000
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

    async def _monitor_missing_gateways(self) -> None:
        while not self._closing:
            missing = sorted(set(self.config.gateways) - set(self.gateways))
            if not missing:
                await asyncio.sleep(self.config.reconnect_delay_seconds)
                continue
            self.logger.info(
                "Background discovery for offline gateway(s): %s",
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
                            "[%s] came online and is now eligible for future controls; previous START was not replayed",
                            device_id,
                        )
                        print(f"\n[ONLINE] Gateway {device_id} connected and calibrated")
                    except Exception:
                        self.logger.exception(
                            "[%s] background connection/calibration failed", device_id
                        )
                        await self._remove_gateway(device_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Background gateway discovery failed")
            await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _periodic_sync(
        self,
        runtime: GatewayRuntime,
        initial_delay: float,
    ) -> None:
        await asyncio.sleep(initial_delay)
        while not self._closing:
            if not runtime.client.is_connected:
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
                    runtime.identity = await self._verify_identity(runtime)
                    await self._calibrate_gateway(runtime)
                    self.logger.info(
                        "[%s] reconnected; STATUS restored without replaying START",
                        runtime.device_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("[%s] reconnect failed", runtime.device_id)

    async def start_all(self) -> list[ControlResult]:
        async with self._control_lock:
            targets = self.ready_gateways
            if not targets:
                self.logger.error("START rejected: no online synchronized gateway")
                return []
            if self.control_state in {
                "STARTING",
                "RUNNING",
                "RUNNING_PARTIAL",
                "STOPPING",
            }:
                self.logger.warning("START ignored while state=%s", self.control_state)
                return []
            session_id = int(time.time())
            if self.current_session_id is not None and session_id <= self.current_session_id:
                session_id = self.current_session_id + 1
            self.current_session_id = session_id
            self.control_state = "STARTING"
            results = await self._command_all("START", session_id, targets)
            all_success = results and all(r.success for r in results)
            self.control_state = (
                "RUNNING"
                if all_success and not self.offline_gateway_ids
                else "RUNNING_PARTIAL"
                if all_success
                else "ERROR"
            )
            self._print_control_summary(results)
            return results

    async def stop_all(self) -> list[ControlResult]:
        async with self._control_lock:
            targets = self.ready_gateways
            if not targets:
                self.logger.error("STOP rejected: no online synchronized gateway")
                return []
            if self.control_state == "STOPPING":
                self.logger.warning("STOP ignored while a STOP is already pending")
                return []
            if self.current_session_id is None:
                self.logger.warning("STOP ignored: there is no current session ID")
                return []
            self.control_state = "STOPPING"
            results = await self._command_all("STOP", self.current_session_id, targets)
            self.control_state = "IDLE" if results and all(r.success for r in results) else "ERROR"
            self._print_control_summary(results)
            return results

    async def _command_all(
        self,
        command: str,
        session_id: int,
        targets: dict[str, GatewayRuntime],
    ) -> list[ControlResult]:
        self.logger.info(
            "%s session=%d requested for online gateway(s): %s",
            command,
            session_id,
            ", ".join(sorted(targets)),
        )
        tasks = [
            self._command_one(runtime, command, session_id)
            for runtime in targets.values()
        ]
        return list(await asyncio.gather(*tasks))

    async def _command_one(
        self,
        runtime: GatewayRuntime,
        command: str,
        session_id: int,
    ) -> ControlResult:
        client = runtime.client
        payload = encode_start(session_id) if command == "START" else encode_stop(session_id)
        client.drain_gateway_messages()
        forwarded = False
        try:
            await client.send_control(payload)
            deadline = asyncio.get_running_loop().time() + self.config.control_ack_timeout_seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Linux ACK timeout")
                message = await asyncio.wait_for(client.gateway_messages.get(), remaining)
                if message.kind == "gateway_error":
                    return ControlResult(
                        runtime.device_id,
                        command,
                        session_id,
                        forwarded,
                        False,
                        error=message.error or message.raw,
                    )
                if message.command != command or message.session_id != session_id:
                    self.logger.info(
                        "[%s] ignoring unrelated control response: %s",
                        runtime.device_id,
                        message.raw,
                    )
                    continue
                if message.kind == "forwarded":
                    forwarded = bool(message.success)
                    if not message.success:
                        return ControlResult(
                            runtime.device_id,
                            command,
                            session_id,
                            forwarded,
                            False,
                            error=message.status or message.raw,
                        )
                    continue
                if message.kind == "linux_ack":
                    return ControlResult(
                        runtime.device_id,
                        command,
                        session_id,
                        forwarded,
                        bool(message.success),
                        frames=message.frames,
                        error=message.error if not message.success else None,
                    )
        except Exception as exc:
            self.logger.exception(
                "[%s] %s session=%d failed", runtime.device_id, command, session_id
            )
            return ControlResult(
                runtime.device_id,
                command,
                session_id,
                forwarded,
                False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _print_control_summary(self, results: list[ControlResult]) -> None:
        if not results:
            return
        command = results[0].command
        session_id = results[0].session_id
        print(f"\n{command} session={session_id}")
        print(f"Targets: {', '.join(sorted(item.device_id for item in results))}")
        for result in sorted(results, key=lambda item: item.device_id):
            if result.success:
                frames = f" FRAMES={result.frames}" if result.frames is not None else ""
                print(f"[{result.device_id}] ACK {command} OK{frames}")
            else:
                print(f"[{result.device_id}] FAILED: {result.error or 'unknown error'}")
        offline = self.offline_gateway_ids
        if offline:
            print(f"Not sent (offline/not ready): {', '.join(offline)}")
        print(f"Overall state: {self.control_state}\n")

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
        await runtime.client.disconnect()

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

    async def close(self) -> None:
        self._closing = True
        await self.disconnect_all()

"""Windows 使用 Bleak 直接连接 68/69/70 Slave，并只负责 UTC 授时与授时状态接收。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any

from .config import AppConfig
from .protocol import (
    ProtocolError,
    ProtocolLengthError,
    TimeStatus,
    pack_time_request,
    parse_status_response,
)


class BleDependencyError(RuntimeError):
    pass


class TargetNotFoundError(RuntimeError):
    pass


class BleAdapterUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class ScanResult:
    name: str
    address: str
    rssi: int | None
    service_uuids: tuple[str, ...]
    is_target: bool
    device: Any


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    status: TimeStatus
    sent_phone_us: int
    t1_wall_ns: int
    t1_monotonic_ns: int
    t4_monotonic_ns: int
    response_source: str


@dataclass(slots=True)
class _PendingRequest:
    expected_phone_us: int
    future: asyncio.Future[tuple[TimeStatus, int, str]]


def _bleak_classes() -> tuple[Any, Any]:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise BleDependencyError(
            "The 'bleak' package is not installed. Run scripts\\install.ps1 only after installation is approved."
        ) from exc
    return BleakClient, BleakScanner


class BleTimeClient:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        gateway_id: str | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.gateway_id = gateway_id
        self.client: Any | None = None
        self._write_characteristic: Any | None = None
        self._status_characteristic: Any | None = None
        self._time_notify_started = False
        self._request_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: _PendingRequest | None = None
        self.disconnected_event = asyncio.Event()
        self._closing = False

    @staticmethod
    def _name(device: Any, advertisement: Any) -> str:
        return (
            getattr(advertisement, "local_name", None)
            or getattr(device, "name", None)
            or ""
        )

    @classmethod
    async def scan(cls, config: AppConfig, logger: logging.Logger) -> list[ScanResult]:
        """Run unfiltered active discovery, then match configured Slave names or service UUID."""

        _, scanner_class = _bleak_classes()
        logger.info(
            "Starting Bleak unfiltered active discovery for %.1f seconds",
            config.scan_timeout_seconds,
        )
        try:
            discovered = await scanner_class.discover(
                timeout=config.scan_timeout_seconds,
                return_adv=True,
                scanning_mode="active",
            )
        except Exception as exc:
            if type(exc).__name__ == "BleakBluetoothNotAvailableError":
                raise BleAdapterUnavailableError(
                    "Windows reports no usable Bluetooth adapter. Check that a BLE-capable adapter is installed and enabled and that its driver is loaded."
                ) from exc
            raise

        results = [
            cls._merge_scan_result(None, device, advertisement, config)
            for device, advertisement in discovered.values()
        ]
        targets = [item for item in results if item.is_target]
        if targets:
            logger.info(
                "Configured Slave candidate(s) discovered: %s",
                ", ".join(
                    f"{item.name}@{item.address}"
                    for item in sorted(targets, key=lambda item: item.name)
                ),
            )
        else:
            logger.info("Active discovery completed without a configured Slave candidate")

        return sorted(
            results,
            key=lambda item: (not item.is_target, -(item.rssi or -999)),
        )

    @classmethod
    def _merge_scan_result(
        cls,
        previous: ScanResult | None,
        device: Any,
        advertisement: Any,
        config: AppConfig,
    ) -> ScanResult:
        current_name = cls._name(device, advertisement)
        if current_name:
            name = current_name
        elif previous is not None:
            name = previous.name
        else:
            name = "(unnamed)"

        services = {str(uuid).lower() for uuid in (advertisement.service_uuids or ())}
        if previous is not None:
            services.update(previous.service_uuids)
        service_uuids = tuple(sorted(services))

        current_rssi = getattr(advertisement, "rssi", None)
        rssi = current_rssi if current_rssi is not None else (previous.rssi if previous else None)
        normalized_name = "" if name == "(unnamed)" else name.casefold()
        expected_names = {item.casefold() for item in config.gateways.values()}
        is_target = (
            normalized_name in expected_names
            or config.service_uuid.lower() in service_uuids
        )
        return ScanResult(
            name=name,
            address=str(device.address),
            rssi=rssi,
            service_uuids=service_uuids,
            is_target=is_target,
            device=device,
        )

    async def find_target(self) -> ScanResult:
        results = await self.scan(self.config, self.logger)
        target = next((item for item in results if item.is_target), None)
        if target is None:
            raise TargetNotFoundError(
                "No configured Slave was found. Check that BLE devices 68/69/70 are powered, advertising, and still advertising after the Master connection."
            )
        return target

    async def connect(self, device: Any | None = None) -> None:
        if self.is_connected:
            return
        if device is None:
            device = (await self.find_target()).device

        client_class, _ = _bleak_classes()
        self._closing = False
        self.disconnected_event.clear()
        self.client = client_class(
            device,
            disconnected_callback=self._on_disconnected,
            timeout=self.config.connect_timeout_seconds,
        )
        prefix = f"[{self.gateway_id}] " if self.gateway_id else ""
        self.logger.info("%sConnecting to %s", prefix, getattr(device, "address", device))
        await asyncio.wait_for(
            self.client.connect(), timeout=self.config.connect_timeout_seconds + 1.0
        )
        self._validate_gatt()
        await self.client.start_notify(
            self._status_characteristic,
            self._notification_callback,
        )
        self._time_notify_started = True
        self.logger.info("%sConnected; Time Status notification enabled", prefix)

    @property
    def is_connected(self) -> bool:
        return bool(self.client is not None and self.client.is_connected)

    def _validate_gatt(self) -> None:
        if self.client is None:
            raise RuntimeError("BLE client is not connected")

        services = self.client.services
        service = services.get_service(self.config.service_uuid)
        if service is None:
            raise RuntimeError(f"Required service not found: {self.config.service_uuid}")

        write_char = services.get_characteristic(self.config.write_characteristic_uuid)
        status_char = services.get_characteristic(self.config.status_characteristic_uuid)

        if write_char is None:
            raise RuntimeError(
                f"Required write characteristic not found: {self.config.write_characteristic_uuid}"
            )
        if status_char is None:
            raise RuntimeError(
                f"Required status characteristic not found: {self.config.status_characteristic_uuid}"
            )

        write_properties = {str(item).lower() for item in write_char.properties}
        status_properties = {str(item).lower() for item in status_char.properties}

        if self.config.write_with_response:
            if "write" not in write_properties:
                raise RuntimeError(
                    "Time Sync characteristic does not advertise write-with-response support"
                )
        elif "write-without-response" not in write_properties:
            raise RuntimeError(
                "Time Sync characteristic does not advertise write-without-response support"
            )

        if not ({"notify", "indicate"} & status_properties):
            raise RuntimeError(
                "Time Status characteristic does not advertise notify/indicate support"
            )

        self._write_characteristic = write_char
        self._status_characteristic = status_char

    def _on_disconnected(self, _client: Any) -> None:
        self.disconnected_event.set()
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(
                ConnectionError("BLE disconnected while a request was pending")
            )
        if not self._closing:
            self.logger.warning(
                "BLE connection was lost; a rescan/reconnect will be attempted"
            )

    def _notification_callback(self, _sender: Any, data: bytearray) -> None:
        # T4 must be captured before parsing or logging work.
        t4_monotonic_ns = time.perf_counter_ns()
        pending = self._pending
        if pending is None:
            self.logger.warning(
                "Ignoring unsolicited Time Status notification (%d bytes)", len(data)
            )
            return
        try:
            status = parse_status_response(data, source="Notify")
        except Exception as exc:
            self._pending = None
            if not pending.future.done():
                pending.future.set_exception(exc)
            return
        if status.phone_us != pending.expected_phone_us:
            self.logger.warning(
                "Ignoring mismatched response phone_us=%d (expected %d)",
                status.phone_us,
                pending.expected_phone_us,
            )
            return
        self._pending = None
        if not pending.future.done():
            pending.future.set_result((status, t4_monotonic_ns, "notify"))

    async def request_time(self, compensation_ms: float = 0.0) -> ExchangeResult:
        async with self._request_lock:
            if not self.is_connected or self.client is None:
                raise ConnectionError("BLE is not connected")
            if self._pending is not None:
                raise RuntimeError("A time-sync request is already pending")

            t1_wall_ns = time.time_ns()
            target_ns = t1_wall_ns + round(compensation_ms * 1_000_000)
            payload, sent_phone_us = pack_time_request(target_ns)
            loop = asyncio.get_running_loop()
            future: asyncio.Future[tuple[TimeStatus, int, str]] = loop.create_future()
            self._pending = _PendingRequest(sent_phone_us, future)
            t1_monotonic_ns = time.perf_counter_ns()

            try:
                async with self._write_lock:
                    await self.client.write_gatt_char(
                        self._write_characteristic,
                        payload,
                        response=self.config.write_with_response,
                    )
            except Exception:
                self._pending = None
                future.cancel()
                raise

            try:
                status, t4_monotonic_ns, source = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.config.notify_timeout_seconds
                )
            except ProtocolLengthError as exc:
                self.logger.error(
                    "%s; attempting a full Time Status characteristic read", exc
                )
                status, t4_monotonic_ns, source = await self._read_status_fallback(
                    sent_phone_us
                )
            except asyncio.TimeoutError as exc:
                self._pending = None
                future.cancel()
                raise TimeoutError(
                    f"No matching Notify received within {self.config.notify_timeout_seconds:.1f} seconds"
                ) from exc
            finally:
                if self._pending is not None and self._pending.future is future:
                    self._pending = None

            return ExchangeResult(
                status=status,
                sent_phone_us=sent_phone_us,
                t1_wall_ns=t1_wall_ns,
                t1_monotonic_ns=t1_monotonic_ns,
                t4_monotonic_ns=t4_monotonic_ns,
                response_source=source,
            )

    async def _read_status_fallback(
        self,
        expected_phone_us: int,
    ) -> tuple[TimeStatus, int, str]:
        if self.client is None or self._status_characteristic is None:
            raise ConnectionError("Cannot read status because BLE is disconnected")
        properties = {
            str(item).lower() for item in self._status_characteristic.properties
        }
        if "read" not in properties:
            raise ProtocolError(
                "Time Status characteristic is not readable; truncated Notify was not parsed"
            )
        raw = await self.client.read_gatt_char(self._status_characteristic)
        t4_monotonic_ns = time.perf_counter_ns()
        status = parse_status_response(raw, source="Read fallback")
        if status.phone_us != expected_phone_us:
            raise ProtocolError(
                f"Read fallback phone_us={status.phone_us} does not match request {expected_phone_us}"
            )
        return status, t4_monotonic_ns, "read"

    def describe_gatt(self) -> list[dict[str, Any]]:
        if self.client is None or not self.is_connected:
            raise ConnectionError("BLE is not connected")
        description: list[dict[str, Any]] = []
        for service in self.client.services:
            characteristics = []
            for char in service.characteristics:
                characteristics.append(
                    {
                        "uuid": str(char.uuid),
                        "handle": getattr(char, "handle", None),
                        "properties": list(char.properties),
                        "descriptors": [
                            {
                                "uuid": str(desc.uuid),
                                "handle": getattr(desc, "handle", None),
                            }
                            for desc in char.descriptors
                        ],
                    }
                )
            description.append(
                {
                    "uuid": str(service.uuid),
                    "description": getattr(service, "description", ""),
                    "characteristics": characteristics,
                }
            )
        return description

    async def disconnect(self) -> None:
        self._closing = True
        pending = self._pending
        self._pending = None
        if pending is not None and not pending.future.done():
            pending.future.cancel()

        if self.client is not None and self.client.is_connected:
            if self._time_notify_started and self._status_characteristic is not None:
                try:
                    await self.client.stop_notify(self._status_characteristic)
                except Exception:
                    self.logger.exception(
                        "Failed to stop Time Status notifications cleanly"
                    )
            try:
                await self.client.disconnect()
            except Exception:
                self.logger.exception("Failed to disconnect BLE cleanly")

        self._time_notify_started = False
        self.client = None
        self._write_characteristic = None
        self._status_characteristic = None
        self.disconnected_event.set()

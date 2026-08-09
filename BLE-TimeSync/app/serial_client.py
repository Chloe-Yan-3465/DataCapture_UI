"""Non-blocking USB-serial client for the Mode2 coordinator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import math
import time
from typing import Any, Callable

from .config import AppConfig
from .coordinator_protocol import (
    TimeAccept,
    TimeError,
    TimeReply,
    encode_time_query,
    encode_time_set,
    parse_coordinator_line,
)


class SerialDependencyError(RuntimeError):
    pass


class CoordinatorSerialError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str
    is_configured: bool


@dataclass(frozen=True, slots=True)
class TimeExchange:
    sequence: int
    coordinator_receive_us: int
    coordinator_transmit_us: int
    t1_wall_ns: int
    t1_monotonic_ns: int
    t4_monotonic_ns: int

    @property
    def total_rtt_us(self) -> float:
        return (self.t4_monotonic_ns - self.t1_monotonic_ns) / 1_000.0

    @property
    def coordinator_processing_us(self) -> int:
        return self.coordinator_transmit_us - self.coordinator_receive_us

    @property
    def net_rtt_us(self) -> float:
        return max(self.total_rtt_us - self.coordinator_processing_us, 0.0)

    @property
    def coordinator_ref_us(self) -> int:
        return (self.coordinator_receive_us + self.coordinator_transmit_us) // 2

    @property
    def utc_ref_ns(self) -> int:
        elapsed_ns = self.t4_monotonic_ns - self.t1_monotonic_ns
        return self.t1_wall_ns + elapsed_ns // 2

    @property
    def uncertainty_us(self) -> int:
        return min(1_000_000, max(1, math.ceil(self.net_rtt_us / 2.0)))


def _serial_modules() -> tuple[Any, Any]:
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as exc:
        raise SerialDependencyError(
            "The 'pyserial' package is not installed. Run scripts\\install.ps1."
        ) from exc
    return serial, list_ports


class CoordinatorSerialClient:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.port: Any | None = None
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.disconnected_event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._pending_replies: dict[int, asyncio.Future[tuple[TimeReply, int]]] = {}
        self._pending_accepts: dict[int, asyncio.Future[TimeAccept]] = {}
        self._closing = False
        self._sequence = 0

    @classmethod
    def list_ports(cls, config: AppConfig) -> list[SerialPortInfo]:
        _, list_ports = _serial_modules()
        configured = config.serial_port.casefold()
        return [
            SerialPortInfo(
                device=str(item.device),
                description=str(item.description or ""),
                hwid=str(item.hwid or ""),
                is_configured=str(item.device).casefold() == configured,
            )
            for item in list_ports.comports()
        ]

    @property
    def is_connected(self) -> bool:
        return bool(self.port is not None and getattr(self.port, "is_open", False))

    async def connect(self) -> None:
        if self.is_connected:
            return
        serial, _ = _serial_modules()
        self._closing = False
        self.disconnected_event.clear()
        self.logger.info(
            "Opening coordinator serial port %s at %d baud",
            self.config.serial_port,
            self.config.baud_rate,
        )
        try:
            self.port = await asyncio.to_thread(
                serial.Serial,
                port=self.config.serial_port,
                baudrate=self.config.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.config.serial_read_timeout_seconds,
                write_timeout=self.config.response_timeout_seconds,
            )
        except Exception as exc:
            self.port = None
            raise CoordinatorSerialError(
                f"Cannot open {self.config.serial_port}: {exc}"
            ) from exc
        await asyncio.sleep(self.config.connect_settle_seconds)
        if self.port is not None:
            await asyncio.to_thread(self.port.reset_input_buffer)
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="mode2-coordinator-serial-reader"
        )
        self.logger.info(
            "Coordinator serial link ready: expected BLE identity=%s (internal only)",
            self.config.coordinator_name,
        )

    async def _reader_loop(self) -> None:
        try:
            while not self._closing and self.port is not None:
                raw = await asyncio.to_thread(self.port.readline)
                received_monotonic_ns = time.perf_counter_ns()
                if not raw:
                    continue
                line = bytes(raw).decode("utf-8", errors="replace").strip("\x00\r\n")
                if not line:
                    continue
                self.logger.info("ESP32> %s", line)
                self.lines.put_nowait(line)
                try:
                    message = parse_coordinator_line(line)
                except Exception:
                    self.logger.exception("Invalid coordinator time-protocol line: %r", line)
                    continue
                if isinstance(message, TimeReply):
                    future = self._pending_replies.pop(message.sequence, None)
                    if future is not None and not future.done():
                        future.set_result((message, received_monotonic_ns))
                elif isinstance(message, TimeAccept):
                    future = self._pending_accepts.pop(message.sequence, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif isinstance(message, TimeError):
                    error = CoordinatorSerialError(message.raw)
                    if message.sequence is not None:
                        futures = (
                            self._pending_replies.pop(message.sequence, None),
                            self._pending_accepts.pop(message.sequence, None),
                        )
                    else:
                        futures = tuple(self._pending_replies.values()) + tuple(
                            self._pending_accepts.values()
                        )
                        self._pending_replies.clear()
                        self._pending_accepts.clear()
                    for future in futures:
                        if future is not None and not future.done():
                            future.set_exception(error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self.logger.exception("Coordinator serial reader failed")
                self._fail_pending(CoordinatorSerialError(f"Serial reader failed: {exc}"))
        finally:
            self.disconnected_event.set()

    def _fail_pending(self, error: Exception) -> None:
        futures = tuple(self._pending_replies.values()) + tuple(
            self._pending_accepts.values()
        )
        self._pending_replies.clear()
        self._pending_accepts.clear()
        for future in futures:
            if not future.done():
                future.set_exception(error)

    async def send(self, payload: bytes) -> None:
        if not self.is_connected or self.port is None:
            raise CoordinatorSerialError("Coordinator serial port is not connected")
        async with self._write_lock:
            try:
                await asyncio.to_thread(self.port.write, payload)
                await asyncio.to_thread(self.port.flush)
            except Exception as exc:
                raise CoordinatorSerialError(f"Serial write failed: {exc}") from exc

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        if self._sequence == 0:
            self._sequence = 1
        return self._sequence

    async def request_time(self) -> TimeExchange:
        async with self._request_lock:
            sequence = self._next_sequence()
            loop = asyncio.get_running_loop()
            future: asyncio.Future[tuple[TimeReply, int]] = loop.create_future()
            self._pending_replies[sequence] = future
            mapping_before_ns = time.perf_counter_ns()
            mapping_wall_ns = time.time_ns()
            mapping_after_ns = time.perf_counter_ns()
            mapping_midpoint_ns = (mapping_before_ns + mapping_after_ns) // 2
            t1_monotonic_ns = mapping_after_ns
            t1_wall_ns = mapping_wall_ns + (t1_monotonic_ns - mapping_midpoint_ns)
            try:
                await self.send(encode_time_query(sequence))
                reply, t4_monotonic_ns = await asyncio.wait_for(
                    asyncio.shield(future), self.config.response_timeout_seconds
                )
            except Exception:
                self._pending_replies.pop(sequence, None)
                if not future.done():
                    future.cancel()
                raise
            return TimeExchange(
                sequence=sequence,
                coordinator_receive_us=reply.coordinator_receive_us,
                coordinator_transmit_us=reply.coordinator_transmit_us,
                t1_wall_ns=t1_wall_ns,
                t1_monotonic_ns=t1_monotonic_ns,
                t4_monotonic_ns=t4_monotonic_ns,
            )

    async def apply_time(self, sample: TimeExchange) -> TimeAccept:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TimeAccept] = loop.create_future()
        self._pending_accepts[sample.sequence] = future
        try:
            await self.send(
                encode_time_set(
                    sample.sequence,
                    sample.coordinator_ref_us,
                    sample.utc_ref_ns,
                    sample.uncertainty_us,
                )
            )
            return await asyncio.wait_for(
                asyncio.shield(future), self.config.response_timeout_seconds
            )
        except Exception:
            self._pending_accepts.pop(sample.sequence, None)
            if not future.done():
                future.cancel()
            raise

    def drain_lines(self) -> None:
        while True:
            try:
                self.lines.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def wait_for_line(
        self, predicate: Callable[[str], bool], timeout: float
    ) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for coordinator response")
            line = await asyncio.wait_for(self.lines.get(), remaining)
            if predicate(line):
                return line

    async def disconnect(self) -> None:
        self._closing = True
        self._fail_pending(CoordinatorSerialError("Coordinator serial link closed"))
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._reader_task = None
        if self.port is not None:
            try:
                await asyncio.to_thread(self.port.close)
            except Exception:
                self.logger.exception("Failed to close coordinator serial port cleanly")
        self.port = None
        self.disconnected_event.set()

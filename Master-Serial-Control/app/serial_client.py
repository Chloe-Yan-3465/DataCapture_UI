"""Windows <-> Master ESP32 USB 串口通信底层。

本模块负责 115200 8N1 串口打开、后台按行接收、异步协议消息分发、
START/STOP/STATUS 请求等待以及同一采集会话 session_id 的维护。
BLE 控制逻辑不在 Windows 端实现，由 Master ESP32 自己负责。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable, Protocol

from .config import AppConfig
from .protocol import (
    MasterMessage,
    MasterProtocolError,
    UINT32_MAX,
    disconnected_slaves,
    encode_start,
    encode_status,
    encode_stop,
    parse_master_message,
    slaves_connected,
    validate_session_id,
)


SERIAL_BAUDRATE = 115200
SERIAL_BYTESIZE = 8
SERIAL_PARITY = "N"
SERIAL_STOPBITS = 1
AUTO_PORT = "AUTO"


class MasterSerialError(RuntimeError):
    """Base error for Master serial control."""


class SerialDependencyError(MasterSerialError):
    """Raised when pyserial is unavailable."""


class SerialPortNotFoundError(MasterSerialError):
    """Raised when no usable serial port is found."""


class SerialPortAmbiguousError(MasterSerialError):
    """Raised when AUTO mode sees multiple possible serial ports."""


class MasterDisconnectedError(MasterSerialError):
    """Raised when the Master serial link is unavailable."""


class MasterTimeoutError(MasterSerialError):
    """Raised when a matching Master response does not arrive in time."""


class MasterCommandError(MasterSerialError):
    """Raised when Master rejects START or STOP."""


class MasterNotReadyError(MasterCommandError):
    """Raised when one or more required Slave links are unavailable."""


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    device: str
    description: str
    hwid: str


class _SerialHandle(Protocol):
    is_open: bool

    def readline(self, size: int = -1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


MessageCallback = Callable[[MasterMessage], None]
SerialFactory = Callable[..., _SerialHandle]
PortProvider = Callable[[], list[SerialPortInfo]]


def _load_pyserial() -> tuple[object, object]:
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as exc:
        raise SerialDependencyError(
            "pyserial is required; install requirements.txt in this project"
        ) from exc
    return serial, list_ports


def list_serial_ports() -> list[SerialPortInfo]:
    """Return serial ports currently visible to Windows."""

    _, list_ports = _load_pyserial()
    return [
        SerialPortInfo(
            device=str(item.device),
            description=str(item.description or ""),
            hwid=str(item.hwid or ""),
        )
        for item in list_ports.comports()
    ]


def resolve_serial_port(
    configured_port: str,
    port_provider: PortProvider | None = None,
) -> str:
    """Resolve an explicit COM port or a single unambiguous AUTO candidate."""

    configured = configured_port.strip()
    if configured.casefold() != AUTO_PORT.casefold():
        return configured

    ports = (port_provider or list_serial_ports)()
    if not ports:
        raise SerialPortNotFoundError(
            "No serial ports were found; connect Master or configure serial_port"
        )
    if len(ports) != 1:
        choices = ", ".join(
            f"{item.device} ({item.description or 'unknown'})"
            for item in ports
        )
        raise SerialPortAmbiguousError(
            "AUTO serial_port requires exactly one visible serial port; "
            f"found: {choices}. Set serial_port to the Master COM port explicitly."
        )
    return ports[0].device


def _default_serial_factory(**kwargs: object) -> _SerialHandle:
    serial, _ = _load_pyserial()
    return serial.Serial(**kwargs)


class MasterSerialClient:
    """Threaded serial client for one Master ESP32."""

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        message_callback: MessageCallback | None = None,
        serial_factory: SerialFactory | None = None,
        port_provider: PortProvider | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.message_callback = message_callback
        self._serial_factory = serial_factory or _default_serial_factory
        self._port_provider = port_provider

        self._serial: _SerialHandle | None = None
        self._port: str | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()

        self._condition = threading.Condition()
        self._message_sequence = 0
        self._messages: deque[tuple[int, MasterMessage]] = deque(maxlen=512)
        self._reader_error: BaseException | None = None
        self._latest_status: MasterMessage | None = None
        self._active_session_id: int | None = None
        self._owned_session_id: int | None = None
        self._last_generated_session_id: int | None = None

    @property
    def port(self) -> str | None:
        return self._port

    @property
    def is_open(self) -> bool:
        handle = self._serial
        return bool(handle is not None and getattr(handle, "is_open", True))

    @property
    def latest_status(self) -> MasterMessage | None:
        with self._condition:
            return self._latest_status

    @property
    def active_session_id(self) -> int | None:
        with self._condition:
            return self._active_session_id

    @property
    def owned_session_id(self) -> int | None:
        with self._condition:
            return self._owned_session_id

    @property
    def all_slaves_connected(self) -> bool:
        with self._condition:
            status = self._latest_status
        return status is not None and slaves_connected(status)

    def open(self) -> str:
        """Open the configured Master serial port and start the RX thread."""

        if self.is_open:
            assert self._port is not None
            return self._port

        port = resolve_serial_port(
            self.config.serial_port,
            self._port_provider,
        )
        handle = self._serial_factory(
            port=port,
            baudrate=SERIAL_BAUDRATE,
            bytesize=SERIAL_BYTESIZE,
            parity=SERIAL_PARITY,
            stopbits=SERIAL_STOPBITS,
            timeout=self.config.read_timeout_seconds,
            write_timeout=self.config.write_timeout_seconds,
        )

        self._serial = handle
        self._port = port
        self._stop_event.clear()
        with self._condition:
            self._reader_error = None
            self._messages.clear()
            self._message_sequence = 0
            self._latest_status = None
            self._active_session_id = None
            self._owned_session_id = None

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="master-serial-rx",
            daemon=True,
        )
        self._reader_thread.start()
        self.logger.info(
            "Master serial opened: %s, %d 8N1",
            port,
            SERIAL_BAUDRATE,
        )
        return port

    def close(self) -> None:
        """Stop the reader thread and close the serial port."""

        self._stop_event.set()
        handle = self._serial

        if handle is not None:
            cancel_read = getattr(handle, "cancel_read", None)
            if callable(cancel_read):
                try:
                    cancel_read()
                except Exception:
                    pass

        thread = self._reader_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self.config.read_timeout_seconds * 4, 1.0))

        if handle is not None:
            try:
                handle.close()
            except Exception:
                self.logger.exception("Failed to close Master serial port")

        self._reader_thread = None
        self._serial = None
        self.logger.info("Master serial closed")

    def _reader_loop(self) -> None:
        handle = self._serial
        if handle is None:
            return

        try:
            while not self._stop_event.is_set():
                line = handle.readline(self.config.max_line_bytes + 1)
                if not line:
                    continue

                if len(line) > self.config.max_line_bytes:
                    self.logger.warning(
                        "Ignoring oversized Master serial line (%d bytes)",
                        len(line),
                    )
                    continue

                try:
                    message = parse_master_message(line)
                except MasterProtocolError as exc:
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    self.logger.warning(
                        "Ignoring non-protocol Master serial line: %s (%s)",
                        text,
                        exc,
                    )
                    continue

                self.logger.info("RX %s", message.raw)
                self._publish_message(message)

        except BaseException as exc:
            if not self._stop_event.is_set():
                self.logger.error("Master serial reader failed: %s", exc)
                with self._condition:
                    self._reader_error = exc
                    self._condition.notify_all()

    def _publish_message(self, message: MasterMessage) -> None:
        callback = self.message_callback

        with self._condition:
            self._message_sequence += 1
            self._messages.append((self._message_sequence, message))

            if message.kind == "status":
                self._latest_status = message
                if message.state == "RUNNING" and message.session_id is not None:
                    self._active_session_id = message.session_id
                elif message.state != "RUNNING":
                    self._active_session_id = None
                    self._owned_session_id = None

            elif message.kind == "ack" and message.success:
                if message.command == "START" and message.session_id is not None:
                    self._active_session_id = message.session_id
                elif message.command == "STOP":
                    self._active_session_id = None
                    if self._owned_session_id == message.session_id:
                        self._owned_session_id = None

            self._condition.notify_all()

        if callback is not None:
            try:
                callback(message)
            except Exception:
                self.logger.exception("Master message callback failed")

    def _current_marker(self) -> int:
        with self._condition:
            return self._message_sequence

    def _raise_reader_error_locked(self) -> None:
        if self._reader_error is not None:
            raise MasterDisconnectedError(
                f"Master serial reader failed: {self._reader_error}"
            ) from self._reader_error

    def check_reader(self) -> None:
        """Raise if the background serial reader has failed."""

        with self._condition:
            self._raise_reader_error_locked()

    def _wait_for_message(
        self,
        predicate: Callable[[MasterMessage], bool],
        after_sequence: int,
        timeout: float,
        description: str,
    ) -> MasterMessage:
        deadline = time.monotonic() + timeout

        with self._condition:
            while True:
                self._raise_reader_error_locked()

                for sequence, message in self._messages:
                    if sequence > after_sequence and predicate(message):
                        return message

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MasterTimeoutError(
                        f"Timed out waiting for {description} after {timeout:.1f} seconds"
                    )
                self._condition.wait(min(remaining, 0.25))

    def _write(self, payload: bytes) -> None:
        handle = self._serial
        if handle is None or not self.is_open:
            raise MasterDisconnectedError("Master serial port is not open")

        with self._write_lock:
            try:
                written = handle.write(payload)
                handle.flush()
            except Exception as exc:
                raise MasterDisconnectedError(
                    f"Failed to write Master serial command: {exc}"
                ) from exc

        if written != len(payload):
            raise MasterDisconnectedError(
                f"Short Master serial write: {written}/{len(payload)} bytes"
            )

        self.logger.info(
            "TX %s",
            payload.decode("ascii").rstrip("\r\n"),
        )

    def request_status(self, timeout: float | None = None) -> MasterMessage:
        """Send STATUS and wait for a later STATUS frame."""

        wait_seconds = timeout or self.config.status_timeout_seconds
        marker = self._current_marker()
        self._write(encode_status())
        return self._wait_for_message(
            lambda message: message.kind == "status",
            marker,
            wait_seconds,
            "STATUS",
        )

    def generate_session_id(self) -> int:
        """Generate a uint32 session ID and avoid immediate reuse."""

        candidate = int(time.time_ns() // 1_000) & UINT32_MAX
        if candidate == self._last_generated_session_id:
            candidate = (candidate + 1) & UINT32_MAX
        self._last_generated_session_id = candidate
        return candidate

    def start_capture(
        self,
        session_id: int | None = None,
        timeout: float | None = None,
    ) -> MasterMessage:
        """Verify all Slave links, send START, and wait for matching ACK."""

        status = self.request_status()
        if not slaves_connected(status):
            missing = ",".join(disconnected_slaves(status))
            raise MasterNotReadyError(
                f"Master Slave links are not ready: {missing}"
            )
        if status.state == "RUNNING":
            raise MasterCommandError(
                f"Master is already running session {status.session_id}"
            )

        selected_session = (
            self.generate_session_id()
            if session_id is None
            else validate_session_id(session_id)
        )
        wait_seconds = timeout or self.config.command_timeout_seconds
        marker = self._current_marker()
        self._write(encode_start(selected_session))

        ack = self._wait_for_message(
            lambda message: (
                message.kind == "ack"
                and message.command == "START"
                and message.session_id == selected_session
            ),
            marker,
            wait_seconds,
            f"ACK START session {selected_session}",
        )
        if ack.success is not True:
            raise MasterCommandError(
                f"Master START rejected: {ack.error or ack.status or 'UNKNOWN'}"
            )
        with self._condition:
            self._owned_session_id = selected_session
        return ack

    def stop_capture(
        self,
        session_id: int | None = None,
        timeout: float | None = None,
    ) -> MasterMessage:
        """Send STOP for the active session and wait for matching ACK."""

        selected_session = session_id
        if selected_session is None:
            selected_session = self.active_session_id

        if selected_session is None:
            status = self.request_status()
            if status.state == "RUNNING" and status.session_id is not None:
                selected_session = status.session_id
            else:
                raise MasterCommandError("No active Master session is known")

        selected_session = validate_session_id(selected_session)
        wait_seconds = timeout or self.config.command_timeout_seconds
        marker = self._current_marker()
        self._write(encode_stop(selected_session))

        ack = self._wait_for_message(
            lambda message: (
                message.kind == "ack"
                and message.command == "STOP"
                and message.session_id == selected_session
            ),
            marker,
            wait_seconds,
            f"ACK STOP session {selected_session}",
        )
        if ack.success is not True:
            raise MasterCommandError(
                f"Master STOP rejected: {ack.error or ack.status or 'UNKNOWN'}"
            )
        return ack

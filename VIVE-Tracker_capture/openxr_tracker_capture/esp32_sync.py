"""Optional sequence-numbered ESP32 UDP event receiver."""

from __future__ import annotations

import ctypes
import json
import queue
import socket
import threading
import time
from dataclasses import dataclass


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
_KERNEL32.QueryPerformanceCounter.restype = ctypes.c_int


def query_performance_counter_ticks() -> int:
    value = ctypes.c_longlong()
    if not _KERNEL32.QueryPerformanceCounter(ctypes.byref(value)):
        raise OSError(ctypes.get_last_error(), "QueryPerformanceCounter failed")
    return value.value


@dataclass
class Esp32SyncMessage:
    sync_id: str
    esp32_timestamp_us: int
    esp32_source_timestamp_us: int | None
    esp32_gpio_timestamp_us: int | None
    pc_receive_qpc_ticks: int
    pc_receive_monotonic_ns: int
    sender_address: str
    raw_message: str
    xr_receive_timestamp_ns: int | None = None
    matched_trigger_timestamp_xr_ns: int | None = None
    match_delta_ns: int | None = None


class Esp32UdpReceiver:
    """Receive independent, sequence-numbered JSON datagrams without ordinal pairing."""

    def __init__(self, bind_host: str, bind_port: int):
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.messages: queue.Queue[Esp32SyncMessage] = queue.Queue()
        self.stop_event = threading.Event()
        self.socket: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.invalid_messages = 0

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_host, self.bind_port))
        sock.settimeout(0.2)
        self.socket = sock
        self.thread = threading.Thread(target=self._run, name="esp32-sync-udp", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        assert self.socket is not None
        while not self.stop_event.is_set():
            try:
                payload, address = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            receive_qpc = query_performance_counter_ticks()
            receive_mono = time.perf_counter_ns()
            raw = payload.decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
                sync_id = str(data["sync_id"])
                primary = data.get("esp32_timestamp_us", data.get("gpio_timestamp_us"))
                if primary is None:
                    raise ValueError("missing esp32_timestamp_us")
                message = Esp32SyncMessage(
                    sync_id=sync_id,
                    esp32_timestamp_us=int(primary),
                    esp32_source_timestamp_us=_optional_int(data.get("source_timestamp_us")),
                    esp32_gpio_timestamp_us=_optional_int(data.get("gpio_timestamp_us")),
                    pc_receive_qpc_ticks=receive_qpc,
                    pc_receive_monotonic_ns=receive_mono,
                    sender_address=f"{address[0]}:{address[1]}",
                    raw_message=raw,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self.invalid_messages += 1
                continue
            self.messages.put(message)

    def drain(self) -> list[Esp32SyncMessage]:
        result = []
        while True:
            try:
                result.append(self.messages.get_nowait())
            except queue.Empty:
                return result

    def close(self) -> None:
        self.stop_event.set()
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            self.thread = None


def _optional_int(value):
    return None if value is None else int(value)

"""Windows 与 Slave 共用的 UTC 授时二进制协议编解码模块。"""

from __future__ import annotations

from dataclasses import dataclass
import struct


REQUEST_STRUCT = struct.Struct("<QI")
RESPONSE_STRUCT = struct.Struct("<QqQQ")
REQUEST_SIZE = REQUEST_STRUCT.size
RESPONSE_SIZE = RESPONSE_STRUCT.size


class ProtocolError(ValueError):
    """Base class for invalid time-sync protocol data."""


class ProtocolLengthError(ProtocolError):
    """Raised when a packet does not have the required fixed length."""

    def __init__(self, actual: int, expected: int, source: str = "Notify") -> None:
        self.actual = actual
        self.expected = expected
        self.source = source
        super().__init__(
            f"{source} data length is {actual} bytes; exactly {expected} bytes are required"
        )


@dataclass(frozen=True, slots=True)
class TimeStatus:
    phone_us: int
    offset_us: int
    esp_receive_us: int
    esp_process_us: int

    @property
    def esp_processing_us(self) -> int:
        return self.esp_process_us - self.esp_receive_us


def pack_time_request(timestamp_ns: int) -> tuple[bytes, int]:
    """Pack Unix epoch nanoseconds as ``<uint64 sec, uint32 usec>``.

    Returns both the packet and the exact microsecond timestamp represented by it.
    """

    if timestamp_ns < 0:
        raise ProtocolError("Unix timestamp cannot be negative")
    sec, remainder_ns = divmod(timestamp_ns, 1_000_000_000)
    usec = remainder_ns // 1_000
    if sec > 0xFFFFFFFFFFFFFFFF:
        raise ProtocolError("Unix seconds do not fit in uint64")
    return REQUEST_STRUCT.pack(sec, usec), sec * 1_000_000 + usec


def parse_status_response(data: bytes | bytearray | memoryview, *, source: str = "Notify") -> TimeStatus:
    """Strictly parse the fixed-size ESP32 response.

    Oversized data is rejected too; silently accepting a prefix could hide a firmware
    protocol mismatch.
    """

    raw = bytes(data)
    if len(raw) != RESPONSE_SIZE:
        raise ProtocolLengthError(len(raw), RESPONSE_SIZE, source)
    status = TimeStatus(*RESPONSE_STRUCT.unpack(raw))
    if status.esp_process_us < status.esp_receive_us:
        raise ProtocolError(
            "ESP processing timestamp precedes its receive timestamp "
            f"({status.esp_process_us} < {status.esp_receive_us})"
        )
    return status

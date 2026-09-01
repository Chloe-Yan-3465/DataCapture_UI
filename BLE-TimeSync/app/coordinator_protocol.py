"""ASCII protocol implemented by ``esp32_mode2_sync_timesync.cpp``."""

from __future__ import annotations

from dataclasses import dataclass
import re


UINT32_MAX = 0xFFFFFFFF


class CoordinatorProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TimeReply:
    sequence: int
    coordinator_receive_us: int
    coordinator_transmit_us: int


@dataclass(frozen=True, slots=True)
class TimeAccept:
    sequence: int
    nodes: int
    uncertainty_us: int


@dataclass(frozen=True, slots=True)
class TimeError:
    raw: str
    sequence: int | None
    message: str


TIME_REPLY_RE = re.compile(r"^TIME_REPLY (\d+) (-?\d+) (-?\d+)$")
TIME_ACCEPT_RE = re.compile(
    r"^TIME_ACCEPT seq=(\d+) nodes=(\d+) uncertainty_us=(\d+)$"
)
TIME_ERROR_RE = re.compile(r"^TIME_ERROR(?: seq=(\d+))?\s*(.*)$")


def _validate_sequence(sequence: int) -> int:
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise CoordinatorProtocolError("sequence must be an integer")
    if not 1 <= sequence <= UINT32_MAX:
        raise CoordinatorProtocolError("sequence must be in uint32 range and non-zero")
    return sequence


def encode_time_query(sequence: int) -> bytes:
    return f"TIME_QUERY {_validate_sequence(sequence)}\n".encode("ascii")


def encode_time_set(
    sequence: int,
    coordinator_ref_us: int,
    utc_ref_ns: int,
    uncertainty_us: int,
) -> bytes:
    _validate_sequence(sequence)
    if not isinstance(coordinator_ref_us, int):
        raise CoordinatorProtocolError("coordinator_ref_us must be an integer")
    if not isinstance(utc_ref_ns, int) or utc_ref_ns <= 0:
        raise CoordinatorProtocolError("utc_ref_ns must be a positive integer")
    if (
        isinstance(uncertainty_us, bool)
        or not isinstance(uncertainty_us, int)
        or not 0 <= uncertainty_us <= 1_000_000
    ):
        raise CoordinatorProtocolError("uncertainty_us must be in range 0..1000000")
    return (
        f"TIME_SET {sequence} {coordinator_ref_us} {utc_ref_ns} {uncertainty_us}\n"
    ).encode("ascii")


PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_path_segment(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CoordinatorProtocolError(
            f"{name} must contain 1..{maximum} characters"
        )
    if PATH_SEGMENT_RE.fullmatch(value) is None:
        raise CoordinatorProtocolError(
            f"{name} may contain only A-Z, a-z, 0-9, '_' or '-'"
        )
    return value


def encode_start(
    task_name: str | None = None,
    complex_level: str | None = None,
) -> bytes:
    if task_name is None and complex_level is None:
        return b"START\n"
    if task_name is None or complex_level is None:
        raise CoordinatorProtocolError(
            "task_name and complex_level must be provided together"
        )
    task = _validate_path_segment(task_name, "task_name", 31)
    level = _validate_path_segment(complex_level, "complex_level", 15)
    return f"START TASK={task} LEVEL={level}\n".encode("ascii")


def encode_stop() -> bytes:
    return b"STOP\n"


def encode_abort() -> bytes:
    return b"ABORT\n"


def encode_status() -> bytes:
    return b"STATUS\n"


def encode_scan() -> bytes:
    return b"SCAN\n"


def parse_coordinator_line(line: str) -> TimeReply | TimeAccept | TimeError | None:
    text = line.strip("\x00\r\n ")
    match = TIME_REPLY_RE.fullmatch(text)
    if match:
        sequence, receive_us, transmit_us = (int(value) for value in match.groups())
        _validate_sequence(sequence)
        if receive_us < 0 or transmit_us < receive_us:
            raise CoordinatorProtocolError("invalid coordinator receive/transmit timestamps")
        return TimeReply(sequence, receive_us, transmit_us)

    match = TIME_ACCEPT_RE.fullmatch(text)
    if match:
        sequence, nodes, uncertainty_us = (int(value) for value in match.groups())
        _validate_sequence(sequence)
        return TimeAccept(sequence, nodes, uncertainty_us)

    match = TIME_ERROR_RE.fullmatch(text)
    if match:
        sequence_text, message = match.groups()
        sequence = int(sequence_text) if sequence_text is not None else None
        if sequence is not None:
            _validate_sequence(sequence)
        return TimeError(text, sequence, message.strip() or "unknown time-sync error")

    return None

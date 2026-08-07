"""Windows <-> Master ESP32 USB 串口协议处理模块。

协议版本：V3-BLE-30HZ-1。

本模块只负责：
1. 将 Windows 侧命令编码为发送给 Master 的串口字节流；
2. 将 Master 返回的一整行文本协议解析为结构化消息。

本模块不负责打开串口，也不负责串口线程、超时或重连。
"""

from __future__ import annotations

from dataclasses import dataclass, field


UINT32_MAX = 0xFFFFFFFF
LINE_ENDING = b"\r\n"
SLAVE_IDS = ("68", "69", "70")


class MasterProtocolError(ValueError):
    """Master serial protocol format error."""


@dataclass(frozen=True, slots=True)
class MasterMessage:
    """One parsed message returned by Master."""

    kind: str
    raw: str
    command: str | None = None
    session_id: int | None = None
    success: bool | None = None
    status: str | None = None
    error: str | None = None
    state: str | None = None
    reason: str | None = None
    device_id: str | None = None
    event: str | None = None
    pulses: int | None = None
    fields: dict[str, str] = field(default_factory=dict)
    flags: tuple[str, ...] = ()


def validate_session_id(session_id: int) -> int:
    """Validate uint32 session ID."""

    if isinstance(session_id, bool) or not isinstance(session_id, int):
        raise MasterProtocolError("session_id must be an integer")
    if not 0 <= session_id <= UINT32_MAX:
        raise MasterProtocolError("session_id must be in uint32 range")
    return session_id


def encode_start(session_id: int) -> bytes:
    """Encode START command."""

    return f"START+{validate_session_id(session_id)}".encode("ascii") + LINE_ENDING


def encode_stop(session_id: int) -> bytes:
    """Encode STOP command."""

    return f"STOP+{validate_session_id(session_id)}".encode("ascii") + LINE_ENDING


def encode_status() -> bytes:
    """Encode STATUS command."""

    return b"STATUS" + LINE_ENDING


def _parse_uint(value: str, field_name: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise MasterProtocolError(
            f"{field_name} must be an unsigned integer"
        ) from exc
    if parsed < 0:
        raise MasterProtocolError(f"{field_name} must be an unsigned integer")
    return parsed


def _split_fields(
    tokens: list[str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Split KEY=VALUE fields and flag tokens."""

    fields: dict[str, str] = {}
    flags: list[str] = []

    for token in tokens:
        if not token:
            raise MasterProtocolError("Master frame contains an empty token")
        if "=" in token:
            key, value = token.split("=", 1)
            if not key or not value:
                raise MasterProtocolError(
                    "Master frame contains an empty KEY=VALUE field"
                )
            if key in fields:
                raise MasterProtocolError(
                    f"Master frame contains duplicate field {key}"
                )
            fields[key] = value
        else:
            flags.append(token)

    return fields, tuple(flags)


def _session_id(fields: dict[str, str]) -> int | None:
    value = fields.get("SESSION")
    if value is None:
        return None
    return validate_session_id(_parse_uint(value, "SESSION"))


def _optional_uint(fields: dict[str, str], key: str) -> int | None:
    value = fields.get(key)
    if value is None:
        return None
    return _parse_uint(value, key)


def parse_master_message(
    data: bytes | bytearray | memoryview | str,
) -> MasterMessage:
    """Parse one complete Master serial frame.

    Caller must first split the serial stream by line.
    """

    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MasterProtocolError("Master frame is not valid UTF-8") from exc

    text = text.strip("\x00\r\n")
    if not text:
        raise MasterProtocolError("Master frame is empty")
    if not text.endswith("+END"):
        raise MasterProtocolError("Master frame must end with +END")

    tokens = text.split("+")
    if len(tokens) < 2 or tokens[-1] != "END":
        raise MasterProtocolError("Invalid Master frame")

    prefix = tokens[0]
    body = tokens[1:-1]
    fields, flags = _split_fields(body)
    session_id = _session_id(fields)

    if prefix == "ACK":
        if flags and flags[0] in {"START", "STOP"}:
            command = flags[0]
            rest = list(flags[1:])

            if session_id is None:
                raise MasterProtocolError(
                    f"ACK {command} frame must contain SESSION"
                )
            if ("OK" in rest) == ("ERROR" in rest):
                raise MasterProtocolError(
                    f"ACK {command} frame must contain exactly one of OK or ERROR"
                )

            if "OK" in rest:
                success = True
                error = None
                ok_index = rest.index("OK")
                trailing = rest[ok_index + 1:]
                status = "+".join(trailing) if trailing else "OK"
            else:
                success = False
                status = "ERROR"
                error_index = rest.index("ERROR")
                trailing = rest[error_index + 1:]
                error = "+".join(trailing) or "UNKNOWN"

            return MasterMessage(
                kind="ack",
                raw=text,
                command=command,
                session_id=session_id,
                success=success,
                status=status,
                error=error,
                reason=fields.get("REASON"),
                pulses=_optional_uint(fields, "PULSES"),
                fields=fields,
                flags=flags,
            )

        if flags and flags[0] == "ERROR":
            error = "+".join(flags[1:]) or "UNKNOWN"
            return MasterMessage(
                kind="error",
                raw=text,
                success=False,
                status="ERROR",
                error=error,
                fields=fields,
                flags=flags,
            )

        raise MasterProtocolError(f"Unsupported ACK frame: {text}")

    if prefix == "STATUS":
        if "STATE" not in fields:
            raise MasterProtocolError("STATUS frame must contain STATE")
        return MasterMessage(
            kind="status",
            raw=text,
            session_id=session_id,
            state=fields.get("STATE"),
            pulses=_optional_uint(fields, "PULSES"),
            fields=fields,
            flags=flags,
        )

    if prefix == "EVENT":
        if not flags or flags[0] != "SLAVE":
            raise MasterProtocolError(f"Unsupported EVENT frame: {text}")
        if fields.get("DEVICE") not in SLAVE_IDS:
            raise MasterProtocolError("Slave EVENT must contain DEVICE=68/69/70")

        event = next(
            (
                item
                for item in flags[1:]
                if item in {"FOUND", "CONNECTED", "DISCONNECTED"}
            ),
            None,
        )
        if event is None:
            raise MasterProtocolError(
                f"Slave EVENT has no known event type: {text}"
            )

        return MasterMessage(
            kind="event",
            raw=text,
            device_id=fields.get("DEVICE"),
            event=event,
            fields=fields,
            flags=flags,
        )

    raise MasterProtocolError(f"Unsupported Master frame: {text}")


def slaves_connected(message: MasterMessage) -> bool:
    """Check whether all three Master-to-Slave BLE links are ready."""

    return message.kind == "status" and all(
        message.fields.get(f"S{device_id}") == "CONNECTED"
        for device_id in SLAVE_IDS
    )


def disconnected_slaves(message: MasterMessage) -> tuple[str, ...]:
    """Return Slave IDs that are not reported as CONNECTED."""

    if message.kind != "status":
        return SLAVE_IDS
    return tuple(
        device_id
        for device_id in SLAVE_IDS
        if message.fields.get(f"S{device_id}") != "CONNECTED"
    )

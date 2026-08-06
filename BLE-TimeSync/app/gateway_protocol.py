"""Text control protocol used by the ESP32 BLE-UART gateways."""

from __future__ import annotations

from dataclasses import dataclass, field


UINT32_MAX = 0xFFFFFFFF


class GatewayProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayMessage:
    kind: str
    raw: str
    command: str | None = None
    session_id: int | None = None
    device_id: str | None = None
    state: str | None = None
    sync: bool | None = None
    success: bool | None = None
    frames: int | None = None
    status: str | None = None
    error: str | None = None
    fields: dict[str, str] = field(default_factory=dict)


def validate_session_id(session_id: int) -> int:
    if isinstance(session_id, bool) or not isinstance(session_id, int):
        raise GatewayProtocolError("session_id must be an integer")
    if not 0 <= session_id <= UINT32_MAX:
        raise GatewayProtocolError("session_id must be in uint32 range")
    return session_id


def encode_start(session_id: int) -> bytes:
    return f"START+{validate_session_id(session_id)}".encode("ascii")


def encode_stop(session_id: int) -> bytes:
    return f"STOP+{validate_session_id(session_id)}".encode("ascii")


def encode_status() -> bytes:
    return b"STATUS"


def _parse_uint(value: str, field_name: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise GatewayProtocolError(f"{field_name} must be an unsigned integer") from exc
    if parsed < 0:
        raise GatewayProtocolError(f"{field_name} must be an unsigned integer")
    return parsed


def _split_fields(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    flags: list[str] = []
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            if not key or not value:
                raise GatewayProtocolError("gateway message contains an empty field")
            fields[key] = value
        else:
            flags.append(token)
    return fields, flags


def parse_gateway_message(data: bytes | bytearray | memoryview | str) -> GatewayMessage:
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GatewayProtocolError("gateway status is not valid UTF-8") from exc
    text = text.strip("\x00\r\n")
    if not text or not text.endswith("+END"):
        raise GatewayProtocolError("gateway status must end with +END")
    tokens = text.split("+")
    if tokens[-1] != "END" or len(tokens) < 3:
        raise GatewayProtocolError("invalid gateway status frame")
    prefix = tokens[0]
    body = tokens[1:-1]
    fields, flags = _split_fields(body)

    session_id = None
    if "SESSION" in fields:
        session_id = _parse_uint(fields["SESSION"], "SESSION")
        validate_session_id(session_id)
    frames = _parse_uint(fields["FRAMES"], "FRAMES") if "FRAMES" in fields else None

    if prefix == "GW" and "DEVICE" in fields:
        sync_text = fields.get("SYNC")
        sync = None if sync_text is None else sync_text == "YES"
        if sync_text not in {None, "YES", "NO"}:
            raise GatewayProtocolError("SYNC must be YES or NO")
        return GatewayMessage(
            kind="gateway_state",
            raw=text,
            session_id=session_id,
            device_id=fields["DEVICE"],
            state=fields.get("STATE"),
            sync=sync,
            fields=fields,
        )

    if prefix == "GW" and body and body[0] == "ERROR":
        error = "+".join(body[1:]) or "UNKNOWN"
        return GatewayMessage(kind="gateway_error", raw=text, error=error, fields=fields)

    if prefix == "GW" and body and body[0] in {"START", "STOP"}:
        command = body[0]
        status = next((item for item in flags[1:] if item != command), None)
        return GatewayMessage(
            kind="forwarded",
            raw=text,
            command=command,
            session_id=session_id,
            success=status in {"FORWARDED", "ALREADY_RUNNING", "ALREADY_FORWARDED"},
            status=status,
            fields=fields,
        )

    if prefix == "ACK" and body and body[0] in {"START", "STOP"}:
        command = body[0]
        result = next((item for item in flags[1:] if item != command), None)
        error = None
        if result == "ERROR":
            tail = [item for item in flags[1:] if item not in {command, "ERROR"}]
            error = "+".join(tail) or "UNKNOWN"
        return GatewayMessage(
            kind="linux_ack",
            raw=text,
            command=command,
            session_id=session_id,
            success=result == "OK",
            frames=frames,
            status=result,
            error=error,
            fields=fields,
        )

    if prefix == "STATUS":
        state = next((item for item in flags if item not in {"STATUS"}), None)
        return GatewayMessage(
            kind="linux_status",
            raw=text,
            session_id=session_id,
            state=state,
            frames=frames,
            fields=fields,
        )

    raise GatewayProtocolError(f"unsupported gateway status frame: {text}")

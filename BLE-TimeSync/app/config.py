"""Configuration loading and validation for the Mode2 coordinator link."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"


@dataclass(frozen=True, slots=True)
class AppConfig:
    coordinator_name: str
    serial_port: str
    baud_rate: int
    serial_read_timeout_seconds: float
    response_timeout_seconds: float
    connect_settle_seconds: float
    calibration_warmup_samples: int
    calibration_samples: int
    calibration_interval_ms: int
    sync_interval_seconds: float
    control_ack_timeout_seconds: float
    log_directory: Path
    reconnect_delay_seconds: float = 3.0

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "AppConfig":
        config_path = Path(path).resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            raw: dict[str, Any] = json.load(handle)

        required = {
            "coordinator_name",
            "serial_port",
            "baud_rate",
            "serial_read_timeout_seconds",
            "response_timeout_seconds",
            "connect_settle_seconds",
            "calibration_warmup_samples",
            "calibration_samples",
            "calibration_interval_ms",
            "sync_interval_seconds",
            "control_ack_timeout_seconds",
            "log_directory",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise ValueError(f"Missing configuration keys: {', '.join(missing)}")

        coordinator_name = str(raw["coordinator_name"]).strip()
        serial_port = str(raw["serial_port"]).strip()
        if not coordinator_name:
            raise ValueError("coordinator_name must not be empty")
        if not serial_port:
            raise ValueError("serial_port must not be empty")

        baud_rate = raw["baud_rate"]
        if isinstance(baud_rate, bool) or not isinstance(baud_rate, int) or baud_rate <= 0:
            raise ValueError("baud_rate must be a positive integer")

        positive = (
            "serial_read_timeout_seconds",
            "response_timeout_seconds",
            "connect_settle_seconds",
            "calibration_samples",
            "calibration_interval_ms",
            "sync_interval_seconds",
            "control_ack_timeout_seconds",
        )
        for key in positive:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{key} must be a positive number")

        warmup = raw["calibration_warmup_samples"]
        if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
            raise ValueError("calibration_warmup_samples must be a non-negative integer")

        reconnect_delay = raw.get("reconnect_delay_seconds", 3.0)
        if (
            isinstance(reconnect_delay, bool)
            or not isinstance(reconnect_delay, (int, float))
            or reconnect_delay <= 0
        ):
            raise ValueError("reconnect_delay_seconds must be a positive number")

        log_directory = Path(str(raw["log_directory"]))
        if not log_directory.is_absolute():
            log_directory = PROJECT_ROOT / log_directory

        return cls(
            coordinator_name=coordinator_name,
            serial_port=serial_port,
            baud_rate=baud_rate,
            serial_read_timeout_seconds=float(raw["serial_read_timeout_seconds"]),
            response_timeout_seconds=float(raw["response_timeout_seconds"]),
            connect_settle_seconds=float(raw["connect_settle_seconds"]),
            calibration_warmup_samples=warmup,
            calibration_samples=int(raw["calibration_samples"]),
            calibration_interval_ms=int(raw["calibration_interval_ms"]),
            sync_interval_seconds=float(raw["sync_interval_seconds"]),
            control_ack_timeout_seconds=float(raw["control_ack_timeout_seconds"]),
            log_directory=log_directory,
            reconnect_delay_seconds=float(reconnect_delay),
        )

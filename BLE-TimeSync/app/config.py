"""Windows BLE 授时工程配置加载与校验模块。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from uuid import UUID


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"


@dataclass(frozen=True, slots=True)
class AppConfig:
    gateways: dict[str, str]
    service_uuid: str
    write_characteristic_uuid: str
    status_characteristic_uuid: str
    scan_timeout_seconds: float
    connect_timeout_seconds: float
    notify_timeout_seconds: float
    calibration_warmup_samples: int
    calibration_samples: int
    calibration_interval_ms: int
    sync_interval_seconds: float
    sync_stagger_ms: int
    compensation_method: str
    write_with_response: bool
    log_directory: Path
    reconnect_delay_seconds: float = 3.0

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "AppConfig":
        config_path = Path(path).resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            raw: dict[str, Any] = json.load(handle)

        required = {
            "gateways",
            "service_uuid",
            "write_characteristic_uuid",
            "status_characteristic_uuid",
            "scan_timeout_seconds",
            "connect_timeout_seconds",
            "notify_timeout_seconds",
            "calibration_warmup_samples",
            "calibration_samples",
            "calibration_interval_ms",
            "sync_interval_seconds",
            "sync_stagger_ms",
            "compensation_method",
            "write_with_response",
            "log_directory",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise ValueError(f"Missing configuration keys: {', '.join(missing)}")

        for key in (
            "service_uuid",
            "write_characteristic_uuid",
            "status_characteristic_uuid",
        ):
            raw[key] = str(UUID(str(raw[key])))

        gateways_raw = raw["gateways"]
        if not isinstance(gateways_raw, dict) or not gateways_raw:
            raise ValueError("gateways must be a non-empty JSON object")
        gateways = {str(device_id): str(name) for device_id, name in gateways_raw.items()}
        if set(gateways) != {"68", "69", "70"}:
            raise ValueError("gateways must define exactly device IDs 68, 69, and 70")
        if gateways != {"68": "68", "69": "69", "70": "70"}:
            raise ValueError("BLE device names must be exactly 68, 69, and 70")

        method = str(raw["compensation_method"])
        if method not in {"net_rtt_median", "total_rtt_mean_half"}:
            raise ValueError(
                "compensation_method must be net_rtt_median or total_rtt_mean_half"
            )
        if not isinstance(raw["write_with_response"], bool):
            raise ValueError("write_with_response must be a JSON boolean")

        non_negative = (
            "calibration_warmup_samples",
            "sync_stagger_ms",
        )
        for key in non_negative:
            if (
                not isinstance(raw[key], int)
                or isinstance(raw[key], bool)
                or raw[key] < 0
            ):
                raise ValueError(f"{key} must be a non-negative integer")

        positive = (
            "scan_timeout_seconds",
            "connect_timeout_seconds",
            "notify_timeout_seconds",
            "calibration_samples",
            "calibration_interval_ms",
            "sync_interval_seconds",
        )
        for key in positive:
            if (
                not isinstance(raw[key], (int, float))
                or isinstance(raw[key], bool)
                or raw[key] <= 0
            ):
                raise ValueError(f"{key} must be a positive number")

        if not 200 <= int(raw["sync_stagger_ms"]) <= 500:
            raise ValueError("sync_stagger_ms must be between 200 and 500 milliseconds")

        log_directory = Path(str(raw["log_directory"]))
        if not log_directory.is_absolute():
            log_directory = PROJECT_ROOT / log_directory

        return cls(
            gateways=gateways,
            service_uuid=raw["service_uuid"],
            write_characteristic_uuid=raw["write_characteristic_uuid"],
            status_characteristic_uuid=raw["status_characteristic_uuid"],
            scan_timeout_seconds=float(raw["scan_timeout_seconds"]),
            connect_timeout_seconds=float(raw["connect_timeout_seconds"]),
            notify_timeout_seconds=float(raw["notify_timeout_seconds"]),
            calibration_warmup_samples=int(raw["calibration_warmup_samples"]),
            calibration_samples=int(raw["calibration_samples"]),
            calibration_interval_ms=int(raw["calibration_interval_ms"]),
            sync_interval_seconds=float(raw["sync_interval_seconds"]),
            sync_stagger_ms=int(raw["sync_stagger_ms"]),
            compensation_method=method,
            write_with_response=raw["write_with_response"],
            log_directory=log_directory,
            reconnect_delay_seconds=float(raw.get("reconnect_delay_seconds", 3.0)),
        )

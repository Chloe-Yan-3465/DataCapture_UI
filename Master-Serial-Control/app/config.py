"""Master 串口控制工程的配置读取与校验模块。

本模块读取 config/config.json，并只保留 Windows <-> Master 串口控制所需参数。
协议固定的 115200 8N1 不作为可变配置，避免配置文件意外改变线上协议。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"


@dataclass(frozen=True, slots=True)
class AppConfig:
    serial_port: str
    read_timeout_seconds: float
    write_timeout_seconds: float
    command_timeout_seconds: float
    status_timeout_seconds: float
    max_line_bytes: int
    log_directory: Path

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "AppConfig":
        config_path = Path(path).resolve()
        with config_path.open("r", encoding="utf-8") as handle:
            raw: dict[str, Any] = json.load(handle)

        required = {
            "serial_port",
            "read_timeout_seconds",
            "write_timeout_seconds",
            "command_timeout_seconds",
            "status_timeout_seconds",
            "max_line_bytes",
            "log_directory",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise ValueError(f"Missing configuration keys: {', '.join(missing)}")

        serial_port = str(raw["serial_port"]).strip()
        if not serial_port:
            raise ValueError("serial_port must be a non-empty string")

        for key in (
            "read_timeout_seconds",
            "write_timeout_seconds",
            "command_timeout_seconds",
            "status_timeout_seconds",
        ):
            value = raw[key]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{key} must be a positive number")

        max_line_bytes = raw["max_line_bytes"]
        if (
            not isinstance(max_line_bytes, int)
            or isinstance(max_line_bytes, bool)
            or max_line_bytes < 128
        ):
            raise ValueError("max_line_bytes must be an integer >= 128")

        log_directory = Path(str(raw["log_directory"]))
        if not log_directory.is_absolute():
            log_directory = PROJECT_ROOT / log_directory

        return cls(
            serial_port=serial_port,
            read_timeout_seconds=float(raw["read_timeout_seconds"]),
            write_timeout_seconds=float(raw["write_timeout_seconds"]),
            command_timeout_seconds=float(raw["command_timeout_seconds"]),
            status_timeout_seconds=float(raw["status_timeout_seconds"]),
            max_line_bytes=max_line_bytes,
            log_directory=log_directory,
        )

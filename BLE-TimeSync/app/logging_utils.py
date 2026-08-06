"""Console, diagnostic, CSV, and JSONL logging helpers."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Mapping


LOG_FIELDS = (
    "device_id",
    "sequence",
    "windows_utc",
    "sent_phone_us",
    "t1_wall_ns",
    "t1_monotonic_ns",
    "t4_monotonic_ns",
    "rtt_ms",
    "esp_processing_ms",
    "net_rtt_ms",
    "estimated_one_way_ms",
    "compensation_ms",
    "esp_offset_us",
    "success",
    "error",
)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def configure_logging(log_directory: str | Path) -> logging.Logger:
    log_dir = Path(log_directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ble_timesync")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    latest = logging.FileHandler(log_dir / "latest.log", mode="w", encoding="utf-8")
    latest.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(latest)
    return logger


class SessionLog:
    """Write one synchronization session to matching CSV and JSONL files."""

    def __init__(self, log_directory: str | Path) -> None:
        log_dir = Path(log_directory)
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = log_dir / f"timesync_{stamp}.csv"
        self.jsonl_path = log_dir / f"timesync_{stamp}.jsonl"
        self._csv_file = self.csv_path.open("w", newline="", encoding="utf-8-sig")
        self._jsonl_file = self.jsonl_path.open("w", encoding="utf-8")
        self._writer = csv.DictWriter(self._csv_file, fieldnames=LOG_FIELDS, extrasaction="ignore")
        self._writer.writeheader()
        self._csv_file.flush()

    def write(self, record: Mapping[str, Any]) -> None:
        row = {field: record.get(field, "") for field in LOG_FIELDS}
        self._writer.writerow(row)
        self._jsonl_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._csv_file.flush()
        self._jsonl_file.flush()

    def close(self) -> None:
        self._csv_file.close()
        self._jsonl_file.close()

    def __enter__(self) -> "SessionLog":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

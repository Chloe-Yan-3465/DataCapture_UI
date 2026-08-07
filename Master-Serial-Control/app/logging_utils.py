"""Master 串口控制工程的控制台与文件日志配置模块。"""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_directory: str | Path) -> logging.Logger:
    """Create the project logger and replace stale handlers."""

    log_dir = Path(log_directory)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("master_serial_control")
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

    latest = logging.FileHandler(
        log_dir / "latest.log",
        mode="w",
        encoding="utf-8",
    )
    latest.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(latest)
    return logger

"""Master 串口控制工程的命令行入口。

本入口支持串口枚举、STATUS 单测、单次 START/STOP，以及后续供 UI 调用的
持续 run 模式。run 模式中的 1/0 只是 Windows 子进程内部控制命令，真正发给
Master 的串口帧始终严格为 START+session、STOP+session 和 STATUS。
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path
import sys
import threading
import time
from typing import Callable

from .config import AppConfig, DEFAULT_CONFIG_PATH
from .logging_utils import configure_logging
from .protocol import MasterMessage, disconnected_slaves, slaves_connected
from .serial_client import (
    MasterCommandError,
    MasterSerialClient,
    MasterSerialError,
    list_serial_ports,
)


class _RunReporter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready: bool | None = None

    def __call__(self, message: MasterMessage) -> None:
        if message.kind == "status":
            ready = slaves_connected(message)
            with self._lock:
                changed = ready != self._ready
                self._ready = ready
            if changed and ready:
                print(
                    "[READY] Master serial connected; S68/S69/S70 CONNECTED",
                    flush=True,
                )
            elif changed:
                missing = ",".join(disconnected_slaves(message))
                print(
                    f"[NOT_READY] Master Slave links missing: {missing}",
                    flush=True,
                )

        elif message.kind == "event":
            print(f"[MASTER_EVENT] {message.raw}", flush=True)

        elif (
            message.kind == "ack"
            and message.command == "STOP"
            and message.success
            and message.reason == "SLAVE_DISCONNECTED"
        ):
            print(f"[AUTO_STOP] {message.raw}", flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Windows USB serial control for Master ESP32."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.json",
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Override serial_port from config, for example COM7",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ports", help="List visible serial ports")
    subparsers.add_parser("status", help="Request one Master STATUS frame")

    start_parser = subparsers.add_parser("start", help="Send one START command")
    start_parser.add_argument("--session-id", type=int, default=None)

    stop_parser = subparsers.add_parser("stop", help="Send one STOP command")
    stop_parser.add_argument("--session-id", type=int, required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Keep the Master serial control process running",
    )
    run_parser.add_argument(
        "--control-stdin",
        action="store_true",
        help="Accept 1/0/status/quit line commands from stdin",
    )
    return parser


def _load_config(args: argparse.Namespace) -> AppConfig:
    config = AppConfig.load(args.config)
    if args.port:
        config = dataclasses.replace(config, serial_port=str(args.port))
    return config


def _open_client(
    config: AppConfig,
    logger: logging.Logger,
    callback: Callable[[MasterMessage], None] | None = None,
) -> MasterSerialClient:
    client = MasterSerialClient(config, logger, message_callback=callback)
    port = client.open()
    print(f"[SERIAL_READY] PORT={port} BAUD=115200 8N1", flush=True)
    return client


def _ports() -> int:
    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return 1

    for item in ports:
        print(f"{item.device}\t{item.description}\t{item.hwid}")
    return 0


def _status(config: AppConfig, logger: logging.Logger) -> int:
    client = _open_client(config, logger)
    try:
        status = client.request_status()
        print(status.raw, flush=True)
        return 0
    finally:
        client.close()


def _start(
    config: AppConfig,
    logger: logging.Logger,
    session_id: int | None,
) -> int:
    client = _open_client(config, logger)
    try:
        ack = client.start_capture(session_id=session_id)
        print(f"[START_OK] SESSION={ack.session_id}", flush=True)
        print(ack.raw, flush=True)
        return 0
    finally:
        client.close()


def _stop(
    config: AppConfig,
    logger: logging.Logger,
    session_id: int,
) -> int:
    client = _open_client(config, logger)
    try:
        ack = client.stop_capture(session_id=session_id)
        print(f"[STOP_OK] SESSION={ack.session_id}", flush=True)
        print(ack.raw, flush=True)
        return 0
    finally:
        client.close()


def _safe_stop(client: MasterSerialClient, logger: logging.Logger) -> None:
    session_id = client.owned_session_id
    if session_id is None:
        return
    try:
        ack = client.stop_capture(session_id=session_id)
        print(f"[STOP_OK] SESSION={ack.session_id}", flush=True)
    except MasterSerialError as exc:
        logger.error("Failed to stop active session during exit: %s", exc)


def _handle_runtime_command(
    client: MasterSerialClient,
    command: str,
    logger: logging.Logger,
) -> bool:
    normalized = command.strip().casefold()
    if not normalized:
        return True

    try:
        if normalized in {"1", "start"}:
            ack = client.start_capture()
            print(f"[START_OK] SESSION={ack.session_id}", flush=True)

        elif normalized in {"0", "stop"}:
            ack = client.stop_capture()
            print(f"[STOP_OK] SESSION={ack.session_id}", flush=True)

        elif normalized == "status":
            status = client.request_status()
            print(status.raw, flush=True)

        elif normalized in {"q", "quit", "exit"}:
            return False

        else:
            logger.warning("Ignoring unknown control command: %s", command)

    except MasterCommandError as exc:
        print(f"[COMMAND_ERROR] {exc}", flush=True)
    except MasterSerialError as exc:
        print(f"[SERIAL_ERROR] {exc}", flush=True)
        raise

    return True


def _stdin_control_loop(
    client: MasterSerialClient,
    logger: logging.Logger,
) -> int:
    print(
        "[CONTROL] stdin mode: 1=START, 0=STOP, status=STATUS, quit=exit",
        flush=True,
    )

    for line in sys.stdin:
        client.check_reader()
        if not _handle_runtime_command(client, line, logger):
            return 0

    logger.info("Control stdin closed; exiting")
    return 0


def _keyboard_control_loop(
    client: MasterSerialClient,
    logger: logging.Logger,
) -> int:
    if sys.platform != "win32":
        raise RuntimeError("Single-key Master control is only supported on Windows")

    import msvcrt

    print(
        "[CONTROL] keyboard mode: 1=START, 0=STOP, S=STATUS, Q=exit",
        flush=True,
    )

    while True:
        client.check_reader()
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue

        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue

        if key == "1":
            command = "start"
        elif key == "0":
            command = "stop"
        elif key.casefold() == "s":
            command = "status"
        elif key.casefold() == "q":
            command = "quit"
        else:
            continue

        if not _handle_runtime_command(client, command, logger):
            return 0


def _run(
    config: AppConfig,
    logger: logging.Logger,
    control_stdin: bool,
) -> int:
    reporter = _RunReporter()
    client = _open_client(config, logger, reporter)
    try:
        status = client.request_status()
        print(f"[MASTER_STATUS] {status.raw}", flush=True)

        if control_stdin:
            return _stdin_control_loop(client, logger)
        return _keyboard_control_loop(client, logger)

    finally:
        _safe_stop(client, logger)
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "ports":
        try:
            return _ports()
        except MasterSerialError as exc:
            print(f"Serial error: {exc}", file=sys.stderr)
            return 2

    try:
        config = _load_config(args)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = configure_logging(config.log_directory)

    try:
        if args.command == "status":
            return _status(config, logger)
        if args.command == "start":
            return _start(config, logger, args.session_id)
        if args.command == "stop":
            return _stop(config, logger, args.session_id)
        return _run(config, logger, args.control_stdin)

    except KeyboardInterrupt:
        logger.info("Ctrl+C received")
        return 130
    except MasterSerialError as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("Command failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

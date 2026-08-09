"""Command-line entry point for Windows-to-Mode2-coordinator time sync."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys
import threading
from typing import Awaitable, Callable

from .config import AppConfig, DEFAULT_CONFIG_PATH
from .coordinator_manager import CoordinatorManager
from .coordinator_protocol import encode_status
from .coordinator_time_sync import CoordinatorTimeSyncEngine
from .logging_utils import SessionLog, configure_logging
from .serial_client import (
    CoordinatorSerialClient,
    CoordinatorSerialError,
    SerialDependencyError,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Windows controller for the Mode2 ESP32 coordinator over USB serial"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="configuration JSON path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="list serial ports and mark the configured port")
    subparsers.add_parser("inspect", help="open the coordinator and make one timing probe")
    subparsers.add_parser("once", help="synchronize the coordinator once and exit")
    run_parser = subparsers.add_parser(
        "run",
        help="synchronize continuously and accept 1/0 capture-control keys",
    )
    run_parser.add_argument(
        "--control-stdin",
        action="store_true",
        help="accept line-based 1/0/quit commands from stdin (for the local Web UI)",
    )
    return parser


def _python_is_supported() -> bool:
    return sys.maxsize > 2**32 and sys.version_info[:2] in {(3, 11), (3, 12)}


async def _scan(config: AppConfig, _logger: logging.Logger) -> int:
    ports = CoordinatorSerialClient.list_ports(config)
    if not ports:
        print("No serial ports were discovered.")
        return 2
    print(f"Discovered {len(ports)} serial port(s):")
    for item in ports:
        marker = "CONFIGURED" if item.is_configured else "other"
        print(
            f"  [{marker}] port={item.device} description={item.description!r} "
            f"hwid={item.hwid!r}"
        )
    if not any(item.is_configured for item in ports):
        print(f"Configured port {config.serial_port} is not currently present.")
        return 2
    return 0


async def _inspect(config: AppConfig, logger: logging.Logger) -> int:
    client = CoordinatorSerialClient(config, logger)
    try:
        await client.connect()
        client.drain_lines()
        await client.send(encode_status())
        sample = await client.request_time()
        print(f"Coordinator: {config.coordinator_name}")
        print(f"Serial: {config.serial_port}@{config.baud_rate} 8N1")
        print(
            f"TIME_REPLY seq={sample.sequence} RTT={sample.total_rtt_us / 1000:.3f} ms "
            f"net_RTT={sample.net_rtt_us / 1000:.3f} ms "
            f"uncertainty={sample.uncertainty_us} us"
        )
        print("Probe only: TIME_SET was not sent.")
        return 0
    finally:
        await client.disconnect()


async def _once(config: AppConfig, logger: logging.Logger) -> int:
    client = CoordinatorSerialClient(config, logger)
    try:
        await client.connect()
        with SessionLog(config.log_directory) as session_log:
            engine = CoordinatorTimeSyncEngine(client, config, session_log, logger)
            await engine.synchronize(include_warmup=True)
        return 0
    finally:
        await client.disconnect()


async def _run(
    config: AppConfig,
    logger: logging.Logger,
    control_stdin: bool = False,
) -> int:
    with SessionLog(config.log_directory) as session_log:
        manager = CoordinatorManager(config, session_log, logger)
        try:
            while True:
                try:
                    await manager.initialize()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Coordinator initialization failed")
                    await manager.close()
                    logger.info(
                        "Retrying coordinator initialization in %.1f seconds",
                        config.reconnect_delay_seconds,
                    )
                    await asyncio.sleep(config.reconnect_delay_seconds)
                    manager = CoordinatorManager(config, session_log, logger)
            if control_stdin:
                await _stdin_control_loop(manager, logger)
            else:
                await _keyboard_control_loop(manager, logger)
            return 0
        finally:
            await manager.close()


async def _stdin_control_loop(
    manager: CoordinatorManager,
    logger: logging.Logger,
) -> int:
    loop = asyncio.get_running_loop()
    commands: asyncio.Queue[str | None] = asyncio.Queue()

    def _read_stdin() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(commands.put_nowait, line.strip())
        finally:
            try:
                loop.call_soon_threadsafe(commands.put_nowait, None)
            except RuntimeError:
                pass

    threading.Thread(
        target=_read_stdin,
        name="mode2-ui-stdin",
        daemon=True,
    ).start()
    print("[CONTROL] stdin mode: send 1 to START, 0 to STOP, quit to exit.", flush=True)

    while True:
        command = await commands.get()
        if command is None:
            logger.info("Control stdin closed; exiting")
            return 0
        normalized = command.casefold()
        if normalized in {"1", "start"}:
            await manager.start_all()
        elif normalized in {"0", "stop"}:
            await manager.stop_all()
        elif normalized in {"q", "quit", "exit"}:
            logger.info("UI requested coordinator controller exit")
            return 0
        elif normalized:
            logger.warning("Ignoring unknown stdin control command: %s", command)


async def _keyboard_control_loop(
    manager: CoordinatorManager,
    logger: logging.Logger,
) -> int:
    if sys.platform != "win32":
        raise RuntimeError("Single-key coordinator control is only supported on Windows")
    import msvcrt

    command_tasks: set[asyncio.Task[object]] = set()

    def _task_done(task: asyncio.Task[object]) -> None:
        command_tasks.discard(task)
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                logger.error(
                    "Keyboard control task failed",
                    exc_info=(type(exception), exception, exception.__traceback__),
                )

    try:
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.05)
                continue
            key = msvcrt.getwch()
            if key in {"\x00", "\xe0"}:
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if key == "1":
                task = asyncio.create_task(manager.start_all(), name="coordinator-start")
            elif key == "0":
                task = asyncio.create_task(manager.stop_all(), name="coordinator-stop")
            else:
                continue
            command_tasks.add(task)
            task.add_done_callback(_task_done)
    finally:
        for task in command_tasks:
            task.cancel()
        if command_tasks:
            await asyncio.gather(*command_tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = AppConfig.load(args.config)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logger = configure_logging(config.log_directory)

    if not _python_is_supported():
        logger.error(
            "Python 3.11 or 3.12 (64-bit) is required; current interpreter is %s (%s-bit)",
            sys.version.split()[0],
            64 if sys.maxsize > 2**32 else 32,
        )
        return 2

    command: Callable[[], Awaitable[int]]
    if args.command == "scan":
        command = lambda: _scan(config, logger)
    elif args.command == "inspect":
        command = lambda: _inspect(config, logger)
    elif args.command == "once":
        command = lambda: _once(config, logger)
    else:
        command = lambda: _run(config, logger, args.control_stdin)

    try:
        return asyncio.run(command())
    except KeyboardInterrupt:
        logger.info("Ctrl+C received; coordinator serial link closed")
        return 130
    except (CoordinatorSerialError, SerialDependencyError) as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("Command failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

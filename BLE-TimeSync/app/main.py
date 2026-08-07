"""Windows BLE 授时工程命令行入口；run 模式只维持 68/69/70 的连接与周期 UTC 授时。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import threading
from typing import Awaitable, Callable

from .ble_client import (
    BleAdapterUnavailableError,
    BleDependencyError,
    BleTimeClient,
    TargetNotFoundError,
)
from .config import AppConfig, DEFAULT_CONFIG_PATH
from .gateway_manager import GatewayManager
from .logging_utils import SessionLog, configure_logging
from .protocol import RESPONSE_SIZE
from .time_sync import TimeSyncEngine


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Windows UTC time-sync client for BLE Slaves 68, 69, and 70"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="configuration JSON path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="scan only; never connect or write time")
    subparsers.add_parser("inspect", help="inspect GATT and make one uncompensated probe")
    subparsers.add_parser("once", help="perform initial time sync, send one compensated update, and exit")
    run_parser = subparsers.add_parser(
        "run",
        help="connect Slaves 68/69/70 and synchronize UTC continuously",
    )
    run_parser.add_argument(
        "--no-compensation",
        action="store_true",
        help="measure initial delay but send subsequent updates without delay compensation",
    )
    run_parser.add_argument(
        "--control-stdin",
        action="store_true",
        help="accept line-based quit/exit commands from stdin for process management",
    )
    return parser


def _python_is_supported() -> bool:
    return sys.maxsize > 2**32 and sys.version_info[:2] in {(3, 11), (3, 12)}


async def _scan(config: AppConfig, logger: logging.Logger) -> int:
    results = await BleTimeClient.scan(config, logger)
    if not results:
        print("No BLE devices were discovered.")
    else:
        print(f"Discovered {len(results)} BLE device(s):")
        for item in results:
            marker = "TARGET" if item.is_target else "other"
            services = ",".join(item.service_uuids) or "-"
            print(
                f"  [{marker}] name={item.name!r} address={item.address} "
                f"RSSI={item.rssi} services={services}"
            )

    found_names = {item.name.casefold() for item in results}
    missing = [
        name for name in config.gateways.values() if name.casefold() not in found_names
    ]
    if missing:
        print(f"Missing configured Slave(s): {', '.join(missing)}")
        if not any(name.casefold() in found_names for name in config.gateways.values()):
            print("Please:")
            print("  1. Check that Slaves 68/69/70 are powered and advertising.")
            print("  2. Confirm the BLE names are exactly 68, 69, and 70.")
            print("  3. Confirm each Slave keeps advertising after the Master connects.")
            return 2
        print("Available Slaves can connect now; missing Slaves will be retried in run mode.")
    return 0


async def _inspect(config: AppConfig, logger: logging.Logger) -> int:
    client = BleTimeClient(config, logger)
    try:
        await client.connect()
        print(json.dumps(client.describe_gatt(), indent=2, ensure_ascii=False))
        print("Required Time Sync and Time Status characteristics: OK")
        with SessionLog(config.log_directory) as session_log:
            engine = TimeSyncEngine(client, config, session_log, logger)
            print("Sending one uncompensated UTC probe to validate Time Status Notify...")
            result = await engine.perform_sync(0.0)
            if not result["success"]:
                raise RuntimeError(str(result["error"]))
            print(f"Notify response length: {RESPONSE_SIZE} bytes (strict parse passed)")
        return 0
    finally:
        await client.disconnect()


async def _once(config: AppConfig, logger: logging.Logger) -> int:
    client = BleTimeClient(config, logger)
    try:
        await client.connect()
        with SessionLog(config.log_directory) as session_log:
            engine = TimeSyncEngine(client, config, session_log, logger)
            compensation_ms = await engine.calibrate()
            result = await engine.perform_sync(compensation_ms)
            return 0 if result["success"] else 1
    finally:
        await client.disconnect()


async def _run(
    config: AppConfig,
    logger: logging.Logger,
    no_compensation: bool,
    control_stdin: bool = False,
) -> int:
    with SessionLog(config.log_directory) as session_log:
        manager = GatewayManager(
            config,
            session_log,
            logger,
            no_compensation=no_compensation,
        )
        try:
            while True:
                try:
                    await manager.initialize()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Slave time-sync initialization failed")
                    await manager.disconnect_all()
                    logger.info(
                        "Retrying Slave time-sync initialization in %.1f seconds",
                        config.reconnect_delay_seconds,
                    )
                    await asyncio.sleep(config.reconnect_delay_seconds)

            if control_stdin:
                await _stdin_lifecycle_loop(logger)
            else:
                await asyncio.Event().wait()
        finally:
            await manager.close()

    return 0


async def _stdin_lifecycle_loop(logger: logging.Logger) -> int:
    """Accept process-lifecycle commands without handling capture START/STOP."""

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
                # The event loop may already be closed after a quit command.
                pass

    threading.Thread(
        target=_read_stdin,
        name="ble-ui-stdin",
        daemon=True,
    ).start()
    print(
        "[CONTROL] stdin mode: quit/exit stops BLE-TimeSync; START/STOP is handled by Master-Serial-Control.",
        flush=True,
    )

    while True:
        command = await commands.get()
        if command is None:
            logger.info("Control stdin closed; exiting")
            return 0

        normalized = command.casefold()
        if normalized in {"q", "quit", "exit"}:
            logger.info("Process manager requested BLE-TimeSync exit")
            return 0
        if normalized in {"1", "start", "0", "stop"}:
            logger.warning(
                "Ignoring capture control command %r: BLE-TimeSync is time-sync only; use Master-Serial-Control",
                command,
            )
        elif normalized:
            logger.warning("Ignoring unknown stdin command: %s", command)


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
        command = lambda: _run(
            config,
            logger,
            args.no_compensation,
            args.control_stdin,
        )

    try:
        return asyncio.run(command())
    except KeyboardInterrupt:
        logger.info("Ctrl+C received; notifications stopped and BLE disconnected")
        return 130
    except (BleAdapterUnavailableError, BleDependencyError, TargetNotFoundError) as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("Command failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Stage-by-stage Mode2 coordinator/head BLE diagnostic runner.

The runner deliberately separates idle keepalive, scheduled UTC delivery, and
START/STOP so a disconnect can be attributed to the first phase that fails.
It writes every serial RX/TX line with wall-clock and monotonic timestamps.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import queue
import re
import threading
import time
from typing import Callable

import serial


NODE_STATUS_RE = re.compile(
    r"^node=(\d+) connected=([01]) state=([A-Z_]+) session=(\d+) "
    r"error=(0x[0-9a-fA-F]+) samples=(\d+) rtt=(-?\d+)us .* fresh=(-?\d+)ms$"
)
TIME_REPLY_RE = re.compile(r"^TIME_REPLY (\d+) (-?\d+) (-?\d+)$")
TIME_ACCEPT_RE = re.compile(
    r"^TIME_ACCEPT seq=(\d+) nodes=(\d+) uncertainty_us=(\d+)$"
)
STOP_FRAME_RE = re.compile(r"^STOP_FRAME session=(\d+) node=(\d+) frames=(-?\d+)$")


@dataclass(frozen=True)
class Event:
    monotonic_ns: int
    line: str


@dataclass(frozen=True)
class TimeSample:
    sequence: int
    coordinator_receive_us: int
    coordinator_transmit_us: int
    t1_wall_ns: int
    t1_monotonic_ns: int
    t4_monotonic_ns: int

    @property
    def net_rtt_us(self) -> float:
        total = (self.t4_monotonic_ns - self.t1_monotonic_ns) / 1_000.0
        processing = self.coordinator_transmit_us - self.coordinator_receive_us
        return max(0.0, total - processing)

    @property
    def coordinator_ref_us(self) -> int:
        return (self.coordinator_receive_us + self.coordinator_transmit_us) // 2

    @property
    def utc_ref_ns(self) -> int:
        return self.t1_wall_ns + (
            self.t4_monotonic_ns - self.t1_monotonic_ns
        ) // 2

    @property
    def uncertainty_us(self) -> int:
        return min(1_000_000, max(1, math.ceil(self.net_rtt_us / 2.0)))


class DiagnosticSerial:
    def __init__(self, port: str, baud: int, log_path: Path) -> None:
        self.port = serial.Serial(
            port=port,
            baudrate=baud,
            timeout=0.05,
            write_timeout=1.0,
            rtscts=False,
            dsrdtr=False,
        )
        self.log_file = log_path.open("w", encoding="utf-8", buffering=1)
        self.events: list[Event] = []
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.reader = threading.Thread(
            target=self._reader_loop, name="diagnostic-serial-reader", daemon=True
        )
        self.reader.start()

    def _record(self, direction: str, value: str, monotonic_ns: int | None = None) -> None:
        if monotonic_ns is None:
            monotonic_ns = time.perf_counter_ns()
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        text = f"{stamp} mono_us={monotonic_ns // 1000} {direction} {value}"
        print(text, flush=True)
        self.log_file.write(text + "\n")

    def _reader_loop(self) -> None:
        buffer = bytearray()
        while not self.stop_event.is_set():
            try:
                data = self.port.read(256)
            except Exception as exc:  # pragma: no cover - hardware path
                self._record("ERR", f"serial read failed: {exc}")
                return
            if not data:
                continue
            buffer.extend(data)
            while b"\n" in buffer:
                raw, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                line = raw.decode("utf-8", errors="replace").strip("\x00\r ")
                if not line:
                    continue
                received_ns = time.perf_counter_ns()
                self._record("RX", line, received_ns)
                with self.condition:
                    self.events.append(Event(received_ns, line))
                    self.condition.notify_all()

    def send(self, command: str) -> None:
        payload = (command.rstrip("\r\n") + "\n").encode("ascii")
        self._record("TX", command.rstrip())
        self.port.write(payload)
        self.port.flush()

    def mark(self, text: str) -> None:
        self._record("MARK", text)

    def cursor(self) -> int:
        with self.condition:
            return len(self.events)

    def wait_for(
        self,
        predicate: Callable[[Event], bool],
        timeout: float,
        start: int | None = None,
    ) -> Event:
        if start is None:
            start = self.cursor()
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for event in self.events[start:]:
                    if predicate(event):
                        return event
                start = len(self.events)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("expected serial line was not received")
                self.condition.wait(remaining)

    def close(self) -> None:
        self.stop_event.set()
        self.reader.join(timeout=1.0)
        self.port.close()
        self.log_file.close()


class StageRunner:
    def __init__(self, link: DiagnosticSerial, node_id: int) -> None:
        self.link = link
        self.node_id = node_id
        self.sequence = 1000

    def _next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def query_node(self, timeout: float = 6.0) -> tuple[bool, str, int]:
        cursor = self.link.cursor()
        self.link.send("STATUS")

        def matches(event: Event) -> bool:
            match = NODE_STATUS_RE.fullmatch(event.line)
            return match is not None and int(match.group(1)) == self.node_id

        event = self.link.wait_for(matches, timeout, cursor)
        match = NODE_STATUS_RE.fullmatch(event.line)
        assert match is not None
        return match.group(2) == "1", match.group(3), int(match.group(8))

    def ensure_connected(self) -> None:
        try:
            connected, state, fresh_ms = self.query_node()
            self.link.mark(
                f"preflight node={self.node_id} connected={int(connected)} "
                f"state={state} fresh_ms={fresh_ms}"
            )
        except TimeoutError:
            connected = False

        if not connected:
            cursor = self.link.cursor()
            self.link.send("SCAN")
            self.link.wait_for(
                lambda event: event.line.startswith(
                    f"connected node {self.node_id} at "
                ),
                12.0,
                cursor,
            )
            time.sleep(2.0)

        connected, state, fresh_ms = self.query_node()
        if not connected:
            raise RuntimeError("head did not remain connected after SCAN")
        self.link.mark(
            f"connected node={self.node_id} state={state} fresh_ms={fresh_ms}"
        )

    def monitor_connected(self, phase: str, duration: float) -> None:
        self.link.mark(f"PHASE_BEGIN {phase} duration_s={duration:.1f}")
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            connected, state, fresh_ms = self.query_node()
            self.link.mark(
                f"PHASE_SAMPLE {phase} connected={int(connected)} "
                f"state={state} fresh_ms={fresh_ms}"
            )
            if not connected:
                raise RuntimeError(f"disconnect detected during {phase}")
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        self.link.mark(f"PHASE_PASS {phase}")

    def request_time(self) -> TimeSample:
        sequence = self._next_sequence()
        cursor = self.link.cursor()
        before_ns = time.perf_counter_ns()
        wall_ns = time.time_ns()
        after_ns = time.perf_counter_ns()
        midpoint_ns = (before_ns + after_ns) // 2
        t1_wall_ns = wall_ns + (after_ns - midpoint_ns)
        self.link.send(f"TIME_QUERY {sequence}")

        def matches(event: Event) -> bool:
            match = TIME_REPLY_RE.fullmatch(event.line)
            return match is not None and int(match.group(1)) == sequence

        # USB CDC output can occasionally be delivered to Windows in a batch
        # several seconds after the coordinator timestamped the command.
        event = self.link.wait_for(matches, 8.0, cursor)
        match = TIME_REPLY_RE.fullmatch(event.line)
        assert match is not None
        return TimeSample(
            sequence=sequence,
            coordinator_receive_us=int(match.group(2)),
            coordinator_transmit_us=int(match.group(3)),
            t1_wall_ns=t1_wall_ns,
            t1_monotonic_ns=after_ns,
            t4_monotonic_ns=event.monotonic_ns,
        )

    def synchronize(self) -> None:
        samples: list[TimeSample] = []
        for index in range(10):
            samples.append(self.request_time())
            if index != 9:
                time.sleep(0.05)
        selected = min(samples, key=lambda item: item.net_rtt_us)
        cursor = self.link.cursor()
        self.link.send(
            f"TIME_SET {selected.sequence} {selected.coordinator_ref_us} "
            f"{selected.utc_ref_ns} {selected.uncertainty_us}"
        )

        def accepted(event: Event) -> bool:
            match = TIME_ACCEPT_RE.fullmatch(event.line)
            return match is not None and int(match.group(1)) == selected.sequence

        event = self.link.wait_for(accepted, 4.0, cursor)
        match = TIME_ACCEPT_RE.fullmatch(event.line)
        assert match is not None
        self.link.mark(
            f"TIME_SYNC_PASS seq={selected.sequence} nodes={match.group(2)} "
            f"net_rtt_us={selected.net_rtt_us:.1f}"
        )

    def periodic_time_phase(self, duration: float, interval: float) -> None:
        self.link.mark(
            f"PHASE_BEGIN periodic_time duration_s={duration:.1f} "
            f"interval_s={interval:.1f}"
        )
        deadline = time.monotonic() + duration
        cycle = 0
        while time.monotonic() < deadline:
            cycle += 1
            self.synchronize()
            apply_deadline = time.monotonic() + 2.2
            while time.monotonic() < apply_deadline:
                connected, state, fresh_ms = self.query_node()
                if not connected:
                    raise RuntimeError(
                        "disconnect/status stall after TIME_SET "
                        f"cycle={cycle} connected={connected} fresh={fresh_ms}ms"
                    )
                time.sleep(0.5)
            next_cycle = min(deadline, time.monotonic() + max(0.0, interval - 2.2))
            while time.monotonic() < next_cycle:
                connected, state, fresh_ms = self.query_node()
                if not connected:
                    raise RuntimeError(
                        f"disconnect/status stall during periodic_time cycle={cycle}"
                    )
                time.sleep(max(0.0, min(2.0, next_cycle - time.monotonic())))
        self.link.mark(f"PHASE_PASS periodic_time cycles={cycle}")

    def start_stop_phase(self, run_seconds: float) -> None:
        self.link.mark(f"PHASE_BEGIN start_stop run_s={run_seconds:.1f}")
        self.synchronize()
        time.sleep(2.0)
        session = int(time.time()) & 0xFFFFFFFF
        cursor = self.link.cursor()
        self.link.send(f"START {session}")
        arm = self.link.wait_for(
            lambda event: (
                event.line.startswith(f"session {session} ARMED")
                or event.line.startswith("START rejected:")
                or event.line.startswith("START aborted:")
            ),
            10.0,
            cursor,
        )
        if " ARMED" not in arm.line:
            raise RuntimeError(arm.line)

        saw_running = False
        deadline = time.monotonic() + run_seconds
        while time.monotonic() < deadline:
            connected, state, fresh_ms = self.query_node()
            self.link.mark(
                f"START_SAMPLE session={session} connected={int(connected)} "
                f"state={state} fresh_ms={fresh_ms}"
            )
            if not connected:
                self.link.send("STOP")
                raise RuntimeError(
                    "disconnect/status stall after START "
                    f"connected={connected} state={state} fresh={fresh_ms}ms"
                )
            saw_running = saw_running or state == "RUNNING"
            time.sleep(1.0)

        cursor = self.link.cursor()
        self.link.send("STOP")
        self.link.wait_for(
            lambda event: event.line.startswith("STOP scheduled at coordinator="),
            5.0,
            cursor,
        )
        if not saw_running:
            raise RuntimeError("session never reached RUNNING before STOP")
        frame_event = self.link.wait_for(
            lambda event: (
                (match := STOP_FRAME_RE.fullmatch(event.line)) is not None
                and int(match.group(1)) == session
                and int(match.group(2)) == self.node_id
            ),
            12.0,
            cursor,
        )
        frame_match = STOP_FRAME_RE.fullmatch(frame_event.line)
        assert frame_match is not None
        frames = int(frame_match.group(3))
        if frames <= 0:
            raise RuntimeError(f"invalid STOP_FRAME count: {frames}")
        self.link.mark(
            f"STOP_FRAME_PASS session={session} node={self.node_id} frames={frames}"
        )
        self.link.mark(f"PHASE_PASS start_stop session={session}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM14")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--node", type=int, default=1)
    parser.add_argument("--settle", type=float, default=3.0)
    parser.add_argument("--idle-seconds", type=float, default=75.0)
    parser.add_argument("--time-seconds", type=float, default=45.0)
    parser.add_argument("--time-interval", type=float, default=10.0)
    parser.add_argument("--run-seconds", type=float, default=15.0)
    parser.add_argument("--abort-only", action="store_true")
    parser.add_argument(
        "--stop-after", choices=("idle", "time", "start"), default="start"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"ble_stage_diagnostic_{stamp}.log"
    link = DiagnosticSerial(args.port, args.baud, log_path)
    runner = StageRunner(link, args.node)
    exit_code = 1
    try:
        link.mark(f"TEST_BEGIN log={log_path}")
        time.sleep(args.settle)
        runner.ensure_connected()
        if args.abort_only:
            link.send("ABORT")
            time.sleep(1.0)
            connected, state, fresh_ms = runner.query_node(timeout=6.0)
            if not connected or state != "IDLE":
                raise RuntimeError(
                    f"ABORT did not restore IDLE: connected={connected} state={state}"
                )
            link.mark(f"ABORT_PASS state={state} fresh_ms={fresh_ms}")
            return 0
        runner.monitor_connected("idle_keepalive", args.idle_seconds)
        if args.stop_after == "idle":
            link.mark("TEST_PASS idle stage")
            return 0
        runner.periodic_time_phase(args.time_seconds, args.time_interval)
        if args.stop_after == "time":
            link.mark("TEST_PASS idle and time stages")
            return 0
        runner.start_stop_phase(args.run_seconds)
        link.mark("TEST_PASS all stages")
        exit_code = 0
    except Exception as exc:
        link.mark(f"TEST_FAIL {type(exc).__name__}: {exc}")
        try:
            link.send("ABORT")
            link.mark("FAILURE_CLEANUP ABORT sent")
            time.sleep(1.0)
            link.send("STATUS")
            link.mark("FAILURE_CAPTURE waiting_s=10.0 for BLE disconnect diagnostics")
            time.sleep(10.0)
            link.send("STATUS")
            time.sleep(1.0)
        except Exception:
            pass
    finally:
        link.close()
    print(f"Diagnostic log: {log_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

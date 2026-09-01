"""Unattended 30-minute RGBD/Mode2 hardware stress test."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import diagnose_ble_stages as diag


STATS_RE = re.compile(
    r"^\[STATS\] rec=(ON|OFF) capture_fps=([0-9.]+).*"
    r"queue_drop=(\d+) oversize_drop=(\d+).*$"
)
STOP_FRAME_RE = re.compile(r"^STOP_FRAME session=(\d+) node=(\d+) frames=(-?\d+)$")
REMOTE_ERROR_RE = re.compile(
    r"(?:RealSense|camera|pipeline|device).*(?:error|failed|disconnect)", re.I
)


@dataclass
class EpisodeResult:
    index: int
    session: int
    requested_seconds: int
    started_at: str
    stopped_at: str = ""
    min_capture_fps: float = 999.0
    stats_count: int = 0
    head_frames: int | None = None
    left_frames: int | None = None
    right_frames: int | None = None
    success: bool = False
    error: str = ""


class CombinedLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, source: str, text: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        line = f"{stamp} mono={time.monotonic():.3f} {source} {text.rstrip()}"
        with self._lock:
            print(line, flush=True)
            self._file.write(line + "\n")

    def close(self) -> None:
        self._file.close()


class RemoteTail:
    def __init__(
        self,
        host: str,
        camera_log: str,
        uart_log: str,
        combined: CombinedLog,
        local_tail_file: Path | None = None,
    ) -> None:
        self.combined = combined
        self.last_stats_mono = 0.0
        self.latest_fps = -1.0
        self.latest_rec = "UNKNOWN"
        self.stats: list[tuple[float, str, float, int, int]] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self.local_tail_file = local_tail_file
        self.process: subprocess.Popen[str] | None = None
        if local_tail_file is None:
            command = [
                "ssh",
                "-o", "PreferredAuthentications=password",
                "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=3",
                host,
                f"tail -n 0 -F -- {camera_log} {uart_log}",
            ]
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=os.environ.copy(),
            )
            target = self._reader_process
        else:
            target = self._reader_file
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def _consume(self, line: str) -> None:
        self.combined.write("NANOPI", line)
        match = STATS_RE.fullmatch(line)
        if match:
            now = time.monotonic()
            rec, fps, queue_drop, oversize_drop = match.groups()
            self.last_stats_mono = now
            self.latest_fps = float(fps)
            self.latest_rec = rec
            self.stats.append(
                (now, rec, float(fps), int(queue_drop), int(oversize_drop))
            )
        elif REMOTE_ERROR_RE.search(line):
            self.errors.append(line)

    def _reader_process(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            self._consume(raw.rstrip("\r\n"))

    def _reader_file(self) -> None:
        assert self.local_tail_file is not None
        deadline = time.monotonic() + 15.0
        while not self.local_tail_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not self.local_tail_file.exists():
            self.errors.append("local SSH tail mirror was not created")
            return
        with self.local_tail_file.open(
            "r", encoding="utf-8", errors="replace"
        ) as handle:
            handle.seek(0, 2)
            while not self._stop.is_set():
                line = handle.readline()
                if line:
                    self._consume(line.rstrip("\r\n"))
                else:
                    time.sleep(0.1)

    def check_alive(self) -> None:
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                raise RuntimeError(f"NanoPi SSH tail exited with code {code}")
        elif self.errors and self.last_stats_mono <= 0:
            raise RuntimeError(self.errors[-1])

    def close(self) -> None:
        self._stop.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._thread.join(timeout=1)


class StressRunner:
    def __init__(
        self,
        link: diag.DiagnosticSerial,
        tail: RemoteTail,
        combined: CombinedLog,
        total_seconds: int,
        expected_nodes: tuple[int, ...] = (1, 3),
        profile: str = "30min",
    ) -> None:
        self.link = link
        self.tail = tail
        self.combined = combined
        self.total_seconds = total_seconds
        self.expected_nodes = expected_nodes
        self.profile = profile
        self.stage = diag.StageRunner(link, 1)
        self.serial_cursor = 0
        self.status: dict[int, tuple[bool, str, int, float]] = {}
        self.active_session = 0
        self.results: list[EpisodeResult] = []

    def mark(self, text: str) -> None:
        self.link.mark(text)
        self.combined.write("TEST", text)

    def _drain_serial_status(self) -> None:
        with self.link.condition:
            events = list(self.link.events[self.serial_cursor :])
            self.serial_cursor = len(self.link.events)
        for event in events:
            match = diag.NODE_STATUS_RE.fullmatch(event.line)
            if match:
                node = int(match.group(1))
                self.status[node] = (
                    match.group(2) == "1",
                    match.group(3),
                    int(match.group(8)),
                    event.monotonic_ns / 1_000_000_000.0,
                )
            self.combined.write("ESP32", event.line)

    def _request_status(self) -> None:
        self.link.send("STATUS")
        self.combined.write("HOST", "TX STATUS")

    def wait_for_nodes(self, expected_state: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        next_status = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_status:
                self._request_status()
                next_status = now + 2.0
            time.sleep(0.2)
            self._drain_serial_status()
            good = True
            for node in self.expected_nodes:
                item = self.status.get(node)
                if item is None or not item[0] or item[1] != expected_state:
                    good = False
            if good:
                return
        snapshot = {node: self.status.get(node) for node in self.expected_nodes}
        raise RuntimeError(f"nodes not {expected_state}: {snapshot}")

    def connect_nodes(self) -> None:
        for attempt in range(1, 4):
            self.mark(f"SCAN_ATTEMPT {attempt}")
            self.link.send("SCAN")
            self.combined.write("HOST", "TX SCAN")
            time.sleep(9.0)
            try:
                self.wait_for_nodes("IDLE", 8.0)
                self.mark(f"NODES_CONNECTED nodes={self.expected_nodes}")
                return
            except RuntimeError as exc:
                self.mark(f"SCAN_RETRY reason={exc}")
        raise RuntimeError(f"nodes {self.expected_nodes} did not all reach IDLE")

    def synchronize(self) -> None:
        self.stage.synchronize()
        self.combined.write("HOST", "TIME_SYNC_PASS")
        time.sleep(2.0)

    def _health_check(
        self,
        recording: bool,
        stats_start: int,
        low_fps_streak: int,
    ) -> int:
        self.tail.check_alive()
        self._drain_serial_status()
        now = time.monotonic()
        if self.tail.last_stats_mono <= 0 or now - self.tail.last_stats_mono > 10.0:
            raise RuntimeError("NanoPi camera STATS absent for >10 seconds")
        fps = self.tail.latest_fps
        if fps <= 1.0:
            raise RuntimeError(f"camera capture_fps critical: {fps:.1f}")
        low_fps_streak = low_fps_streak + 1 if fps < 20.0 else 0
        if low_fps_streak >= 3:
            raise RuntimeError(f"camera capture_fps stayed below 20: {fps:.1f}")
        for node in self.expected_nodes:
            item = self.status.get(node)
            if item is None or now - item[3] > 12.0:
                raise RuntimeError(f"node {node} status stale")
            connected, state, fresh_ms, _ = item
            if not connected:
                raise RuntimeError(f"node {node} disconnected")
            # A single BLE fit sample can be delayed for roughly 3 seconds while
            # the link stays connected and immediately recovers. START performs
            # a fresh synchronization, so reserve failure for a longer outage.
            if fresh_ms > 5000:
                raise RuntimeError(f"node {node} timesync stale: {fresh_ms}ms")
            if recording and state not in {"ARMED", "RUNNING"}:
                raise RuntimeError(f"node {node} unexpected recording state: {state}")
            if not recording and state != "IDLE":
                raise RuntimeError(f"node {node} unexpected idle state: {state}")
        new_stats = self.tail.stats[stats_start:]
        if new_stats:
            if any(item[3] != 0 or item[4] != 0 for item in new_stats):
                raise RuntimeError("NanoPi queue_drop or oversize_drop became non-zero")
        return low_fps_streak

    def monitor(self, seconds: float, recording: bool, stats_start: int) -> None:
        deadline = time.monotonic() + seconds
        next_status = 0.0
        low_fps_streak = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_status:
                self._request_status()
                next_status = now + 4.0
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            low_fps_streak = self._health_check(
                recording, stats_start, low_fps_streak
            )

    def stop_episode(self, session: int) -> dict[int, int]:
        cursor = self.link.cursor()
        self.link.send("STOP")
        self.combined.write("HOST", f"TX STOP session={session}")
        self.link.wait_for(
            lambda event: event.line.startswith("STOP scheduled at coordinator="),
            10.0,
            cursor,
        )
        frames: dict[int, int] = {}
        deadline = time.monotonic() + 20.0
        scan = cursor
        while time.monotonic() < deadline and len(frames) < len(self.expected_nodes):
            with self.link.condition:
                events = list(self.link.events[scan:])
                scan = len(self.link.events)
            for event in events:
                match = STOP_FRAME_RE.fullmatch(event.line)
                if match and int(match.group(1)) == session:
                    frames[int(match.group(2))] = int(match.group(3))
            if len(frames) < len(self.expected_nodes):
                time.sleep(0.2)
        self.active_session = 0
        self.wait_for_nodes("IDLE", 15.0)
        if set(frames) != set(self.expected_nodes):
            raise RuntimeError(f"missing STOP_FRAME: {frames}")
        return frames

    def emergency_abort(self, reason: str) -> None:
        self.mark(f"EMERGENCY reason={reason}")
        try:
            if self.active_session:
                self.link.send("STOP")
                self.combined.write("HOST", "TX STOP emergency")
                time.sleep(3.0)
            self.link.send("ABORT")
            self.combined.write("HOST", "TX ABORT emergency")
            time.sleep(2.0)
        except Exception as exc:
            self.mark(f"EMERGENCY_CLEANUP_ERROR {exc}")
        self.active_session = 0

    def run_episode(self, index: int, duration: int) -> None:
        self.wait_for_nodes("IDLE", 15.0)
        self.synchronize()
        session = (int(time.time()) + index) & 0xFFFFFFFF
        result = EpisodeResult(
            index=index,
            session=session,
            requested_seconds=duration,
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self.results.append(result)
        stats_start = len(self.tail.stats)
        cursor = self.link.cursor()
        self.link.send(f"START {session}")
        self.combined.write("HOST", f"TX START session={session} duration={duration}")
        arm = self.link.wait_for(
            lambda event: (
                event.line.startswith(f"session {session} ARMED")
                or event.line.startswith("START rejected:")
                or event.line.startswith("START aborted:")
            ),
            15.0,
            cursor,
        )
        if " ARMED" not in arm.line:
            raise RuntimeError(arm.line)
        self.active_session = session
        self.mark(f"EPISODE_BEGIN index={index} session={session} seconds={duration}")
        try:
            self.monitor(duration, True, stats_start)
            frames = self.stop_episode(session)
            episode_stats = self.tail.stats[stats_start:]
            result.min_capture_fps = min(item[2] for item in episode_stats)
            result.stats_count = len(episode_stats)
            result.head_frames = frames.get(1)
            result.left_frames = frames.get(2)
            result.right_frames = frames.get(3)
            result.stopped_at = datetime.now().astimezone().isoformat(timespec="seconds")
            result.success = True
            self.mark(
                f"EPISODE_PASS index={index} session={session} "
                f"frames={frames} "
                f"min_fps={result.min_capture_fps:.1f}"
            )
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.stopped_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.emergency_abort(result.error)
            raise

    def run(self) -> None:
        started = time.monotonic()
        stats_start = len(self.tail.stats)
        self.connect_nodes()
        self.wait_for_nodes("IDLE", 10.0)
        if self.profile == "10min-5":
            initial_idle = 25
            durations = [35, 55, 42, 70, 48]
            rests = [38, 57, 31, 49]
        else:
            initial_idle = 45
            durations = [35, 78, 48, 90, 32, 67, 55, 84]
            rests = [92, 167, 61, 213, 104, 188, 76]
        self.mark(f"INITIAL_IDLE_BEGIN seconds={initial_idle}")
        self.monitor(initial_idle, False, stats_start)
        for index, duration in enumerate(durations, start=1):
            self.run_episode(index, duration)
            if index <= len(rests):
                rest = rests[index - 1]
                self.mark(f"INTERVAL_BEGIN after={index} seconds={rest}")
                self.monitor(rest, False, len(self.tail.stats))
                self.mark(f"INTERVAL_PASS after={index}")

        remaining = self.total_seconds - (time.monotonic() - started)
        if remaining > 0:
            self.mark(f"FINAL_IDLE_BEGIN seconds={remaining:.1f}")
            self.monitor(remaining, False, len(self.tail.stats))
        self.mark(
            f"TEST_PASS elapsed={time.monotonic() - started:.1f}s "
            f"episodes={len(self.results)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM14")
    parser.add_argument("--host", default="pi@192.168.8.68")
    parser.add_argument("--camera-log", required=True)
    parser.add_argument("--uart-log", required=True)
    parser.add_argument("--remote-tail-file", type=Path)
    parser.add_argument("--total-seconds", type=int, default=1800)
    parser.add_argument("--nodes", default="1,3")
    parser.add_argument("--profile", choices=("30min", "10min-5"), default="30min")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    combined_path = log_dir / f"rgbd_stress_{stamp}.log"
    serial_path = log_dir / f"rgbd_stress_serial_{stamp}.log"
    summary_path = log_dir / f"rgbd_stress_{stamp}.json"
    combined = CombinedLog(combined_path)
    link: diag.DiagnosticSerial | None = None
    tail: RemoteTail | None = None
    runner: StressRunner | None = None
    exit_code = 1
    try:
        combined.write("TEST", f"BEGIN total_seconds={args.total_seconds}")
        tail = RemoteTail(
            args.host,
            args.camera_log,
            args.uart_log,
            combined,
            args.remote_tail_file,
        )
        link = diag.DiagnosticSerial(args.port, 115200, serial_path)
        expected_nodes = tuple(int(item) for item in args.nodes.split(","))
        if not expected_nodes or len(set(expected_nodes)) != len(expected_nodes):
            raise ValueError("--nodes must contain unique comma-separated node IDs")
        runner = StressRunner(
            link,
            tail,
            combined,
            args.total_seconds,
            expected_nodes=expected_nodes,
            profile=args.profile,
        )
        time.sleep(3.0)
        runner.run()
        exit_code = 0
    except Exception as exc:
        combined.write("TEST", f"FAIL {type(exc).__name__}: {exc}")
        if runner is not None:
            runner.emergency_abort(f"top-level {type(exc).__name__}: {exc}")
    finally:
        if runner is not None:
            summary = {
                "success": exit_code == 0,
                "episodes": [asdict(item) for item in runner.results],
                "latest_fps": tail.latest_fps if tail else None,
                "latest_rec": tail.latest_rec if tail else None,
                "remote_errors": tail.errors if tail else [],
                "combined_log": str(combined_path),
                "serial_log": str(serial_path),
            }
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if link is not None:
            link.close()
        if tail is not None:
            tail.close()
        combined.write("TEST", f"END exit_code={exit_code} summary={summary_path}")
        combined.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

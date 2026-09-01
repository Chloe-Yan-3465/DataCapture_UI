"""Independent capture processes and coordinated start/stop behavior."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable


ROOT_DIR = Path(__file__).resolve().parent.parent
TASK_OPTIONS_PATH = Path(__file__).resolve().parent / "config" / "task_options.json"
DEFAULT_TASK_NAMES = (
    "single_arm_pick",
    "limited_space",
    "dual_arm_interaction",
    "test",
)
COMPLEX_LEVELS = ("L0", "L1", "L_test")
PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
BLE_DIR = ROOT_DIR / "BLE-TimeSync"
MANUS_DIR = ROOT_DIR / "manus_vive_com"
VR_OUTPUT_ROOT = ROOT_DIR / "vr_data"
MANUS_RECORDER = MANUS_DIR / "capture_recorder.py"
MANUS_CONFIG = MANUS_DIR / "capture_config.yaml"
MANUS_REQUIREMENTS = MANUS_DIR / "requirements_capture.txt"
MANUS_CLIENT = (
    MANUS_DIR
    / "Output"
    / "x64"
    / "Debug"
    / "SDKMinimalClient_Windows_2_4_120hz.exe"
)

MODE2_NODE_PATTERN = re.compile(
    r"node=(?P<node_id>\d+)\s+connected=(?P<connected>[01])\s+"
    r"state=(?P<state>[A-Z0-9_]+)(?P<details>.*)",
    re.IGNORECASE,
)
MODE2_READY_PATTERN = re.compile(
    r"\[READY\]\s+(?P<name>.+?)\s+via\s+(?P<serial>\S+)", re.IGNORECASE
)
MODE2_STOP_FRAME_PATTERN = re.compile(
    r"\bSTOP_FRAME\s+session=(?P<session>\d+)\s+"
    r"node=(?P<node_id>\d+)\s+frames=(?P<frames>-?\d+)\b",
    re.IGNORECASE,
)
TRACKER_STATUS_PATTERN = re.compile(
    r"\[TRACKER_STATUS\]\s+role=(?P<role>\S+)\s+"
    r"serial=(?P<serial>\S+)\s+connected=(?P<connected>[01])\s+"
    r"tracking=(?P<tracking>[01])",
    re.IGNORECASE,
)

BLE_CONTROL_TOKENS = (
    "[CONTROL]",
    "[SCAN]",
    "[START]",
    "[START FAILED]",
    "[STOP]",
    "START REJECTED",
    "START ABORTED",
    "START IGNORED",
    "START DID NOT RECEIVE",
    "STOP DID NOT RECEIVE",
    "START SCHEDULED",
    "STOP SCHEDULED",
    "STOP_FRAME",
    " ARMED ",
    "NO ACTIVE SESSION",
    "SCAN ",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_capture_segment(value: str, name: str, maximum: int) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if (
        not clean
        or clean == "__add__"
        or len(clean) > maximum
        or PATH_SEGMENT_PATTERN.fullmatch(clean) is None
    ):
        raise RuntimeError(
            f"{name} 只能包含字母、数字、下划线或连字符，长度不超过 {maximum}"
        )
    return clean


def _load_task_names() -> list[str]:
    names = list(DEFAULT_TASK_NAMES)
    try:
        data = json.loads(TASK_OPTIONS_PATH.read_text(encoding="utf-8"))
        saved = data.get("task_names", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        saved = []
    for value in saved:
        try:
            task = _validate_capture_segment(value, "task_name", 31)
        except RuntimeError:
            continue
        if task not in names:
            names.append(task)
    return names


class LogStore:
    def __init__(self, limit: int = 3000) -> None:
        self._lines: deque[dict] = deque(maxlen=limit)
        self._sequence = 0
        self._condition = threading.Condition()

    def append(self, text: str, stream: str = "output") -> int:
        clean = text.rstrip("\r\n")
        if not clean:
            return self.last_sequence
        with self._condition:
            self._sequence += 1
            self._lines.append(
                {
                    "seq": self._sequence,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "stream": stream,
                    "text": clean,
                }
            )
            self._condition.notify_all()
            return self._sequence

    @property
    def last_sequence(self) -> int:
        with self._condition:
            return self._sequence

    def since(self, sequence: int) -> tuple[list[dict], int]:
        with self._condition:
            lines = [dict(item) for item in self._lines if item["seq"] > sequence]
            return lines, self._sequence

    def contains_after(self, text: str, sequence: int) -> bool:
        with self._condition:
            return any(
                item["seq"] > sequence and text in item["text"]
                for item in self._lines
            )

    def wait_for(self, text: str, sequence: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if any(
                    item["seq"] > sequence and text in item["text"]
                    for item in self._lines
                ):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.25))

    def clear(self) -> None:
        with self._condition:
            self._lines.clear()
            self._condition.notify_all()


class ProjectProcess:
    def __init__(
        self,
        key: str,
        title: str,
        on_line: Callable[[str, str], None] | None = None,
        on_exit: Callable[[str, int], None] | None = None,
    ) -> None:
        self.key = key
        self.title = title
        self.logs = LogStore()
        self._on_line = on_line
        self._on_exit = on_exit
        self._lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.status = "stopped"
        self.exit_code: int | None = None
        self.started_at: str | None = None
        self.last_error: str | None = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def start(self, command: list[str], cwd: Path) -> None:
        with self._lock:
            if self.is_active:
                raise RuntimeError(f"{self.title} is already running")
            self.exit_code = None
            self.last_error = None
            self.started_at = _now()
            self.status = "starting"
            self.logs.append(f"[UI] Working directory: {cwd}", "system")
            self.logs.append(
                "[UI] Command: " + subprocess.list2cmdline(command), "system"
            )
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                self.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=environment,
                    creationflags=creation_flags,
                )
            except Exception as exc:
                self.status = "error"
                self.last_error = str(exc)
                self.logs.append(f"[UI] Failed to start: {exc}", "system")
                raise
            self.status = "running"
            threading.Thread(
                target=self._read_output,
                name=f"{self.key}-output",
                daemon=True,
            ).start()

    def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                self.logs.append(line)
                if self._on_line is not None:
                    self._on_line(self.key, line.rstrip("\r\n"))
        except Exception as exc:
            self.logs.append(f"[UI] Output reader failed: {exc}", "system")
        finally:
            exit_code = process.wait()
            process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()
            with self._lock:
                self.exit_code = exit_code
                self.status = "exited" if exit_code == 0 else "error"
                if exit_code != 0:
                    self.last_error = f"Process exited with code {exit_code}"
            self.logs.append(f"[UI] Process exited with code {exit_code}.", "system")
            if self._on_exit is not None:
                self._on_exit(self.key, exit_code)

    def send_line(self, command: str) -> bool:
        with self._lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                process.stdin.write(command + "\n")
                process.stdin.flush()
                self.logs.append(f"[UI -> process] {command}", "system")
                return True
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.logs.append(f"[UI] Could not send command: {exc}", "system")
                return False

    def wait(self, timeout: float) -> bool:
        process = self.process
        if process is None:
            return True
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def force_stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        self.logs.append("[UI] Graceful stop timed out; interrupting process.", "system")
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.logs.append("[UI] Interrupt timed out; terminating process.", "system")
        try:
            process.terminate()
            process.wait(timeout=3)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.logs.append("[UI] Terminate timed out; killing process.", "system")
        try:
            process.kill()
        except OSError:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            process = self.process
            pid = process.pid if process is not None and process.poll() is None else None
            return {
                "key": self.key,
                "title": self.title,
                "status": self.status,
                "pid": pid,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "last_error": self.last_error,
            }


class CaptureCoordinator:
    """Keep BLE time sync alive while capture sessions start and stop independently."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ble_ready = threading.Event()
        self._manus_recorder_ready = threading.Event()
        self._streams_ready = threading.Event()
        self._capture_ready = threading.Event()
        self._capture_saved = threading.Event()
        self._capture_start_error: str | None = None
        self._ble_start_result = threading.Event()
        self._ble_stop_result = threading.Event()
        self._ble_stop_requested = threading.Event()
        self._stream_stop_requested = threading.Event()
        self._capture_stop_requested = threading.Event()
        self._shutting_down = False
        self.phase = "idle"
        self.message = "请先开启授时与 Tracker/MANUS 数据流"
        self.error: str | None = None
        self.capture_started_at: str | None = None
        self.last_stopped_at: str | None = None
        self.capture_output_dir: str | None = None
        self._task_names = _load_task_names()
        self._capture_task_name = self._task_names[0]
        self._capture_complex_level = COMPLEX_LEVELS[0]
        self.ble_timesync_logs = LogStore()
        self.ble_control_logs = LogStore()
        self._ble_start_ok: bool | None = None
        self._ble_stop_ok: bool | None = None
        self._ble_control_state = "OFFLINE"
        self._ble_control_result = "OFFLINE"
        self._ble_time_sync_state = "STOPPED"
        self._coordinator_name = "Mode2Coordinator"
        self._coordinator_serial = "未连接"
        self._utc_map_state = "UNKNOWN"
        self._node_states: dict[str, dict[str, object]] = {}
        self._frame_report_status = "idle"
        self._frame_report_session: str | None = None
        self._frame_report_expected_nodes: set[str] = set()
        self._frame_report_nodes: dict[str, dict[str, object]] = {}
        self._tracker_states: dict[str, dict[str, object]] = {}
        self.ble = ProjectProcess("ble", "BLE-TimeSync", self._on_line, self._on_exit)
        self.manus = ProjectProcess(
            "manus", "MANUS + Tracker recorder", self._on_line, self._on_exit
        )
        self.manus_client = ProjectProcess(
            "manus_client", "MANUS SDK client", self._on_line, self._on_exit
        )
        # Present the Python recorder and C++ SDK client as one ordered terminal.
        self.manus_client.logs = self.manus.logs

    def preflight(self) -> dict:
        uv_path = shutil.which("uv")
        checks = [
            {
                "name": "Windows 平台",
                "ok": sys.platform == "win32",
                "detail": sys.platform,
            },
            {
                "name": "授时 Python 运行环境",
                "ok": (BLE_DIR / ".venv" / "Scripts" / "python.exe").is_file(),
                "detail": str(BLE_DIR / ".venv" / "Scripts" / "python.exe"),
            },
            {
                "name": "Mode2 授时入口",
                "ok": (BLE_DIR / "app" / "main.py").is_file(),
                "detail": str(BLE_DIR / "app" / "main.py"),
            },
            {
                "name": "uv",
                "ok": uv_path is not None,
                "detail": uv_path or "未找到",
            },
            {
                "name": "MANUS 联合采集入口",
                "ok": MANUS_RECORDER.is_file(),
                "detail": str(MANUS_RECORDER),
            },
            {
                "name": "MANUS 采集配置",
                "ok": MANUS_CONFIG.is_file() and MANUS_REQUIREMENTS.is_file(),
                "detail": f"{MANUS_CONFIG}; {MANUS_REQUIREMENTS}",
            },
            {
                "name": "MANUS 120Hz 客户端",
                "ok": MANUS_CLIENT.is_file(),
                "detail": str(MANUS_CLIENT),
            },
        ]
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    def _set_phase(self, phase: str, message: str, error: str | None = None) -> None:
        with self._lock:
            self.phase = phase
            self.message = message
            self.error = error

    def _start_frame_report(self) -> None:
        with self._lock:
            self._frame_report_status = "recording"
            self._frame_report_session = None
            self._frame_report_expected_nodes = {
                node_id
                for node_id, item in self._node_states.items()
                if item["connected"]
            }
            self._frame_report_nodes.clear()

    def _wait_for_frame_report(self) -> None:
        with self._lock:
            self._frame_report_status = "waiting"
            self._frame_report_session = None
            self._frame_report_expected_nodes = {
                node_id
                for node_id, item in self._node_states.items()
                if item["connected"]
            }
            self._frame_report_nodes.clear()

    def _require_checks(self, names: set[str]) -> None:
        checks = self.preflight()["checks"]
        failed = [item["name"] for item in checks if item["name"] in names and not item["ok"]]
        if failed:
            raise RuntimeError("启动检查未通过：" + "、".join(failed))

    def add_task_name(self, task_name: str) -> str:
        task = _validate_capture_segment(task_name, "task_name", 31)
        with self._lock:
            if task not in self._task_names:
                self._task_names.append(task)
                TASK_OPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
                temporary = TASK_OPTIONS_PATH.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(
                        {"task_names": self._task_names},
                        ensure_ascii=False,
                        indent=2,
                    ) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, TASK_OPTIONS_PATH)
        return task

    # BLE service lifecycle -------------------------------------------------
    def start_ble(self) -> None:
        self._require_checks(
            {
                "Windows 平台",
                "授时 Python 运行环境",
                "Mode2 授时入口",
                "uv",
                "MANUS 联合采集入口",
                "MANUS 采集配置",
                "MANUS 120Hz 客户端",
            }
        )
        with self._lock:
            if self.phase not in {"idle", "error"}:
                raise RuntimeError("当前状态不能启动 BLE 授时")
            if self.ble.is_active or self.manus.is_active or self.manus_client.is_active:
                raise RuntimeError("仍有项目进程没有退出")
            self._ble_ready.clear()
            self._manus_recorder_ready.clear()
            self._streams_ready.clear()
            self._ble_stop_requested.clear()
            self._stream_stop_requested.clear()
            self._ble_start_result.clear()
            self._ble_stop_result.clear()
            self._ble_start_ok = None
            self._ble_stop_ok = None
            self._ble_control_state = "CONNECTING"
            self._ble_control_result = "STANDBY"
            self._ble_time_sync_state = "SYNCING"
            self._coordinator_serial = "连接中"
            self._utc_map_state = "UNKNOWN"
            self._node_states.clear()
            self.error = None
            self.phase = "starting_ble"
            self.message = "正在开启授时并启动 Tracker/MANUS 常驻数据流…"
        self.ble_timesync_logs.append(
            "[UI] ===== Start persistent Mode2 time sync =====", "system"
        )
        self.ble.logs.append("[UI] ===== Start BLE time sync service =====", "system")
        threading.Thread(
            target=self._start_ble_worker,
            name="ble-start-sequence",
            daemon=True,
        ).start()

    def _start_ble_worker(self) -> None:
        try:
            ble_python = BLE_DIR / ".venv" / "Scripts" / "python.exe"
            self.ble.start(
                [
                    str(ble_python),
                    "-u",
                    "-m",
                    "app.main",
                    "run",
                    "--control-stdin",
                ],
                BLE_DIR,
            )
            if not self._wait_until_ready(
                self._ble_ready, self.ble, self._ble_stop_requested
            ):
                return
            self._launch_manus_streams()
            if not self.ble.is_active or not self._ble_ready.is_set():
                raise RuntimeError("Mode2 授时在数据流启动期间退出")
            self._set_phase(
                "streams_ready",
                "授时与 Tracker/MANUS 数据流已就绪；开始录制只会开启写盘",
            )
        except Exception as exc:
            if not self._ble_stop_requested.is_set():
                self._stop_manus_processes()
                self._set_phase("error", f"授时或数据流启动失败：{exc}", str(exc))

    def _launch_manus_streams(self) -> None:
        uv_path = shutil.which("uv")
        if uv_path is None:
            raise RuntimeError("uv disappeared after preflight")
        self._manus_recorder_ready.clear()
        self._streams_ready.clear()
        self._stream_stop_requested.clear()
        self._tracker_states.clear()
        self.manus.logs.append(
            "[UI] ===== Start persistent MANUS + OpenVR streams =====", "system"
        )
        self.manus.start(
            [
                uv_path,
                "run",
                "--no-project",
                "--with-requirements",
                str(MANUS_REQUIREMENTS),
                "python",
                "-u",
                str(MANUS_RECORDER),
                "--config",
                str(MANUS_CONFIG),
                "--output-root",
                str(VR_OUTPUT_ROOT),
                "--control-stdin",
            ],
            MANUS_DIR,
        )
        if not self._wait_until_ready(
            self._manus_recorder_ready, self.manus, self._stream_stop_requested
        ):
            raise RuntimeError("MANUS recorder stream startup was cancelled")
        self.manus.logs.append(
            "[UI] Recorder listening; starting the MANUS 120Hz SDK client", "system"
        )
        self.manus_client.start([str(MANUS_CLIENT)], MANUS_DIR)
        if not self.manus_client.send_line("2"):
            raise RuntimeError("无法向 MANUS SDK 客户端选择 Core Local")
        self.manus.logs.append(
            "[UI] Selected [2] Core Local for the MANUS SDK client", "system"
        )
        if not self._wait_until_ready(
            self._streams_ready, self.manus, self._stream_stop_requested
        ):
            raise RuntimeError("Tracker/MANUS stream startup was cancelled")

    def start_streams(self) -> None:
        self._require_checks(
            {"Windows 平台", "uv", "MANUS 联合采集入口", "MANUS 采集配置", "MANUS 120Hz 客户端"}
        )
        with self._lock:
            if self.phase != "ble_ready" or not self.ble.is_active or not self._ble_ready.is_set():
                raise RuntimeError("请先开启授时并等待中控就绪")
            if self.manus.is_active or self.manus_client.is_active:
                raise RuntimeError("Tracker/MANUS 数据流已经运行")
            self.phase = "starting_streams"
            self.message = "正在启动 Tracker/MANUS 常驻数据流…"
            self.error = None
        threading.Thread(
            target=self._start_streams_worker,
            name="stream-start-sequence",
            daemon=True,
        ).start()

    def _start_streams_worker(self) -> None:
        try:
            self._launch_manus_streams()
            self._set_phase("streams_ready", "Tracker/MANUS 数据流已就绪，可以开始录制")
        except Exception as exc:
            self._stop_manus_processes()
            self._set_phase("ble_ready", f"数据流启动失败：{exc}", str(exc))

    def stop_streams(self) -> None:
        with self._lock:
            if self.phase in {"starting_capture", "recording", "stopping_capture"}:
                raise RuntimeError("请先停止并保存当前录制")
            if self.phase == "stopping_streams":
                return
            if not self.manus.is_active and not self.manus_client.is_active:
                self._streams_ready.clear()
                self._tracker_states.clear()
                self.phase = "ble_ready" if self.ble.is_active else "idle"
                self.message = "Tracker/MANUS 数据流未运行"
                return
            self._stream_stop_requested.set()
            self.phase = "stopping_streams"
            self.message = "正在停止 Tracker/MANUS 数据流；授时保持运行…"
            self.error = None
        threading.Thread(
            target=self._stop_streams_worker,
            name="stream-stop-sequence",
            daemon=True,
        ).start()

    def _stop_streams_worker(self) -> None:
        self._stop_manus_processes()
        self._streams_ready.clear()
        with self._lock:
            self._tracker_states.clear()
        if self.ble.is_active and self._ble_ready.is_set():
            self._set_phase("ble_ready", "Tracker/MANUS 数据流已停止；Mode2 继续授时")
        else:
            self._set_phase("idle", "Tracker/MANUS 数据流与授时均未运行")

    def scan_wearables(self) -> None:
        with self._lock:
            if (
                self.phase not in {"ble_ready", "streams_ready"}
                or not self.ble.is_active
                or not self._ble_ready.is_set()
            ):
                raise RuntimeError("请先启动 BLE 授时并等待中控串口就绪")
        if not self.ble.send_line("s"):
            raise RuntimeError("无法向 BLE 授时进程发送扫描指令")
        self.ble_control_logs.append(
            "[UI] Requested wearable scan (keyboard s)", "system"
        )

    def stop_ble(self) -> None:
        with self._lock:
            if self.phase in {"starting_capture", "recording", "stopping_capture"}:
                raise RuntimeError("请先停止并保存当前采集，再停止 BLE 授时")
            if self.manus.is_active or self.manus_client.is_active:
                raise RuntimeError("请先点击“停止 Tracker & MANUS 数据流”")
            if self.phase == "stopping_ble":
                return
            if not self.ble.is_active:
                self._ble_ready.clear()
                self._ble_control_state = "OFFLINE"
                self._ble_control_result = "OFFLINE"
                self._ble_time_sync_state = "STOPPED"
                self.phase = "idle"
                self.message = "Mode2 授时未运行"
                return
            self._ble_stop_requested.set()
            self.phase = "stopping_ble"
            self.message = "正在停止 Mode2 常驻授时进程…"
            self.error = None
        threading.Thread(
            target=self._stop_ble_worker,
            name="ble-stop-sequence",
            daemon=True,
        ).start()

    def _stop_ble_worker(self) -> None:
        try:
            self.ble.send_line("quit")
            if not self.ble.wait(10):
                self.ble.force_stop()
        finally:
            if self.ble.is_active:
                self.ble.force_stop()
            self._ble_ready.clear()
            with self._lock:
                self._node_states.clear()
                self._ble_control_state = "OFFLINE"
                self._ble_control_result = "OFFLINE"
                self._ble_time_sync_state = "STOPPED"
                self._coordinator_serial = "未连接"
                self._utc_map_state = "UNKNOWN"
            self._set_phase("idle", "Mode2 常驻授时已停止")

    # MANUS + OpenVR episode write lifecycle -------------------------------
    def start_capture(
        self,
        task_name: str = DEFAULT_TASK_NAMES[0],
        complex_level: str = COMPLEX_LEVELS[0],
    ) -> None:
        task = self.add_task_name(task_name)
        level = _validate_capture_segment(complex_level, "complex_level", 15)
        if level not in COMPLEX_LEVELS:
            raise RuntimeError("complex_level 必须是 L0、L1 或 L_test")
        with self._lock:
            if (
                self.phase != "streams_ready"
                or not self.ble.is_active
                or not self._ble_ready.is_set()
                or not self.manus.is_active
                or not self.manus_client.is_active
                or not self._streams_ready.is_set()
            ):
                raise RuntimeError("请先开启授时并等待 Tracker/MANUS 数据流就绪")
            self._capture_stop_requested.clear()
            self._capture_ready.clear()
            self._capture_saved.clear()
            self._capture_start_error = None
            self._ble_start_result.clear()
            self._ble_start_ok = None
            self.error = None
            self.capture_started_at = None
            self.capture_output_dir = None
            self._capture_task_name = task
            self._capture_complex_level = level
            self.phase = "starting_capture"
            self.message = "正在开启 VR 写盘并发送 Mode2 START…"
        self.manus.logs.append(
            f"[UI] ===== Start episode task={task} level={level} =====", "system"
        )
        threading.Thread(
            target=self._start_capture_worker,
            name="capture-start-sequence",
            daemon=True,
        ).start()

    def _start_capture_worker(self) -> None:
        try:
            episode_command = (
                f"start {self._capture_task_name} {self._capture_complex_level}"
            )
            if not self.manus.send_line(episode_command):
                raise RuntimeError("无法通知 VR recorder 开始写盘")
            if not self._wait_until_ready(
                self._capture_ready, self.manus, self._capture_stop_requested
            ):
                return
            if self._capture_start_error:
                raise RuntimeError(self._capture_start_error)
            with self._lock:
                self._ble_control_state = "STARTING"
                self._ble_control_result = "STARTING"
            self.ble_control_logs.append(
                f"[UI -> Mode2] START task={self._capture_task_name} "
                f"level={self._capture_complex_level}（等待全部 wearable ARMED）",
                "system",
            )
            start_command = (
                f"start {self._capture_task_name} "
                f"{self._capture_complex_level}"
            )
            if not self.ble.is_active or not self.ble.send_line(start_command):
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                raise RuntimeError("无法向 Mode2 控制器发送键盘事件 1")
            if not self._ble_start_result.wait(45):
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                raise RuntimeError("等待 Mode2 START 结果超时")
            if not self._ble_start_ok:
                raise RuntimeError("Mode2 START 未成功，详情见录制控制日志")
            if self._capture_stop_requested.is_set():
                return
            self.capture_started_at = _now()
            self._set_phase(
                "recording", "录制中：Mode2、MANUS RawSkeleton 与 OpenVR Tracker 正在写盘"
            )
        except Exception as exc:
            if self._capture_stop_requested.is_set():
                return
            if self.manus.is_active:
                self._capture_saved.clear()
                self.manus.send_line("stop")
                self._capture_saved.wait(10)
            if self.ble.is_active and self._ble_ready.is_set():
                self._set_phase(
                    "streams_ready",
                    f"录制启动失败：{exc}；授时与数据流仍保持运行",
                    str(exc),
                )
            else:
                self._set_phase("error", f"采集启动失败：{exc}", str(exc))

    def stop_capture(self) -> None:
        with self._lock:
            if self.phase not in {"starting_capture", "recording"}:
                raise RuntimeError("当前没有正在启动或运行的采集")
            if self.phase == "starting_capture":
                self._ble_start_ok = False
                self._ble_start_result.set()
            self._capture_stop_requested.set()
            self.phase = "stopping_capture"
            self.message = "正在发送 Mode2 STOP 并关闭本轮 VR 文件；数据流保持运行…"
            self.error = None
        self._wait_for_frame_report()
        threading.Thread(
            target=self._stop_capture_worker,
            name="capture-stop-sequence",
            daemon=True,
        ).start()

    def _stop_capture_worker(self) -> None:
        self._ble_stop_result.clear()
        self._ble_stop_ok = None
        stop_sent = False
        self._capture_saved.clear()
        if self.manus.is_active:
            self.manus.send_line("stop")
        if self.ble.is_active and self._ble_ready.is_set():
            with self._lock:
                self._ble_control_state = "STOPPING"
                self._ble_control_result = "STOPPING"
            self.ble_control_logs.append(
                "[UI -> Mode2] 0 / STOP（等待中控确认）", "system"
            )
            stop_sent = self.ble.send_line("0")
        vr_saved = self._capture_saved.wait(35) if self.manus.is_active else False
        stop_confirmed = True
        if stop_sent and self.ble.is_active and self._ble_ready.is_set():
            stop_confirmed = self._ble_stop_result.wait(55) and bool(self._ble_stop_ok)
        self.last_stopped_at = _now()
        if self.ble.is_active and self._ble_ready.is_set():
            if stop_confirmed:
                self._set_phase(
                    "streams_ready",
                    "本轮录制已停止并保存；授时与 Tracker/MANUS 数据流继续运行",
                    None if vr_saved else "VR recorder did not confirm [SAVED]",
                )
            else:
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                self._set_phase(
                    "streams_ready",
                    "VR 写盘已停止，但未确认 Mode2 STOP 成功；数据流继续运行",
                    "Mode2 STOP was not confirmed",
                )
        else:
            self._set_phase(
                "error", "录制已停止，但 Mode2 授时进程已退出", "BLE exited"
            )

    def _stop_manus_processes(self) -> None:
        self._stream_stop_requested.set()
        if self.manus.is_active:
            self.manus.send_line("shutdown")
            if not self.manus.wait(35):
                self.manus.force_stop()
        if self.manus_client.is_active and not self.manus_client.wait(10):
            self.manus_client.force_stop()

    def _wait_until_ready(
        self,
        ready_event: threading.Event,
        process: ProjectProcess,
        cancel_event: threading.Event,
    ) -> bool:
        while not ready_event.wait(0.2):
            if cancel_event.is_set():
                return False
            if not process.is_active:
                raise RuntimeError(f"{process.title} 在就绪前退出")
        return not cancel_event.is_set()

    # Process callbacks and state ------------------------------------------
    def _on_line(self, key: str, line: str) -> None:
        if key == "ble":
            self._record_ble_line(line)
            self._update_mode2_state_from_line(line)
        elif key == "manus":
            tracker_match = TRACKER_STATUS_PATTERN.search(line)
            if tracker_match:
                serial = tracker_match.group("serial")
                with self._lock:
                    self._tracker_states[serial] = {
                        "serial": serial,
                        "role": tracker_match.group("role"),
                        "connected": tracker_match.group("connected") == "1",
                        "tracking": tracker_match.group("tracking") == "1",
                        "updated_at": _now(),
                    }
            if line.startswith("[READY]"):
                self._manus_recorder_ready.set()
            if line.startswith("[STREAMING]"):
                self._streams_ready.set()
            recording_match = re.match(
                r"\[RECORDING\]\s+(.+?)\s+\(.+\)$", line
            )
            if recording_match:
                self.capture_output_dir = recording_match.group(1).strip()
                self._capture_ready.set()
            if line.startswith("[SAVED]"):
                saved_path = line[len("[SAVED]") :].strip()
                if saved_path:
                    self.capture_output_dir = saved_path
                self._capture_saved.set()
            if line.startswith("[CONTROL_ERROR]"):
                self._capture_start_error = line[len("[CONTROL_ERROR]") :].strip()
                self._capture_ready.set()
                self._capture_saved.set()

    def _record_ble_line(self, line: str) -> None:
        upper = line.upper()
        if MODE2_NODE_PATTERN.search(line) or any(
            token in upper for token in BLE_CONTROL_TOKENS
        ):
            self.ble_control_logs.append(line)
        else:
            self.ble_timesync_logs.append(line)

    def _update_mode2_state_from_line(self, line: str) -> None:
        upper = line.upper()
        now = _now()

        ready_match = MODE2_READY_PATTERN.search(line)
        if ready_match:
            with self._lock:
                self._coordinator_name = ready_match.group("name").strip()
                self._coordinator_serial = ready_match.group("serial").strip()
                self._ble_control_state = "IDLE"
                self._ble_control_result = "STANDBY"
                self._ble_time_sync_state = "SYNCING"
            self._ble_ready.set()

        node_match = MODE2_NODE_PATTERN.search(line)
        if node_match:
            node_id = node_match.group("node_id")
            with self._lock:
                self._node_states[node_id] = {
                    "connected": node_match.group("connected") == "1",
                    "state": node_match.group("state").upper(),
                    "details": node_match.group("details").strip(),
                    "updated_at": now,
                }

        utc_match = re.search(r"\butc_map=(LOCKED|UNLOCKED)\b", line, re.IGNORECASE)
        if utc_match:
            with self._lock:
                self._utc_map_state = utc_match.group(1).upper()

        if "TIME SYNC ACCEPTED:" in upper:
            with self._lock:
                self._ble_time_sync_state = "SYNCED"
        elif (
            "INITIAL TIME SYNC WAS NOT ACCEPTED" in upper
            or "PERIODIC COORDINATOR TIME SYNC FAILED" in upper
            or "TIME_ERROR" in upper
        ):
            with self._lock:
                self._ble_time_sync_state = "RETRYING"

        if "[START]" in upper and "WEARABLE" in upper and "ARMED" in upper:
            with self._lock:
                self._ble_control_state = "RUNNING"
                self._ble_control_result = "START_OK"
                self._ble_start_ok = True
            self._start_frame_report()
            self._ble_start_result.set()
        elif any(
            marker in upper
            for marker in (
                "[START FAILED]",
                "START REJECTED BECAUSE",
                "START DID NOT RECEIVE",
                "START IGNORED",
            )
        ):
            with self._lock:
                self._ble_control_state = "ERROR"
                self._ble_control_result = "ERROR"
                self._ble_start_ok = False
            self._ble_start_result.set()

        if "[STOP]" in upper:
            with self._lock:
                self._ble_control_state = "IDLE"
                self._ble_control_result = "STOP_OK"
                self._ble_stop_ok = True
            self._ble_stop_result.set()
        elif "STOP DID NOT RECEIVE" in upper:
            with self._lock:
                self._ble_control_state = "ERROR"
                self._ble_control_result = "ERROR"
                self._ble_stop_ok = False
            self._ble_stop_result.set()

        frame_match = MODE2_STOP_FRAME_PATTERN.search(line)
        if frame_match:
            session = frame_match.group("session")
            node_id = frame_match.group("node_id")
            frames = int(frame_match.group("frames"))
            with self._lock:
                if self._frame_report_session not in {None, session}:
                    self._frame_report_nodes.clear()
                self._frame_report_session = session
                self._frame_report_nodes[node_id] = {
                    "frames": frames,
                    "updated_at": now,
                }
                received_nodes = set(self._frame_report_nodes)
                expected_nodes = self._frame_report_expected_nodes
                self._frame_report_status = (
                    "complete"
                    if not expected_nodes or expected_nodes <= received_nodes
                    else "receiving"
                )

    def _on_exit(self, key: str, exit_code: int) -> None:
        if self._shutting_down:
            return
        with self._lock:
            phase = self.phase
        if key == "ble":
            self._ble_ready.clear()
            with self._lock:
                self._node_states.clear()
                self._ble_control_state = "OFFLINE"
                self._ble_control_result = "OFFLINE"
                self._ble_time_sync_state = "STOPPED"
                self._ble_start_ok = False
                self._ble_stop_ok = False
            self._ble_start_result.set()
            self._ble_stop_result.set()
            if phase == "stopping_ble":
                return
            if phase in {"starting_capture", "recording", "stopping_capture"}:
                self._capture_stop_requested.set()
                self._set_phase(
                    "error",
                    f"Mode2 授时意外退出（代码 {exit_code}），正在关闭本轮 VR 文件",
                    f"BLE exited with code {exit_code}",
                )
                threading.Thread(
                    target=self._finish_episode_after_ble_failure,
                    name="episode-stop-after-ble-failure",
                    daemon=True,
                ).start()
            elif phase != "idle":
                self._set_phase(
                    "error",
                    f"Mode2 授时进程意外退出（代码 {exit_code}）",
                    f"BLE exited with code {exit_code}",
                )
            return

        if key not in {"manus", "manus_client"}:
            return
        self._streams_ready.clear()
        if self._stream_stop_requested.is_set() or phase == "stopping_streams":
            return
        if phase in {"starting_ble", "starting_streams", "streams_ready"}:
            self._capture_stop_requested.set()
            self._stop_manus_processes()
            if self.ble.is_active and self._ble_ready.is_set():
                self._set_phase(
                    "ble_ready",
                    f"Tracker/MANUS 数据流意外退出（代码 {exit_code}）；Mode2 继续授时",
                    f"{key} exited with code {exit_code}",
                )
            else:
                self._set_phase("error", "Tracker/MANUS 数据流与 Mode2 均未正常运行")
        elif phase in {"starting_capture", "recording", "stopping_capture"}:
            if self._capture_stop_requested.is_set():
                return
            self._capture_stop_requested.set()
            self._set_phase(
                "stopping_capture",
                f"Tracker/MANUS 数据流意外退出（代码 {exit_code}），正在向 Mode2 发送 STOP",
                f"{key} exited with code {exit_code}",
            )
            threading.Thread(
                target=self._finish_after_unexpected_capture_exit,
                name="ble-stop-after-manus-failure",
                daemon=True,
            ).start()

    def _finish_episode_after_ble_failure(self) -> None:
        self._capture_saved.clear()
        if self.manus.is_active:
            self.manus.send_line("stop")
            self._capture_saved.wait(20)

    def _finish_after_unexpected_capture_exit(self) -> None:
        self._stop_manus_processes()
        if self.ble.is_active and self._ble_ready.is_set():
            self._wait_for_frame_report()
            self._ble_stop_result.clear()
            self._ble_stop_ok = None
            with self._lock:
                self._ble_control_state = "STOPPING"
                self._ble_control_result = "STOPPING"
            self.ble_control_logs.append(
                "[UI -> Mode2] MANUS 联合采集异常退出，发送 0 / STOP", "system"
            )
            self.ble.send_line("0")
            confirmed = self._ble_stop_result.wait(20) and bool(self._ble_stop_ok)
            if confirmed:
                self._set_phase(
                    "ble_ready",
                    "Tracker/MANUS 数据流异常退出；Mode2 STOP 已确认，常驻授时继续",
                    "Tracker/MANUS stream exited unexpectedly",
                )
            else:
                with self._lock:
                    self._ble_control_state = "ERROR"
                    self._ble_control_result = "ERROR"
                self._set_phase(
                    "ble_ready",
                    "Tracker/MANUS 数据流异常退出；未确认 Mode2 STOP，请检查录制控制日志",
                    "Tracker/MANUS stream exited and Mode2 STOP was not confirmed",
                )
        else:
            self._set_phase("error", "MANUS 联合采集与 Mode2 均未正常运行")

    def clear_logs(self) -> None:
        self.ble.logs.clear()
        self.ble_timesync_logs.clear()
        self.ble_control_logs.clear()
        self.manus.logs.clear()

    def state(
        self,
        after_ble_timesync: int = 0,
        after_ble_control: int = 0,
        after_manus: int = 0,
    ) -> dict:
        timesync_lines, timesync_sequence = self.ble_timesync_logs.since(
            after_ble_timesync
        )
        control_lines, control_sequence = self.ble_control_logs.since(
            after_ble_control
        )
        manus_lines, manus_sequence = self.manus.logs.since(after_manus)
        with self._lock:
            ble_active = self.ble.is_active
            streams_active = self.manus.is_active or self.manus_client.is_active
            controls = {
                "can_start_ble": self.phase in {"idle", "error"} and not ble_active and not streams_active,
                "can_stop_ble": ble_active and not streams_active and self.phase in {"ble_ready", "error"},
                "can_start_streams": self.phase == "ble_ready" and ble_active and not streams_active,
                "can_stop_streams": streams_active and self.phase in {"streams_ready", "error"},
                "can_start_capture": (
                    self.phase == "streams_ready"
                    and ble_active
                    and self._ble_ready.is_set()
                    and streams_active
                    and self._streams_ready.is_set()
                ),
                "can_scan_wearables": self.phase in {"ble_ready", "streams_ready"} and ble_active and self._ble_ready.is_set(),
                "can_stop_capture": self.phase in {"starting_capture", "recording"},
            }
            return {
                "phase": self.phase,
                "message": self.message,
                "error": self.error,
                "ble_ready": self._ble_ready.is_set() and ble_active,
                "streams_ready": self._streams_ready.is_set() and streams_active,
                "capture_started_at": self.capture_started_at,
                "last_stopped_at": self.last_stopped_at,
                "capture_output_dir": self.capture_output_dir,
                "capture_options": {
                    "task_names": list(self._task_names),
                    "complex_levels": list(COMPLEX_LEVELS),
                    "selected_task_name": self._capture_task_name,
                    "selected_complex_level": self._capture_complex_level,
                },
                "trackers": [
                    dict(item)
                    for _, item in sorted(
                        self._tracker_states.items(),
                        key=lambda pair: (str(pair[1]["role"]), pair[0]),
                    )
                ],
                "mode2": {
                    "name": self._coordinator_name,
                    "serial": self._coordinator_serial,
                    "time_sync_state": self._ble_time_sync_state,
                    "control_state": self._ble_control_state,
                    "control_result": self._ble_control_result,
                    "utc_map_state": self._utc_map_state,
                    "nodes": [
                        {
                            "node_id": node_id,
                            "connected": item["connected"],
                            "state": item["state"],
                            "details": item["details"],
                            "updated_at": item["updated_at"],
                        }
                        for node_id, item in sorted(
                            self._node_states.items(), key=lambda pair: int(pair[0])
                        )
                    ],
                    "frame_report": {
                        "status": self._frame_report_status,
                        "session_id": self._frame_report_session,
                        "expected_nodes": sorted(
                            self._frame_report_expected_nodes, key=int
                        ),
                        "nodes": [
                            {
                                "node_id": node_id,
                                "frames": item["frames"],
                                "updated_at": item["updated_at"],
                            }
                            for node_id, item in sorted(
                                self._frame_report_nodes.items(),
                                key=lambda pair: int(pair[0]),
                            )
                        ],
                    },
                },
                "controls": controls,
                "processes": {
                    "ble": self.ble.snapshot(),
                    "manus": self.manus.snapshot(),
                    "manus_client": self.manus_client.snapshot(),
                },
                "logs": {
                    "ble_timesync": {
                        "items": timesync_lines,
                        "last_seq": timesync_sequence,
                    },
                    "ble_control": {
                        "items": control_lines,
                        "last_seq": control_sequence,
                    },
                    "manus": {"items": manus_lines, "last_seq": manus_sequence},
                },
            }

    def shutdown(self) -> None:
        self._shutting_down = True
        self._ble_stop_requested.set()
        self._stream_stop_requested.set()
        self._capture_stop_requested.set()
        self._stop_manus_processes()
        if self.ble.is_active:
            if self._ble_ready.is_set() and self.phase in {"recording", "stopping_capture"}:
                self.ble.send_line("0")
                time.sleep(0.5)
            self.ble.send_line("quit")
            if not self.ble.wait(10):
                self.ble.force_stop()

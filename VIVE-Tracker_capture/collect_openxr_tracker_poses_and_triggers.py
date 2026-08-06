# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#   "pyopenxr==1.1.5301",
# ]
# ///

"""Immediately record VIVE Tracker poses through OpenXR until Ctrl+C."""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import xr

from openxr_tracker_capture import CaptureConfig, OpenXRTrackerCapture
from openxr_tracker_capture.schema import POSE_FIELDS


class PoseOnlyOpenXRTrackerCapture(OpenXRTrackerCapture):
    """Sample Tracker poses without polling or recording Trigger actions."""

    def _capture_sample(self, writer, frame_index: int, timestamp_xr_ns: int) -> None:
        self._capture_pose_sample(frame_index, timestamp_xr_ns)

    def _capture_pose_sample(self, frame_index: int, timestamp_xr_ns: int) -> None:
        """Capture one pose-only frame without polling Trigger actions."""

        xr.sync_actions(
            self.session,
            xr.ActionsSyncInfo(
                active_action_sets=[xr.ActiveActionSet(self.action_set, xr.NULL_PATH)]
            ),
        )
        self._refresh_interaction_profiles()

        frame_rows: list[dict] = []
        for tracker in self.trackers:
            pose_state = xr.get_action_state_pose(
                self.session,
                xr.ActionStateGetInfo(
                    action=self.pose_action,
                    subaction_path=tracker.role_xr_path,
                ),
            )
            pose = self._locate(tracker.action_space, timestamp_xr_ns)
            host_receive_ns = time.perf_counter_ns()
            frame_rows.append(
                {
                    "timestamp_xr_ns": timestamp_xr_ns,
                    "host_receive_monotonic_ns": host_receive_ns,
                    "host_receive_system_time_ns": time.time_ns(),
                    "latency_ns": self.xr_now() - timestamp_xr_ns,
                    "frame_index": frame_index,
                    "device_id": tracker.device_id,
                    "serial_number": tracker.serial_number,
                    "tracker_role": tracker.tracker_role,
                    "interaction_profile": tracker.interaction_profile,
                    "pose_action_active": int(bool(pose_state.is_active)),
                    "tracking_valid": int(
                        bool(pose_state.is_active) and pose.tracking_valid
                    ),
                    "location_flags": pose.location_flags,
                    "velocity_flags": pose.velocity_flags,
                    "position_x": pose.position_x,
                    "position_y": pose.position_y,
                    "position_z": pose.position_z,
                    "rotation_quaternion_x": pose.rotation_x,
                    "rotation_quaternion_y": pose.rotation_y,
                    "rotation_quaternion_z": pose.rotation_z,
                    "rotation_quaternion_w": pose.rotation_w,
                    "linear_velocity_x": pose.linear_x,
                    "linear_velocity_y": pose.linear_y,
                    "linear_velocity_z": pose.linear_z,
                    "angular_velocity_x": pose.angular_x,
                    "angular_velocity_y": pose.angular_y,
                    "angular_velocity_z": pose.angular_z,
                }
            )
            self.pose_row_count += 1

        if self.config.pose_frame_callback is not None:
            self.config.pose_frame_callback(frame_rows)


class PoseOutput:
    def __init__(self, output_root: Path):
        self.output_root = output_root
        self.output_dir: Path | None = None
        self.pose_handle = None
        self.pose_writer = None
        self.frame_count = 0
        self.pose_row_count = 0
        self.started_at_utc: str | None = None
        self.started_host_monotonic_ns: int | None = None

    def start(self) -> None:
        if self.output_dir is not None:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = (self.output_root / f"vr_capture_{stamp}").resolve()
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.pose_handle = (self.output_dir / "tracker_poses.csv").open(
            "w", newline="", encoding="utf-8"
        )
        self.pose_writer = csv.DictWriter(self.pose_handle, fieldnames=POSE_FIELDS)
        self.pose_writer.writeheader()
        self.pose_handle.flush()
        self.started_at_utc = datetime.now(timezone.utc).isoformat()
        self.started_host_monotonic_ns = time.perf_counter_ns()
        print(f"Recording poses to: {self.output_dir}")

    def write_pose_frame(self, rows: list[dict]) -> None:
        if self.pose_writer is None:
            return
        for row in rows:
            output_row = dict(row)
            output_row["frame_index"] = self.frame_count
            self.pose_writer.writerow(output_row)
            self.pose_row_count += 1
        self.frame_count += 1
        if self.frame_count % 120 == 0:
            self.pose_handle.flush()

    def close_poses(self) -> None:
        if self.pose_handle is not None:
            self.pose_handle.flush()
            self.pose_handle.close()
            self.pose_handle = None
            self.pose_writer = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role-map",
        type=Path,
        default=Path("tracker_roles.json"),
        help="Tracker role-to-serial mapping JSON",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("vr_captures"),
        help="Parent directory for timestamped VR captures",
    )
    parser.add_argument(
        "--tracker-rate",
        type=float,
        default=120.0,
        help="Tracker pose sampling rate in Hz",
    )
    parser.add_argument(
        "--console-interval",
        type=float,
        default=5.0,
        help="Seconds between console status messages; use 0 to disable",
    )
    parser.add_argument(
        "--control-stdin",
        action="store_true",
        help="stop and save when a stop/quit line is received on stdin",
    )
    args = parser.parse_args()

    if args.tracker_rate <= 0:
        parser.error("--tracker-rate must be positive")
    if args.console_interval < 0:
        parser.error("--console-interval must not be negative")
    if not args.role_map.is_file():
        parser.error(f"role map does not exist: {args.role_map}")
    return args


def main() -> None:
    args = parse_args()
    stop_event = threading.Event()
    output = PoseOutput(args.output_root)
    stopped_by_ctrl_c = False
    stop_reason = "external_stop"

    def on_pose_frame(rows: list[dict]) -> None:
        output.write_pose_frame(rows)

    def request_stop(signum, frame) -> None:
        nonlocal stopped_by_ctrl_c, stop_reason
        if not stopped_by_ctrl_c:
            stopped_by_ctrl_c = True
            stop_reason = "ctrl_c"
            print("\nCtrl+C received; finishing the current sample and saving data...")
        stop_event.set()

    def watch_stdin() -> None:
        nonlocal stop_reason
        for line in sys.stdin:
            if line.strip().casefold() in {"0", "stop", "q", "quit", "exit"}:
                stop_reason = "ui_stdin"
                print(
                    "\n[CONTROL] Stop requested by UI; saving capture...",
                    flush=True,
                )
                stop_event.set()
                return

    config = CaptureConfig(
        output_dir=args.output_root,
        duration_s=None,
        rate_hz=args.tracker_rate,
        console_pose_interval_s=args.console_interval,
        role_map_path=args.role_map.resolve(),
        write_output_files=False,
        pose_frame_callback=on_pose_frame,
        external_stop_event=stop_event,
    )
    capture = PoseOnlyOpenXRTrackerCapture(config)
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    print(f"OpenXR Tracker capture will start immediately at {args.tracker_rate:g} Hz.")
    print("Press Ctrl+C to save and exit.")
    if args.control_stdin:
        threading.Thread(
            target=watch_stdin,
            name="vive-ui-stdin",
            daemon=True,
        ).start()
        print("[CONTROL] stdin mode: send stop to save and exit.", flush=True)

    try:
        signal.signal(signal.SIGINT, request_stop)
        capture.initialize()
        output.start()
        runtime_metadata = capture.run()

        output.close_poses()
        finished_at_utc = datetime.now(timezone.utc).isoformat()
        finished_host_ns = time.perf_counter_ns()
        metadata = dict(runtime_metadata)
        metadata["frame_count"] = output.frame_count
        metadata["pose_row_count"] = output.pose_row_count
        metadata["capture_control"] = {
            "start_reason": "openxr_initialized",
            "started_at_utc": output.started_at_utc,
            "finished_at_utc": finished_at_utc,
            "recording_duration_s": (
                (finished_host_ns - output.started_host_monotonic_ns) / 1e9
            ),
            "stop_reason": stop_reason,
        }
        metadata["files"] = {"tracker_poses": "tracker_poses.csv"}
        metadata.pop("esp32_sync_event_count", None)
        metadata.pop("esp32_invalid_message_count", None)
        metadata.pop("trigger_interface_diagnostics", None)
        (output.output_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"Finished: frames={output.frame_count}, "
            f"pose_rows={output.pose_row_count}"
        )
        print(f"Saved VR capture: {output.output_dir}")
    finally:
        output.close_poses()
        capture.close()
        signal.signal(signal.SIGINT, previous_sigint_handler)


if __name__ == "__main__":
    main()

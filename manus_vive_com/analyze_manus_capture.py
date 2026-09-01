"""Read-only integrity and timing report for a MANUS capture session."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def quaternion_angle_degrees(first: list[float], second: list[float]) -> float:
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    dot = abs(sum(a * b for a, b in zip(first, second)) / (first_norm * second_norm))
    return math.degrees(2.0 * math.acos(min(1.0, max(-1.0, dot))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--motion-bins", type=float, default=0.0, metavar="SECONDS")
    args = parser.parse_args()
    path = args.session_dir / "manus_raw_skeleton.jsonl"
    with path.open(encoding="utf-8") as source:
        frames = [json.loads(line) for line in source if line.strip()]
    if len(frames) < 2:
        raise SystemExit("Need at least two frames")

    callback_steady = [int(frame["callback_steady_time_ns"]) for frame in frames]
    callback_sequence = [int(frame.get("callback_sequence", frame["sequence"])) for frame in frames]
    callbacks_coalesced = [int(frame.get("callbacks_coalesced_into_frame", 0)) for frame in frames]
    callback_system = [int(frame["callback_system_time_unix_ns"]) for frame in frames]
    receiver_steady = [int(frame["recorder_receive"]["receiver_steady_time_ns"]) for frame in frames]
    receiver_system = [int(frame["recorder_receive"]["receiver_system_time_unix_ns"]) for frame in frames]
    publish_time = [int(frame["manus_publish_time_raw"]) for frame in frames]
    callback_delta_ms = [(b - a) / 1e6 for a, b in zip(callback_steady, callback_steady[1:])]
    publish_delta = [b - a for a, b in zip(publish_time, publish_time[1:])]
    steady_latency_ms = [(b - a) / 1e6 for a, b in zip(callback_steady, receiver_steady)]
    system_latency_ms = [(b - a) / 1e6 for a, b in zip(callback_system, receiver_system)]
    duration_seconds = (callback_steady[-1] - callback_steady[0]) / 1e9

    output_rate = (len(frames) - 1) / duration_seconds
    source_callback_rate = (callback_sequence[-1] - callback_sequence[0]) / duration_seconds
    target_rates = sorted({frame.get("output_rate_hz") for frame in frames if frame.get("output_rate_hz") is not None})
    print(f"frames: {len(frames)} (output sequence {frames[0]['sequence']}..{frames[-1]['sequence']})")
    print(
        f"duration: {duration_seconds:.6f} s; output rate: {output_rate:.3f} Hz; "
        f"source callback estimate: {source_callback_rate:.3f} Hz; target: {target_rates or 'legacy/unlimited'}")
    print(
        f"source callback sequence: {callback_sequence[0]}..{callback_sequence[-1]}; "
        f"callbacks coalesced into output: {sum(callbacks_coalesced)}")
    print("callback interval ms (min/median/p95/max): " + "/".join(
        f"{value:.4f}" for value in (
            min(callback_delta_ms), statistics.median(callback_delta_ms),
            percentile(callback_delta_ms, 0.95), max(callback_delta_ms))))
    print("publish-time delta (min/median/p95/max): " + "/".join(
        str(value) for value in (
            min(publish_delta), statistics.median(publish_delta),
            percentile(publish_delta, 0.95), max(publish_delta))))
    print("steady transport ms (min/median/p95/max): " + "/".join(
        f"{value:.4f}" for value in (
            min(steady_latency_ms), statistics.median(steady_latency_ms),
            percentile(steady_latency_ms, 0.95), max(steady_latency_ms))))
    print(
        f"system transport: {sum(value < 0 for value in system_latency_ms)} negative; "
        f"min/median/max={min(system_latency_ms):.4f}/"
        f"{statistics.median(system_latency_ms):.4f}/{max(system_latency_ms):.4f} ms")
    print(
        "non-increasing timestamps (steady/system): "
        f"{sum(b <= a for a, b in zip(callback_steady, callback_steady[1:]))}/"
        f"{sum(b <= a for a, b in zip(callback_system, callback_system[1:]))}")

    skeleton_counts = sorted({len(frame["skeletons"]) for frame in frames})
    node_counts = sorted({len(skeleton["nodes"]) for frame in frames for skeleton in frame["skeletons"]})
    glove_orders = [tuple(skeleton["glove_id"] for skeleton in frame["skeletons"]) for frame in frames]
    print(f"skeletons per frame: {skeleton_counts}; nodes per skeleton: {node_counts}")
    print(f"glove order: {glove_orders[0]}; order changes: {sum(order != glove_orders[0] for order in glove_orders)}")

    quaternion_norms = [
        math.sqrt(sum(value * value for value in node["quaternion_xyzw"]))
        for frame in frames for skeleton in frame["skeletons"] for node in skeleton["nodes"]
    ]
    print(
        f"quaternion norm min/max/max error: {min(quaternion_norms):.9f}/"
        f"{max(quaternion_norms):.9f}/{max(abs(value - 1.0) for value in quaternion_norms):.3g}")

    for skeleton_index, glove_id in enumerate(glove_orders[0]):
        glove_hashes = [
            hashlib.sha256(json.dumps(frame["skeletons"][skeleton_index], sort_keys=True, separators=(",", ":")).encode()).digest()
            for frame in frames
        ]
        changed = [a != b for a, b in zip(glove_hashes, glove_hashes[1:])]
        longest_repeat_run = 0
        current_repeat_run = 0
        repeat_runs: list[tuple[int, int]] = []
        repeat_start: int | None = None
        for transition_index, did_change in enumerate(changed):
            current_repeat_run = 0 if did_change else current_repeat_run + 1
            longest_repeat_run = max(longest_repeat_run, current_repeat_run)
            if not did_change and repeat_start is None:
                repeat_start = transition_index
            elif did_change and repeat_start is not None:
                repeat_runs.append((repeat_start, transition_index))
                repeat_start = None
        if repeat_start is not None:
            repeat_runs.append((repeat_start, len(frames) - 1))
        print(
            f"glove {glove_id:08X}: changed transitions={sum(changed)}/{len(changed)}; "
            f"effective change rate={sum(changed) / duration_seconds:.3f} Hz; "
            f"consecutive repeats={sum(not value for value in changed)}; "
            f"longest repeat run={longest_repeat_run}")
        for start, end in sorted(repeat_runs, key=lambda run: run[1] - run[0], reverse=True)[:3]:
            elapsed = (callback_steady[end] - callback_steady[start]) / 1e9
            print(
                f"  repeat seq {frames[start]['sequence']}..{frames[end]['sequence']}: "
                f"{end - start} transitions, {elapsed:.3f} s")
        for node_id in (0, 4, 9, 14, 19, 24):
            positions = [frame["skeletons"][skeleton_index]["nodes"][node_id]["position"] for frame in frames]
            spans = [max(position[axis] for position in positions) - min(position[axis] for position in positions) for axis in range(3)]
            unique = len({tuple(position) for position in positions})
            print(
                f"glove {glove_id:08X} node {node_id:02d}: position span m "
                f"{spans[0]:.5f}/{spans[1]:.5f}/{spans[2]:.5f}; unique={unique}")

    pose_hashes = [
        hashlib.sha256(json.dumps(frame["skeletons"], sort_keys=True, separators=(",", ":")).encode()).digest()
        for frame in frames
    ]
    print(f"exact consecutive duplicate poses: {sum(a == b for a, b in zip(pose_hashes, pose_hashes[1:]))}")
    print(f"unique pose frames: {len(set(pose_hashes))}/{len(pose_hashes)}")

    if args.motion_bins > 0:
        bin_count = math.ceil(duration_seconds / args.motion_bins)
        movement = [[{"root_degrees": 0.0, "position_mm": [], "transitions": 0} for _ in glove_orders[0]] for _ in range(bin_count)]
        for frame_index in range(1, len(frames)):
            relative_seconds = (callback_steady[frame_index] - callback_steady[0]) / 1e9
            bin_index = min(bin_count - 1, int(relative_seconds / args.motion_bins))
            for skeleton_index in range(len(glove_orders[0])):
                previous = frames[frame_index - 1]["skeletons"][skeleton_index]["nodes"]
                current = frames[frame_index]["skeletons"][skeleton_index]["nodes"]
                root_degrees = quaternion_angle_degrees(
                    previous[0]["quaternion_xyzw"], current[0]["quaternion_xyzw"])
                squared_position_delta = [
                    sum((a - b) ** 2 for a, b in zip(old["position"], new["position"]))
                    for old, new in zip(previous, current)
                ]
                position_rms_mm = math.sqrt(sum(squared_position_delta) / len(squared_position_delta)) * 1000.0
                record = movement[bin_index][skeleton_index]
                record["root_degrees"] += root_degrees
                record["position_mm"].append(position_rms_mm)
                record["transitions"] += 1
        print(f"motion timeline ({args.motion_bins:g} s bins; root rotation sum deg / median skeleton delta mm):")
        for bin_index, records in enumerate(movement):
            start = bin_index * args.motion_bins
            end = min(duration_seconds, start + args.motion_bins)
            values = []
            for record in records:
                median_position = statistics.median(record["position_mm"]) if record["position_mm"] else 0.0
                values.append(f"{record['root_degrees']:.2f}/{median_position:.3f}")
            print(f"  {start:5.1f}-{end:5.1f}s: " + " | ".join(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

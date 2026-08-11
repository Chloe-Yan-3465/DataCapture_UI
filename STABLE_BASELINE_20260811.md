# Stable Mode2 baseline — 2026-08-11

Git tag: `stable-2026-08-11-mode2`

This baseline is the Windows control side of the hardware combination validated
on 2026-08-11. The matching NanoPi/ESP32 repository uses the same tag name.

## Fixed behavior

- Windows parses `STOP_FRAME session=<id> node=<id> frames=<count>`.
- The UI has a dedicated “NanoPi 落盘确认” area and groups frame counts by
  episode and node.
- Missing, zero, and unavailable frame reports are visually distinct.
- The Mode2 coordinator remains alive between episodes.
- `BLE-TimeSync/scripts/` contains the staged BLE diagnostic and the 30-minute
  RGBD regression test used for this baseline.

## Validation

- Python unit tests: 8 passed.
- Frontend runtime smoke test: passed.
- Hardware stress test: 1800 seconds, 8/8 episodes passed with head and right.
- No post-connect BLE disconnects and no non-zero wearable error flags.
- Both cameras reported zero queue/oversize drops and no sample at or below 1 Hz.

The detailed local test summary is
`BLE-TimeSync/logs/rgbd_stress_20260811_181534.json` (runtime logs are ignored by
Git).

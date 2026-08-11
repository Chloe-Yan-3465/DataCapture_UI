# Mode2 hardware diagnostics

These scripts preserve the hardware checks used to validate the 2026-08-11
Mode2 baseline. They do not contain credentials and they do not modify firmware.

## Stage diagnostic

`diagnose_ble_stages.py` separates BLE idle keepalive, UTC distribution, and
START/STOP so a failure can be assigned to a specific phase. Opening the serial
port resets the coordinator; the script then scans and reconnects the wearable
nodes. It always attempts `ABORT` during failure cleanup.

```powershell
python -B .\diagnose_ble_stages.py --port COM14 \
  --idle-seconds 300 --time-seconds 180 --run-seconds 180
```

## Thirty-minute RGBD stress test

`stress_rgbd_30min.py` runs eight episodes with deliberately varied recording
and idle durations. It requires head (`node 1`) and right (`node 3`) and checks:

- camera STATS remain present and `capture_fps` never reaches 1 Hz;
- three consecutive samples below 20 Hz fail the run;
- `queue_drop` and `oversize_drop` remain zero;
- both wearable links remain connected with fresh time synchronization;
- STOP returns `STOP_FRAME` for both nodes.

With SSH key authentication, the script can follow NanoPi logs directly:

```powershell
python -B .\stress_rgbd_30min.py --port COM14 \
  --host pi@192.168.8.68 \
  --camera-log /home/pi/logs/camera_YYYYMMDD_HHMMSS.log \
  --uart-log /home/pi/logs/uart_YYYYMMDD_HHMMSS.log
```

If SSH output is mirrored by another process, pass its local append-only file
using `--remote-tail-file`. Results are written under `BLE-TimeSync/logs/` as a
combined log, a raw serial log, and a JSON summary.

Do not run either script during a real collection. Both scripts own the
coordinator serial port and send Mode2 control commands.

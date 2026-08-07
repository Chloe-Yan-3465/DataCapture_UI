# Windows BLE 授时程序

本工程在 v3 架构中只负责 **Windows 自身蓝牙 → Slave 68/69/70 的 UTC 授时**。
采集 `START / TICK / STOP` 已从本工程移出，由 `Master-Serial-Control` 与 Master ESP32 负责。

## 1. 当前链路

```text
Windows BLE
   ├── Time Sync / Time Status ↔ Slave 68
   ├── Time Sync / Time Status ↔ Slave 69
   └── Time Sync / Time Status ↔ Slave 70

Windows USB Serial
   └── Master ESP32 ── BLE Pulse Control ──→ Slave 68/69/70
```

Windows 不通过本工程向 Slave 发送 START / STOP，也不写 Pulse Control。
Master 不接收或转发 Windows 的 UTC 授时数据。

## 2. BLE 固定配置

三台 Slave 的 BLE 名称必须严格为：

```text
68
69
70
```

GATT：

- Service：`12345678-1234-1234-1234-123456789abc`
- Time Sync：`12345678-1234-1234-1234-123456789abd`
- Time Status：`12345678-1234-1234-1234-123456789abe`

`Pulse Control (...9ac1)` 属于 Master → Slave 控制链，本 Windows 工程不读取也不写入。

每台 Slave 必须支持两条 BLE Central 连接：Master 一条、Windows 一条。Master 已连接后 Slave 仍需继续广播，直到 Windows 也完成连接。

## 3. run 模式

```powershell
.\.venv\Scripts\python.exe -m app.main run
```

运行流程：

1. 扫描 BLE 名称 `68 / 69 / 70`。
2. 为每台发现的 Slave 建立独立 `BleakClient`。
3. 发现 Service、Time Sync 和 Time Status。
4. 订阅 Time Status Notify。
5. 对每台在线设备执行首次测量和授时。
6. 后续每 30 秒授时一次；不同 Slave 的写入默认错开 300 ms。
7. 缺失 Slave 在后台继续扫描；某台断线后只恢复该设备的 Windows BLE 授时连接。

只有 68 / 69 / 70 三台都完成 Windows BLE 连接和首次授时后，程序才输出：

```text
[READY] Online gateways: 68, 69, 70
```

如果只有部分设备在线，例如只有 69，则正常输出：

```text
[WAIT] Online gateways: 69
[OFFLINE] Gateways: 68, 70
```

69 会正常保持连接和周期授时，68 / 70 在后台继续重试。

## 4. 与 Master-Serial-Control 的边界

本工程负责：

```text
Windows BLE → Slave：UTC Time Sync
Slave → Windows BLE：Time Status Notify
```

本工程不负责：

```text
Windows → Master：START / STOP / STATUS
Master → Slave：START / 30 Hz TICK / STOP
Slave GPIO2：30 Hz 相机硬件触发
Slave UART：TIMESYNC / START / STOP 发给开发板
```

这些功能分别由 `Master-Serial-Control`、Master 固件和 Slave 固件完成。

旧版 Windows BLE 采集控制协议已经退出 v3 运行链路，旧 `app/gateway_protocol.py` 及其旧单元测试已移除。

## 5. 与 capture_ui 的关系

`capture_ui` 已完成 v3 接入。

点击 UI 的 **启动 BLE 授时** 时：

```text
Master-Serial-Control
+
BLE-TimeSync
```

会并行启动，不会等待 Master 三台控制链全部 READY 后才启动 Windows BLE。

完整采集 READY 仍要求：

```text
Master：S68 / S69 / S70 全部 CONNECTED
+
BLE-TimeSync：68 / 69 / 70 全部连接并完成首次授时
```

因此部分硬件在线时，可以继续验证已在线 Slave 的 BLE 授时，但 UI 不会进入最终可采集状态。

## 6. 配置

配置文件：

```text
config/config.json
```

当前关键默认值：

```json
{
  "gateways": {
    "68": "68",
    "69": "69",
    "70": "70"
  },
  "sync_interval_seconds": 30,
  "sync_stagger_ms": 300,
  "calibration_warmup_samples": 0,
  "calibration_samples": 1
}
```

常规 `run` 初始化只做一次首次测量，避免旧版多轮校准在连接阶段连续占用 BLE 链路。

## 7. 命令

```powershell
# 只扫描，不连接、不授时
.\.venv\Scripts\python.exe -m app.main scan

# 连接候选 Slave，查看 GATT，并发送一次 UTC 探测
.\.venv\Scripts\python.exe -m app.main inspect

# 完成一次初始测量，再发送一次补偿后的授时并退出
.\.venv\Scripts\python.exe -m app.main once

# 连接三台 Slave 并持续每 30 秒授时
.\.venv\Scripts\python.exe -m app.main run

# 保留测量，但周期授时不应用补偿
.\.venv\Scripts\python.exe -m app.main run --no-compensation

# UI 子进程模式：stdin 只接受 quit/exit
.\.venv\Scripts\python.exe -m app.main run --control-stdin
```

## 8. 运行环境

要求 Windows、64 位 Python 3.11 或 3.12，以及：

```text
bleak>=2,<4
```

安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

## 9. 日志

运行产生：

```text
logs/timesync_YYYYMMDD_HHMMSS.csv
logs/timesync_YYYYMMDD_HHMMSS.jsonl
logs/latest.log
```

授时请求使用 12 字节 Little Endian `<QI>`，Time Status 严格按 32 字节 `<QqQQ>` 解析。

## 10. 当前实机验证状态

已使用 Master + Slave69 完成：

```text
Master 已连接 Slave69
Windows 在 Master 已占用一条 BLE 后仍能扫描到 Slave69
Windows 成功建立第二条 BLE
Time Status Notify 正常
首次授时正常
30 秒周期授时正常
缺失 68 / 70 时后台持续重试
```

Slave69 实测状态：

```text
BLE_CONNECTIONS=2
SYNC=YES
```

当前没有 Slave68 / Slave70 硬件，因此三台同时在线的最终 READY 和三机同步采集仍待后续实机验收。

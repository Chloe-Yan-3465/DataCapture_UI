# Mode2 Coordinator TimeSync（Windows）

本程序让 Windows 笔记本通过 USB 串口给 `Mode2Coordinator` 授时，并通过同一串口发送三机同步采集的 `START` / `STOP`。

## 当前链路

```text
Windows 系统时间
  -> USB CDC 串口 115200 8N1
  -> Mode2Coordinator
  -> BLE（中控作为 Central）
  -> Mode2Node-1 / Mode2Node-2 / Mode2Node-3
  -> UART
  -> 三块 NanoPi
```

`Mode2Coordinator` 当前不是 BLE Peripheral/GATT Server，因此 Windows 不能通过修改设备名或 UUID 直接连接中控。固件中的以下配置只属于中控与 wearable 之间的内部 BLE 链路，Windows 程序不会占用它们：

- 中控本地 BLE 名称：`Mode2Coordinator`（Central，不广播可连接服务）
- wearable 名称：`Mode2Node-1/2/3`
- Service：`5d6f0001-4f3c-4f59-a9f2-36f36a7c1000`
- Command：`5d6f0002-4f3c-4f59-a9f2-36f36a7c1000`
- Status：`5d6f0003-4f3c-4f59-a9f2-36f36a7c1000`

旧的 `ESP32S3-Gateway-68/69/70` 与 `12345678-...` GATT 配置不适用于当前 Mode2 架构。

## 准备

1. 确保 Windows 系统时间已由 Windows Time/NTP 校准。程序传递的是 Windows 当前 UTC；它不会替代 Windows 自身的网络校时。
2. 在设备管理器中确认中控的 COM 口。
3. 修改 `config/config.json` 中的 `serial_port`。默认值是此前测试使用的 `COM14`。
4. 关闭串口调试助手。一个 COM 口同一时刻只能由一个程序稳定占用。

首次安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

## 运行

先查看串口：

```powershell
.\.venv\Scripts\python.exe -m app.main scan
```

只验证串口和一次 `TIME_QUERY`，不修改设备时间：

```powershell
.\.venv\Scripts\python.exe -m app.main inspect
```

完成一次授时后退出：

```powershell
.\.venv\Scripts\python.exe -m app.main once
```

持续授时并启用采集控制：

```powershell
.\.venv\Scripts\python.exe -m app.main run
```

运行后：

- 按 `S`：向中控发送 `SCAN`，触发一次 4 秒 wearable 扫描；中控平时不会自动扫描新设备。
- 按 `1`：先完成一轮新授时并等待2秒应用窗口，再发送无 ID 的裸
  `START`（兼容模式使用 `test/L_test`），最后等待中控报告当前已连接节点均已 `ARMED`。
- UI 控制模式发送 `start <task_name> <complex_level>`，串口对应
  `START TASK=<task_name> LEVEL=<complex_level>`。
- 按 `0`：发送无 ID 的 `STOP`。
- `Ctrl+C`：退出并关闭串口。

供本地 UI 使用：

```powershell
.\.venv\Scripts\python.exe -m app.main run --control-stdin
```

stdin 接受 `1/start`、`start <task_name> <complex_level>`、`0/stop`、`quit`。

## 授时协议

所有命令和响应均为 ASCII 文本，每条以换行结束。

```text
Windows -> TIME_QUERY <seq>
ESP32   -> TIME_REPLY <seq> <coordinator_rx_us> <coordinator_tx_us>

Windows -> TIME_SET <seq> <coordinator_ref_us> <utc_ref_ns> <uncertainty_us>
ESP32   -> TIME_PLAN ...
ESP32   -> TIME_ACCEPT seq=<seq> nodes=<connected_count> uncertainty_us=<value>
        或 TIME_ERROR ...
```

Windows 连续查询多次，剔除预热样本，选择净 RTT 最小的样本。`coordinator_ref_us` 和 `utc_ref_ns` 都取该次往返的中点，`uncertainty_us` 取扣除 ESP32 处理时间后的半程延迟上界。只有收到 `TIME_ACCEPT`，该轮授时才记录为成功；`nodes` 可以是当前实际连接并成功授时的 1～3 个节点，Windows 不要求固定等于 3。

采集控制严格发送：

```text
SCAN
START TASK=<task_name> LEVEL=<complex_level>
STOP
```

Windows 不发送 session ID；中控固件自行生成内部 session，并把 task、level
及 session 统一下发给当前已连接的 wearable。

## 输出

`logs/` 中包含：

- `latest.log`：串口原始响应、连接、授时和控制状态。
- `timesync_*.csv`：每次查询的 RTT、中控时间、Windows UTC 映射、选中样本和接受节点数。
- `timesync_*.jsonl`：与 CSV 对应的机器可读记录。

## 常见问题

- `Cannot open COMx`：COM 号错误、串口助手仍占用端口，或中控未连接。
- `TIME_ERROR nodes are not synchronized`：中控与 wearable 的 BLE 时钟拟合尚未完成；`run` 会保持串口连接并重试。
- `START rejected`：当前参与录制的某个已连接 wearable 未同步，或状态不是 `IDLE/FAULT`。
- 看不到 `TIME_ACCEPT nodes=<count>`：当前没有节点完成可用的 BLE 时钟拟合；检查需要参与录制的 wearable 电源与 BLE 状态。

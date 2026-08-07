# 联合数据采集 UI

这是一个仅在本机运行的 Web 控制台，用于统一管理 v3 采集链路中的三个子系统：

- `Master-Serial-Control`：通过 Master ESP32 的 USB 串口负责 `START / STOP / STATUS`。
- `BLE-TimeSync`：Windows 直接连接 Slave `68 / 69 / 70`，只负责 UTC 授时。
- `VIVE-Tracker_capture`：通过 OpenXR 采集 VIVE Tracker 位姿并保存 CSV / JSON。

服务只监听 `127.0.0.1`，默认地址：

```text
http://127.0.0.1:8765
```

不会向局域网开放。

## 1. v3 通信架构

```text
                         USB Serial 115200 8N1
Windows UI ─────────────────────────────────────→ Master ESP32
    │                                                │
    │                                                │ BLE START/TICK/STOP
    │                                                ├────→ Slave 68
    │                                                ├────→ Slave 69
    │                                                └────→ Slave 70
    │
    ├── BLE UTC 授时 ───────────────────────────────→ Slave 68
    ├── BLE UTC 授时 ───────────────────────────────→ Slave 69
    └── BLE UTC 授时 ───────────────────────────────→ Slave 70

VIVE Tracker ── OpenXR ──→ Windows UI
```

职责固定分离：

```text
Windows → Master：USB 串口 START / STOP / STATUS
Master  → Slave ：BLE START / 30 Hz TICK / STOP
Windows → Slave ：BLE UTC Time Sync
VIVE    → Windows：OpenXR Tracker 位姿
```

`BLE-TimeSync` 不负责采集 START / STOP，Windows 也不直接向 Slave 写 Pulse Control。

## 2. 启动前准备

### Master ESP32

Master 通过 USB 串口连接 Windows，固定参数为：

```text
115200 8N1
```

串口配置位于：

```text
Master-Serial-Control\config\config.json
```

当前实机测试环境使用 `COM21`。如果电脑存在多个串口，建议明确配置 Master 端口，例如：

```json
{
  "serial_port": "COM21"
}
```

`AUTO` 只适用于 Windows 当前恰好可见一个串口的情况；多串口时程序会拒绝自动猜测设备。

### BLE Slave

三台 Slave 的 BLE 名称必须严格为：

```text
68
69
70
```

每台 Slave 同时允许两条 BLE Central 连接：

```text
Master  → Slave：控制链路
Windows → Slave：授时链路
```

Master 建立第一条连接后，Slave 仍需继续广播，直到 Windows 建立第二条连接。

### VIVE

启动并确认：

```text
Steam
SteamVR
VIVE Hub
```

Tracker 需要正常连接、定位，并在 SteamVR 中完成角色分配。角色或设备变化后，需要重新生成：

```text
VIVE-Tracker_capture\tracker_roles.json
```

可以直接使用 UI 中的 **绑定 Tracker 角色**。

## 3. UI 启动预检

页面顶部当前检查 8 项：

```text
Windows 平台
BLE Python 3.12 虚拟环境
BLE 入口
Master Python 3.12 虚拟环境
Master 串口入口
uv
VIVE 采集入口
Tracker 角色映射
```

这些属于静态环境检查。ESP32 是否在线、BLE 是否连接、OpenXR 是否真正定位成功，需要在程序运行后动态判断。

## 4. 启动 UI

在项目根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\capture_ui\start_ui.ps1
```

也可以进入 `capture_ui` 后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start_ui.ps1
```

默认打开：

```text
http://127.0.0.1:8765
```

更换端口：

```powershell
.\start_ui.ps1 -Port 8877
```

不自动打开浏览器：

```powershell
.\start_ui.ps1 -NoBrowser
```

## 5. 启动 BLE 授时总进程

点击 **启动 BLE 授时** 后，UI 会并行启动：

```text
Master-Serial-Control
+
BLE-TimeSync
```

两者互不等待对方先完全 READY。

### Master-Serial-Control

工作目录：

```text
Master-Serial-Control
```

实际入口：

```powershell
.\.venv\Scripts\python.exe -u -m app.main run --control-stdin
```

负责：

```text
打开 Master USB 串口
主动请求 STATUS
持续接收 Master 每约 5 秒的异步 STATUS
判断 S68 / S69 / S70 控制链状态
接收 UI 的内部 1 / 0 命令并转换为 START / STOP 串口协议
```

### BLE-TimeSync

工作目录：

```text
BLE-TimeSync
```

实际入口：

```powershell
.\.venv\Scripts\python.exe -u -m app.main run --control-stdin
```

负责：

```text
扫描 68 / 69 / 70
建立 Windows → Slave BLE 连接
订阅 Time Status Notify
首次校准和 UTC 授时
每 30 秒周期授时
缺失或断线 Slave 的后台重连
```

`--control-stdin` 在 BLE-TimeSync 中只用于 `quit/exit` 生命周期控制，不接收采集 START / STOP。

## 6. READY 判定

完整采集只有在下面两个条件同时满足时才进入 `ble_ready`：

```text
1. Master-Serial-Control READY
   └─ S68 / S69 / S70 全部 CONNECTED

2. BLE-TimeSync READY
   └─ Windows 已连接 68 / 69 / 70，并完成三台首次授时
```

因此部分硬件在线时，已经在线的设备仍然可以正常连接和授时，但 UI 不会允许正式开始采集。

例如只有 Slave69 时，正常状态是：

```text
Master：
S68=DISCONNECTED
S69=CONNECTED
S70=DISCONNECTED

BLE-TimeSync：
[WAIT] Online gateways: 69
[OFFLINE] Gateways: 68, 70

UI 顶部：
[68] ERROR
[69] IDLE
[70] ERROR
```

此时页面保持“BLE 启动中”属于正常现象，不代表 69 的授时失败。

## 7. 网关状态含义

| 状态 | 含义 |
|---|---|
| `IDLE` | Master 已连接该 Slave，目前未采集 |
| `WAIT_START_ACK` | 已请求开始，等待 Master START 确认 |
| `RUNNING` | Master 已确认 START，正在采集 |
| `WAIT_STOP_ACK` | 已请求停止，等待 Master STOP 确认 |
| `ERROR` | Slave 控制链未连接或通信异常 |
| `OTA` | OTA 模式 |

左侧 BLE-TimeSync 日志框同时显示 BLE-TimeSync 和 Master-Serial-Control 日志；Master 日志统一带 `[MASTER]` 前缀，不额外增加第三个终端。

## 8. 开始一轮采集

完整 READY 后：

1. 设置 Tracker 采样率，默认 `120 Hz`。
2. 点击 **开始**。
3. UI 先启动 VIVE OpenXR Tracker 采集。
4. Tracker 创建本轮输出目录并真正进入记录状态后，UI 向 Master-Serial-Control stdin 发送：

   ```text
   1
   ```

5. Master-Serial-Control 生成新的 `uint32 session_id`，通过 USB 串口发送：

   ```text
   START+<session_id>
   ```

6. Master 确认三台 Slave 均已连接后，启动本机 GPIO2，并向三台 Slave 下发 START 和连续约 30 Hz TICK。
7. UI 收到 `[START_OK]` 后才进入正式 `recording`。

如果任意 Slave 未连接，Master 会拒绝 START，UI 不应进入正式采集状态。

## 9. 停止一轮采集

点击 **停止** 后：

```text
UI → VIVE：stop
UI → Master-Serial-Control：0
Master-Serial-Control → Master：STOP+当前 session_id
Master → Slave 68/69/70：STOP
```

Tracker 停止并保存数据。本轮结束后：

```text
Master-Serial-Control 保持运行
BLE-TimeSync 保持运行
周期授时继续
```

因此可以继续开始下一轮采集。

## 10. 停止 BLE 授时

所有采集结束后点击 **停止 BLE 授时**。

UI 会依次停止：

```text
BLE-TimeSync
Master-Serial-Control
```

采集中不能直接停止通信总进程，应先停止当前采集。

## 11. Tracker 角色绑定

点击 **绑定 Tracker 角色** 后，UI 调用：

```powershell
VIVE-Tracker_capture\01_绑定Tracker角色.ps1 -NonInteractive
```

成功后更新：

```text
VIVE-Tracker_capture\tracker_roles.json
```

角色绑定和正式采集不能同时运行。

## 12. 日志与数据位置

BLE-TimeSync 日志：

```text
BLE-TimeSync\logs\latest.log
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.csv
BLE-TimeSync\logs\timesync_YYYYMMDD_HHMMSS.jsonl
```

VIVE 每轮采集输出：

```text
VIVE-Tracker_capture\vr_captures\vr_capture_YYYYMMDD_HHMMSS\
├── tracker_poses.csv
└── metadata.json
```

页面底部会显示当前 VIVE 输出目录。

## 13. 当前实机验证状态

已使用 Master + Slave69 完成以下实机验证：

```text
Windows UI → Master-Serial-Control
Master USB Serial 115200 8N1
Master 周期 STATUS
Master → Slave69 BLE 控制连接
Windows → Slave69 BLE 授时连接
Slave69 双 BLE 同时连接
Time Status Notify
首次 UTC 授时
30 秒周期授时
缺失 Slave 后台重试
UI 实时显示 Master 状态
```

实测 Slave69：

```text
BLE_CONNECTIONS=2
SYNC=YES
```

实测 Master：

```text
S68=DISCONNECTED
S69=CONNECTED
S70=DISCONNECTED
STATE=IDLE
SESSION=0
PULSES=0
```

当前没有 Slave68 / Slave70 硬件，因此三 Slave 同步 START / TICK / STOP 尚未完成最终实机验收。

## 14. 开发校验

UI：

```powershell
cd .\capture_ui
..\BLE-TimeSync\.venv\Scripts\python.exe -m compileall -q .
..\BLE-TimeSync\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

BLE-TimeSync：

```powershell
cd ..\BLE-TimeSync
.\.venv\Scripts\python.exe -m compileall -q app tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Master-Serial-Control：

```powershell
cd ..\Master-Serial-Control
.\.venv\Scripts\python.exe -m compileall -q app tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

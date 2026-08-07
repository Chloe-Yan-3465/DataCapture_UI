# Master Serial Control

## 1. 作用

本工程只负责 **Windows 上位机 ↔ Master ESP32 USB 串口控制链路**。

固定职责：

```text
Windows --USB Serial 115200 8N1--> Master ESP32

Windows -> Master:
START+<session_id>\r\n
STOP+<session_id>\r\n
STATUS\r\n

Master -> Windows:
ACK+START+...
ACK+STOP+...
STATUS+...
EVENT+SLAVE+...
```

本工程不负责 Windows BLE 授时，也不直接连接 Slave BLE。Master ESP32 收到 START / STOP 后如何通过 BLE 控制 68 / 69 / 70，属于 Master 固件职责。

## 2. 串口配置

线上协议固定为：

```text
115200 baud
8 data bits
no parity
1 stop bit
ASCII/UTF-8 text
```

Windows 命令以 `\r\n` 发送，Master 正式协议帧以 `\r\n` 结束。

配置文件：

```text
config/config.json
```

当前实机测试环境已明确配置 Master 为 `COM21`。

如果以后 Master COM 口变化，应修改：

```json
{
  "serial_port": "COMx"
}
```

`AUTO` 只在 Windows 当前恰好看到一个串口时可自动使用；存在多个 COM 口时程序会拒绝猜测。

也可以临时覆盖：

```powershell
.\.venv\Scripts\python.exe -m app.main --port COM21 status
```

## 3. 安装

在 `Master-Serial-Control` 根目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

依赖：

```text
pyserial>=3.5,<4
```

## 4. 命令

### 枚举串口

```powershell
.\.venv\Scripts\python.exe -m app.main ports
```

### 查询 STATUS

```powershell
.\.venv\Scripts\python.exe -m app.main --port COM21 status
```

正常示例：

```text
STATUS+STATE=IDLE+...+S68=CONNECTED+...+S69=CONNECTED+...+S70=CONNECTED+...+END
```

只有 `S68 / S69 / S70` 都是 `CONNECTED` 才允许 START。

### 单次 START

```powershell
.\.venv\Scripts\python.exe -m app.main --port COM21 start --session-id 15
```

成功：

```text
ACK+START+SESSION=15+OK+RATE=30+WIDTH_US=5000+END
```

如果不指定 `--session-id`，程序自动生成 `uint32 session_id`。

### 单次 STOP

单次命令模式已经退出进程，因此必须显式使用与 START 相同的 session：

```powershell
.\.venv\Scripts\python.exe -m app.main --port COM21 stop --session-id 15
```

成功示例：

```text
ACK+STOP+SESSION=15+OK+PULSES=1234+REASON=WINDOWS+END
```

### 持续控制模式

```powershell
.\.venv\Scripts\python.exe -m app.main --port COM21 run
```

键盘控制：

```text
1 = START，自动生成新的 session_id
0 = STOP，复用当前 session_id
S = STATUS
Q = 退出
```

程序启动后主动请求一次 STATUS。Master 后续每约 5 秒主动输出 STATUS，后台 RX 线程持续异步解析，不假设一次写命令只对应下一行返回。

三台 Slave 全部连接时输出：

```text
[READY] Master serial connected; S68/S69/S70 CONNECTED
```

缺设备时输出：

```text
[NOT_READY] Master Slave links missing: ...
```

## 5. capture_ui 接口

`capture_ui` 已正式接入本工程，不再是预留接口。

UI 启动通信系统时使用：

```powershell
.\.venv\Scripts\python.exe -u -m app.main run --control-stdin
```

stdin 内部控制命令：

```text
1
0
status
quit
```

这里的 `1 / 0` 只是 UI 与 Python 子进程之间的内部命令；实际 USB 串口仍发送：

```text
START+<session_id>\r\n
STOP+<same_session_id>\r\n
```

UI 会把本工程日志合并显示到左侧 BLE-TimeSync 日志框，并统一添加 `[MASTER]` 前缀。

## 6. 与 BLE-TimeSync 的关系

UI 点击 **启动 BLE 授时** 后，会并行启动：

```text
Master-Serial-Control
+
BLE-TimeSync
```

Master-Serial-Control 不应等待 BLE-TimeSync；BLE-TimeSync 也不应等待 Master 三台控制链全部 READY 后才启动。

完整采集 READY 由 UI 联合判断：

```text
Master：S68 / S69 / S70 全 CONNECTED
+
BLE-TimeSync：68 / 69 / 70 全部完成 Windows BLE 首次授时
```

## 7. 关键安全逻辑

1. 每次 START 前主动请求 STATUS。
2. 只要 68 / 69 / 70 任意一台不是 `CONNECTED`，Windows 不发送 START。
3. START 和 STOP 使用同一 `session_id`。
4. 串口 RX 始终由独立后台线程接收。
5. Master 异步 STATUS / EVENT 与 START / STOP ACK 可以交错到达，客户端按消息类型、命令和 session_id 匹配。
6. Master 运行中因 Slave 断线自动 STOP 时，客户端解析 `REASON=SLAVE_DISCONNECTED` 并清除本地活动会话。
7. 没有活动 session 时，持续控制模式不会凭空构造 STOP，会返回 `No active Master session is known`。

## 8. 当前实机验证状态

已使用 Master + Slave69 验证：

```text
COM21 打开正常
115200 8N1 正常
主动 STATUS 正常
Master 每 5 秒异步 STATUS 正常
S69=CONNECTED 正常
S68/S70 缺失时正确显示 NOT_READY
STATE=IDLE / SESSION=0 / PULSES=0 保持正确
```

当前没有 Slave68 / Slave70，所以正式三 Slave START / TICK / STOP 仍待硬件齐全后最终验收。

## 9. 不接硬件验证

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

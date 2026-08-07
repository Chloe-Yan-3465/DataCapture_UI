# BLE-TimeSync v3 工作上下文

最后更新：2026-08-07

## 当前职责

BLE-TimeSync 已从旧版“授时 + BLE START/STOP 控制”改为纯 Windows BLE 授时工程。

当前链路：

```text
Windows BLE ── Time Sync / Time Status ──→ Slave 68/69/70
```

采集控制链路：

```text
Windows USB Serial ──→ Master ESP32 ── BLE START/TICK/STOP ──→ Slave 68/69/70
```

## 当前代码边界

- `app/protocol.py`：12 字节 UTC 请求与 32 字节 Time Status 协议。
- `app/time_sync.py`：RTT、ESP 处理时间和补偿计算。
- `app/ble_client.py`：只发现 Service、Time Sync、Time Status；不发现或写旧 Control/Gateway Status。
- `app/gateway_manager.py`：管理三台 Slave 的 Windows BLE 连接、首次授时、周期授时和重连。
- `app/main.py`：`run` 不提供采集 START/STOP；`--control-stdin` 只用于 `quit/exit` 生命周期控制。
- 旧 `app/gateway_protocol.py` 及对应旧单元测试已删除，不再保留历史 BLE 采集控制实现。

## 固定设备与连接约束

BLE 名称严格为：

```text
68
69
70
```

每台 Slave 必须允许两条 BLE Central 连接：

```text
Master + Windows
```

Slave 在 Master 建立第一条连接后继续广播，直到 Windows 建立第二条连接。

## 授时参数

- Service：`12345678-1234-1234-1234-123456789abc`
- Time Sync：`12345678-1234-1234-1234-123456789abd`
- Time Status：`12345678-1234-1234-1234-123456789abe`
- 请求：12 字节 Little Endian `<QI>`
- 响应：32 字节 Little Endian `<QqQQ>`
- 周期：30 秒
- 三机写入错开：300 ms
- 首次测量：0 warm-up + 1 measured sample

## capture_ui 集成状态

`capture_ui` 已完成 v3 改造：

```text
点击“启动 BLE 授时”
    ├─ 并行启动 Master-Serial-Control
    └─ 并行启动 BLE-TimeSync
```

不能让 BLE-TimeSync 等待 Master 三台 Slave 全部 READY 后才启动，否则部分设备在线时 Windows 无法建立第二条 BLE 授时连接。

最终 `ble_ready` 必须同时满足：

```text
_master_ready = true
_ble_ready = true
```

也就是：

```text
Master 侧 68/69/70 全部 CONNECTED
+
Windows BLE 侧 68/69/70 全部完成首次授时
```

采集 START / STOP 由 UI 发送给 `Master-Serial-Control` stdin：

```text
1 = START
0 = STOP
```

BLE-TimeSync stdin 不接受 1/0。

## 当前实机验证

已使用 Master + Slave69 完成联调：

```text
Master USB Serial：正常
Master → Slave69 BLE：CONNECTED
Windows → Slave69 BLE：第二条连接正常
Slave69：BLE_CONNECTIONS=2
Slave69：SYNC=YES
首次授时：正常
30 秒周期授时：正常
UI 中 Master 和 BLE-TimeSync 并行运行：正常
缺 68/70 时后台重试：正常
```

当前缺少 Slave68 / Slave70 硬件，所以完整三机 READY、START/TICK/STOP 仍待最终验收。

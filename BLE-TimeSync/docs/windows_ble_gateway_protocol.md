# v3 Windows—Master—Slave 30 Hz 采集与授时协议

协议版本：`V3-BLE-30HZ-1`  
本文档是 Windows 上位机、Master ESP32、68/69/70 Slave ESP32 和开发板之间的固定接口定义。

## 1. 最终通信架构

```text
                         USB Serial 115200
Windows 上位机 ─────────────────────────────→ Master ESP32
    │                                            │
    │ BLE 授时（分别连接 68/69/70）               │ BLE START/TICK/STOP
    │                                            ├────────→ Slave 68
    ├────────────────────────────────────────────┼────────→ Slave 69
    │                                            └────────→ Slave 70
    │
    └─ 每台 Slave 与 Windows 建立独立 BLE 连接

Slave GPIO2 ── 30 Hz / 高电平 5 ms ──→ 相机硬件触发输入
Slave GPIO42 ── UART 授时、START、STOP ──→ 对应开发板
Slave GPIO41 ←── UART RX（本协议当前不解析开发板返回）
```

链路职责固定如下：

| 来源 | 目标 | 传输内容 |
|---|---|---|
| Windows | Master | USB 串口 START、STOP、STATUS |
| Master | Slave 68/69/70 | BLE START、30 Hz TICK、STOP |
| Windows | Slave 68/69/70 | BLE UTC 授时 |
| Slave | 相机 | GPIO2 硬件触发脉冲 |
| Slave | 开发板 | GPIO42 UART UTC、START、STOP |

Windows 不向 Slave 发送 START/STOP，也不向 Master 发送授时数据。

## 2. 固定设备配置

### 2.1 Master

| 项目 | 固定值 |
|---|---|
| Windows 控制接口 | Master USB 调试串口 `Serial` |
| 串口参数 | `115200 8N1` |
| BLE 角色 | Central/Client |
| 目标设备 | BLE 名称严格为 `68`、`69`、`70` |
| 本机 GPIO2 | 同时输出 30 Hz、5 ms 高电平脉冲，供调试或本机触发使用 |
| OTA AP | `ActionMaster`，按键开启 |

### 2.2 三台 Slave

| 设备 | BLE 名称 | 固件入口 | OTA AP | OTA Hostname |
|---:|---|---|---|---|
| 68 | `68` | `v3_hwsync/slavetimesync_68.cpp` | `ActionSlave68` | `esp32-slave-68` |
| 69 | `69` | `v3_hwsync/slavetimesync_69.cpp` | `ActionSlave69` | `esp32-slave-69` |
| 70 | `70` | `v3_hwsync/slavetimesync_70.cpp` | `ActionSlave70` | `esp32-slave-70` |

每台 Slave 需要同时保持两条 BLE 连接：

1. Master 连接，用于写入 START、TICK 和 STOP。
2. Windows 连接，用于写入授时数据并接收授时结果 Notify。

Slave 在第一条 BLE 连接建立后继续广播，直到连接数达到 2。Windows 扫描时应按设备名建立 68/69/70 三个独立连接对象。

当前 BLE 未启用配对、绑定或应用层鉴权。Windows 只能写授时特征值，不应写脉冲控制特征值。

## 3. Windows 与 Master 的 USB 串口协议

### 3.1 分帧

- 波特率：`115200`
- 数据位/校验/停止位：`8/N/1`
- 编码：ASCII/UTF-8
- Windows 发送的每条命令必须以 `\n` 结尾；也可使用 `\r\n`。
- Master 返回的正式协议帧均以 `\r\n` 结尾。
- Master 每 5 秒主动输出一次 STATUS，因此 Windows 必须按行异步接收，不能假设一次写命令只对应一行返回。

### 3.2 开始采集

Windows 发送：

```text
START+<session_id>\r\n
```

示例：

```text
START+15\r\n
```

`session_id` 为十进制 `uint32`。同一次采集的 START 和 STOP 必须使用相同值。

三台 Slave 均已连接时，Master 返回：

```text
ACK+START+SESSION=15+OK+RATE=30+WIDTH_US=5000+END\r\n
```

存在未连接 Slave 时：

```text
ACK+START+SESSION=15+ERROR+SLAVES_NOT_READY+END\r\n
```

已经运行时：

```text
ACK+START+SESSION=15+ERROR+ALREADY_RUNNING+END\r\n
```

成功 START 后，Master 执行：

1. GPIO2 启动 30 Hz、5 ms 高电平 LEDC 输出。
2. 向三台 Slave 分别发送 BLE START 帧。
3. 每 `33333 us` 产生一个 BLE TICK 帧，即名义频率约 `30.0003 Hz`。

### 3.3 停止采集

Windows 发送：

```text
STOP+15\r\n
```

正常返回：

```text
ACK+STOP+SESSION=15+OK+PULSES=1234+REASON=WINDOWS+END\r\n
```

如果已经停止：

```text
ACK+STOP+SESSION=15+OK+ALREADY_STOPPED+END\r\n
```

会话不匹配：

```text
ACK+STOP+SESSION=16+ERROR+SESSION_MISMATCH+CURRENT=15+END\r\n
```

运行期间任意 Slave BLE 断线，Master 自动停止本机脉冲并向仍在线 Slave 发 STOP：

```text
ACK+STOP+SESSION=15+OK+PULSES=1234+REASON=SLAVE_DISCONNECTED+END\r\n
```

### 3.4 查询状态

Windows 发送：

```text
STATUS\r\n
```

返回示例：

```text
STATUS+STATE=RUNNING+SESSION=15+RATE=30+WIDTH_US=5000+PULSES=1234+QUEUE_DROP=0+S68=CONNECTED+S68_TX=1235+S68_SKIP=0+S69=CONNECTED+S69_TX=1235+S69_SKIP=0+S70=CONNECTED+S70_TX=1235+S70_SKIP=0+END\r\n
```

Master 控制链的必要条件是 `S68/S69/S70` 均为 `CONNECTED`。在联合 UI 中，还必须同时确认 Windows BLE-TimeSync 已完成 68/69/70 三台的首次授时，才允许用户点击开始。

### 3.5 Master 事件和错误

可能异步出现：

```text
EVENT+SLAVE+DEVICE=68+FOUND+ADDR=xx:xx:xx:xx:xx:xx+END
EVENT+SLAVE+DEVICE=68+CONNECTED+MTU=64+END
EVENT+SLAVE+DEVICE=68+DISCONNECTED+END
ACK+ERROR+INVALID_COMMAND+END
ACK+ERROR+LINE_TOO_LONG+END
```

Windows 应记录这些行，但只把 `ACK+START`、`ACK+STOP` 和 `STATUS` 用作采集状态判断。

## 4. Master 与 Slave 的 BLE 协议

### 4.1 GATT 配置

| 名称 | UUID | 属性 | 写入方 |
|---|---|---|---|
| 主服务 | `12345678-1234-1234-1234-123456789abc` | Service | — |
| Time Sync | `12345678-1234-1234-1234-123456789abd` | Write、Write Without Response | Windows |
| Time Status | `12345678-1234-1234-1234-123456789abe` | Read、Notify | Slave→Windows |
| Pulse Control | `12345678-1234-1234-1234-123456789ac1` | Write、Write Without Response | Master |

Master 使用 Pulse Control 的 Write Without Response，避免 30 Hz 三连接下等待逐包 ACK。Windows 不得写 Pulse Control。

### 4.2 16 字节脉冲控制帧

所有多字节字段均为 Little Endian，帧长度固定为 16 字节：

| 偏移 | 长度 | 类型 | 字段 | 固定含义 |
|---:|---:|---|---|---|
| 0 | 2 | `uint16` | `magic` | `0x5033`，线上字节为 `33 50` |
| 2 | 1 | `uint8` | `version` | `1` |
| 3 | 1 | `uint8` | `command` | `1=START`、`2=TICK`、`3=STOP` |
| 4 | 4 | `uint32` | `session_id` | Windows 提供的采集会话号 |
| 8 | 2 | `uint16` | `sequence` | TICK 序号，允许自然回绕 |
| 10 | 2 | `uint16` | `pulse_width_us` | 当前固定 `5000` |
| 12 | 4 | `uint32` | `master_time_us` | Master 启动后单调微秒计数低 32 位 |

命令时序：

```text
START(sequence=0)
TICK(sequence=1)
TICK(sequence=2)
...
STOP(sequence=最后一个 TICK 序号)
```

Slave 使用相邻 TICK 的 `master_time_us` 计算输入频率，保留重复包、旧包、跳号和队列溢出统计。连续三个不同于当前 LEDC 整数频率的相同目标值才更新 LEDC。

### 4.3 脉冲安全策略

- Slave 收到 START 后启动 GPIO2 LEDC，初始值为 30 Hz、5 ms 高电平。
- 收到 STOP 后立即将 GPIO2 拉低。
- 运行中超过 `500 ms` 没有收到 START/TICK，Slave 自动停止 GPIO2，并通过 UART 向开发板发送 STOP。
- Master 端任意 Slave 断线时停止整组采集。

注意：BLE TICK 用于维持和测量频率，Slave 的 GPIO2 由本地 LEDC 连续产生。三台 Slave 的启动相位取决于各自收到 START 的时刻；本协议保证稳定的 30 Hz 频率控制，但不承诺三路边沿达到微秒级同相。

## 5. Windows 与 Slave 的 BLE 授时协议

Windows 必须直接连接三个 Slave，并分别授时。Master 不接收或转发 TIME 命令。

### 5.1 Windows 写入 Time Sync

UUID：

```text
12345678-1234-1234-1234-123456789abd
```

推荐固定发送 12 字节 Little Endian：

| 偏移 | 长度 | 类型 | 含义 |
|---:|---:|---|---|
| 0 | 8 | `uint64` | Unix UTC 秒数 |
| 8 | 4 | `uint32` | 当前秒内微秒数，范围 `0..999999` |

兼容 8 字节格式：只发送 UTC 秒，Slave 将微秒设为 0。

Windows 建议流程：

1. 扫描并连接 BLE 名称 `68`、`69`、`70`。
2. 对每台设备发现主服务和 Time Sync/Time Status。
3. 订阅 Time Status Notify。
4. 分别写入 12 字节 UTC 数据。
5. 建连后立即授时一次，之后每 30 秒一次；三台写入错开 200～500 ms。

### 5.2 Slave 返回 Time Status

UUID：

```text
12345678-1234-1234-1234-123456789abe
```

固定 32 字节 Little Endian：

| 偏移 | 长度 | 类型 | 含义 |
|---:|---:|---|---|
| 0 | 8 | `uint64` | `phone_us`，Windows 发送的 UTC 总微秒数 |
| 8 | 8 | `int64` | `offset_us`，设置前目标时间减 Slave 原系统时间 |
| 16 | 8 | `uint64` | `esp_receive_us`，Slave 启动后的接收时刻 |
| 24 | 8 | `uint64` | `esp_process_us`，完成 `settimeofday()` 的时刻 |

内部处理耗时：

```text
esp_process_us - esp_receive_us
```

后两个字段是启动后单调时间，不是 UTC。

## 6. Slave 与开发板的 UART 协议

### 6.1 电气和串口配置

| 项目 | 固定值 |
|---|---|
| Slave TX | GPIO42，连接开发板 RX |
| Slave RX | GPIO41，连接开发板 TX |
| 参数 | `115200 8N1` |
| 分帧 | 每帧以 `\r\n` 结束 |
| 当前方向 | 固件发送；当前版本不解析开发板返回帧 |

授时任务和控制任务共用 UART TX 互斥锁，完整文本帧不会互相穿插。

### 6.2 UTC 授时帧

Slave 每次收到 Windows BLE 授时后立即发送一次，之后每 5 秒发送一次：

```text
TIMESYNC+2026-08-07T12:34:56.123456Z+END\r\n
```

时间固定为 UTC，末尾 `Z` 不包含本地时区偏移。

### 6.3 开始帧

Slave 收到 Master BLE START、成功启动 GPIO2 后发送：

```text
CMD+START+SESSION=15+RATE=30+WIDTH_US=5000+END\r\n
```

开发板应使用 `SESSION` 关联一次采集，并检查 `RATE` 和 `WIDTH_US`。

### 6.4 停止帧

收到 Master BLE STOP 或脉冲接收超时后发送：

```text
CMD+STOP+SESSION=15+END\r\n
```

开发板应把 STOP 设计成幂等命令：重复收到同一会话 STOP 也必须保证采集已经停止并安全关闭文件。

## 7. Windows 推荐完整流程

### 7.1 初始化

Master 控制链和 Windows BLE 授时链应并行建立，不要求先等 Master 三台全部 READY 后才启动 Windows BLE：

```text
并行启动
├─ 打开 Master USB 串口 115200
│   └─ Master 自动连接 Slave 68/69/70，并持续输出 STATUS
│
└─ Windows 分别扫描并连接 BLE 68/69/70
    └─ 订阅 Time Status，分别发送 UTC 授时并确认 Notify

最终联合 READY 条件：
1. Master STATUS：S68/S69/S70 均 CONNECTED
2. Windows BLE-TimeSync：68/69/70 均已连接并完成首次授时
```

如果当前只发现部分 Slave，已在线设备仍应正常建立双 BLE 和完成授时，缺失设备继续后台重试；此时不得开始正式采集。

### 7.2 开始

```text
生成新的 uint32 session_id
    ↓
向 Master 写 START+session_id
    ↓
等待 ACK+START+...+OK
    ↓
Master 向三台 Slave 发 START 和连续 TICK
    ↓
三台 Slave 启动 GPIO2，并各自向开发板发 UART START
```

### 7.3 停止

```text
向 Master 写 STOP+同一 session_id
    ↓
等待 ACK+STOP+...+OK
    ↓
Master 向三台 Slave 发 STOP
    ↓
三台 Slave 拉低 GPIO2，并各自向开发板发 UART STOP
```

## 8. 固件与构建入口

`main_ss.cpp` 仅作为历史备份，不参与 v3 构建。

如果固件工程使用统一的 `platformio_v3.ini`，推荐使用用户目录下的 PlatformIO Core：

```powershell
$pio = "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe"
& $pio run -c platformio_v3.ini -e v3_master
& $pio run -c platformio_v3.ini -e v3_slave_68
& $pio run -c platformio_v3.ini -e v3_slave_69
& $pio run -c platformio_v3.ini -e v3_slave_70
```

也可以像当前实机调试一样，为 Master、Slave68、Slave69、Slave70 建立独立 PlatformIO 工程；无论采用哪种工程组织方式，下面的固件入口和线上协议必须保持一致。

对应源码：

- Master：`v3_hwsync/main_mm.cpp`
- Slave 公共实现：`v3_hwsync/slave_ble_pulse_common.hpp`
- 三个独立烧录入口：`slavetimesync_68.cpp`、`slavetimesync_69.cpp`、`slavetimesync_70.cpp`

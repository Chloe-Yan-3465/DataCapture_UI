# Windows 上位机—ESP32 网关 BLE 通信协议（旧版，已停用）

> 本文仅保留作 `ESP32S3-Gateway-68/69/70` 历史协议参考。当前 Mode2 中控方案请使用 `windows_mode2_coordinator_protocol.md`；不要把本文的设备名或 `12345678-...` UUID 配进当前程序。

## 1. 协议范围

本文档定义 Windows 笔记本与 ESP32-S3 BLE-UART 网关之间的 BLE 通信协议，供 Windows 上位机工程实现连接、授时、开始采集、停止采集和状态接收。

协议版本：`1.2`

系统链路：

```text
Windows 上位机
    ↕ BLE GATT
├─ ESP32S3-Gateway-68 ↔ UART ↔ Linux 68 ↔ 相机
├─ ESP32S3-Gateway-69 ↔ UART ↔ Linux 69 ↔ 相机
└─ ESP32S3-Gateway-70 ↔ UART ↔ Linux 70 ↔ 相机
```

Windows 通过 BLE 向 ESP32 发送授时和控制命令。ESP32 将控制命令转发给 Linux，并将 Linux 的执行结果通过 BLE Notify 原样返回 Windows。

## 2. BLE 基本配置

| 项目 | 配置值 |
|---|---|
| BLE 设备名 | `ESP32S3-Gateway-68/69/70`，详见下表 |
| 主服务 UUID | `12345678-1234-1234-1234-123456789abc` |
| 建议 MTU | `185` |
| 广播间隔 | 100～150 ms |
| 同时连接数 | 每块 ESP32 接受 1 个 Windows Central；Windows 同时维护 3 条连接 |
| 配对/绑定 | 当前版本不要求 |
| 应用层加密 | 当前版本无 |

ESP32 设置的本地 MTU 为 185。Windows 连接后应请求或协商尽可能接近 185 的 MTU。若 Windows BLE API 不允许主动指定 MTU，应读取协商结果，并确保其至少能够接收 32 字节的授时状态 Notify。

当前协议未配置 BLE 配对、绑定和应用层身份认证，附近设备理论上可以连接和写入控制命令。正式部署若有误操作或安全风险，需要另行增加白名单、配对或应用层令牌。

### 2.1 三台网关的固定名称与 Linux 映射

BLE 协议本身可以依靠设备地址区分三块 ESP32，并不强制设备名唯一。但本项目需要让 Windows 稳定识别每块 ESP32 所绑定的 Linux 开发板，因此三个固件必须使用不同的 BLE 名称，并将 Linux IP 末段写入名称。

| Linux IP 末段 | BLE 完整名称 | 对应固件 | OTA AP 名称 | OTA Hostname |
|---:|---|---|---|---|
| 68 | `ESP32S3-Gateway-68` | `slavetimesync_68.cpp` | `ActionSlave68` | `esp32-gateway-68` |
| 69 | `ESP32S3-Gateway-69` | `slavetimesync_69.cpp` | `ActionSlave69` | `esp32-gateway-69` |
| 70 | `ESP32S3-Gateway-70` | `slavetimesync_70.cpp` | `ActionSlave70` | `esp32-gateway-70` |

三台设备使用相同的主服务 UUID 和特征值 UUID，因为它们属于三条互相独立的 BLE 连接。Windows 必须为每台设备分别保存连接对象、特征值对象、订阅状态、授时状态、采集状态和最后一次 ACK，不能在三个连接之间共用 GATT 特征值句柄。

设备名可能位于 Scan Response 而不是主 Advertising 数据包中。Windows 扫描逻辑应等待并合并 Scan Response，使用主服务 UUID 找到候选设备，再按上述三个完整名称建立固定映射。

连接后还应读取 Gateway Status，使用返回的 `DEVICE=68/69/70` 再校验一次名称映射。BLE 名称用于发现，`DEVICE` 字段用于连接后的身份确认。

## 3. GATT 特征值

| 名称 | UUID | 属性 | Windows 用途 |
|---|---|---|---|
| Time Sync | `12345678-1234-1234-1234-123456789abd` | Write | 发送 UTC 授时数据 |
| Time Status | `12345678-1234-1234-1234-123456789abe` | Read、Notify | 接收授时结果 |
| Control | `12345678-1234-1234-1234-123456789abf` | Write | 发送 START、STOP、STATUS |
| Gateway Status | `12345678-1234-1234-1234-123456789ac0` | Read、Notify | 接收网关状态和 Linux 返回结果 |

Windows 连接后，应先订阅两个 Notify 特征值，再发送任何授时或控制数据：

1. 连接设备并发现主服务。
2. 订阅 `Time Status` Notify。
3. 订阅 `Gateway Status` Notify。
4. 发送一次授时数据。
5. 等待授时 Notify。
6. 读取或发送 `STATUS` 获取当前采集状态。
7. 根据用户操作发送 START/STOP。

所有 Write 建议使用“写入并等待 GATT Response”的方式。GATT 写入成功只表示 ESP32 已收到数据，不表示 Linux 或相机已经执行成功。

以上连接初始化流程需要对 68、69、70 三台设备分别执行。只有目标设备完成连接、两个 Notify 订阅和首次授时后，该设备才可进入“可控制”状态。

## 4. UTC 授时协议

### 4.1 写入特征值

写入 UUID：

```text
12345678-1234-1234-1234-123456789abd
```

数据采用二进制小端格式，支持 8 字节和 12 字节两种长度。

推荐 Windows 始终发送 12 字节格式：

| 偏移 | 长度 | 类型 | 字节序 | 含义 |
|---:|---:|---|---|---|
| 0 | 8 | `uint64` | Little Endian | Unix UTC 秒数 |
| 8 | 4 | `uint32` | Little Endian | 当前秒内的微秒数，范围 0～999999 |

8 字节兼容格式只包含 Unix UTC 秒数，ESP32 会将微秒部分设为 0。

示意：

```text
sec  = Unix UTC seconds
usec = UTC microseconds within the current second
payload = LE64(sec) + LE32(usec)
```

注意事项：

- 必须使用 UTC，不得发送本地时区时间。
- `sec` 是自 1970-01-01 00:00:00 UTC 起的秒数。
- 应尽量靠近 BLE Write 调用时刻读取 Windows 系统时间。
- 当前协议没有进行完整 NTP 式往返延迟补偿，授时精度需要通过实机测量确认。

### 4.2 建议发送频率

推荐策略：

- BLE 建立连接并完成 Notify 订阅后立即发送一次。
- 正常运行期间每 30 秒发送一次。
- 对 68、69、70 的周期授时建议错开 200～500 ms，避免三个连接在同一瞬间集中写入。
- BLE 重连后立即重新发送一次。
- Windows 从睡眠恢复、系统时间被校正或上位机重新开始实验时立即发送一次。
- 不建议高于 1 Hz 持续发送，避免频繁设置 ESP32 系统时间造成不必要抖动。

ESP32 每次成功授时后会立即向 Linux 发送一次 UTC，之后每 5 秒向 Linux 发送一次当前 UTC。

### 4.3 授时状态 Notify

Notify UUID：

```text
12345678-1234-1234-1234-123456789abe
```

返回固定 32 字节二进制，小端格式：

| 偏移 | 长度 | 类型 | 含义 |
|---:|---:|---|---|
| 0 | 8 | `uint64` | `phone_us`，Windows 发送的 UTC 总微秒数 |
| 8 | 8 | `int64` | `offset_us`，设置前目标时间与 ESP32 原系统时间之差 |
| 16 | 8 | `uint64` | `esp_receive_us`，ESP32 自启动后的单调接收时间 |
| 24 | 8 | `uint64` | `esp_process_us`，ESP32 完成设置后的单调时间 |

可以计算 ESP32 内部处理耗时：

```text
process_time_us = esp_process_us - esp_receive_us
```

`esp_receive_us` 和 `esp_process_us` 是 ESP32 启动后的单调计时，不是 UTC 时间。

## 5. 控制命令协议

### 5.1 写入特征值

写入 UUID：

```text
12345678-1234-1234-1234-123456789abf
```

控制命令使用 UTF-8/ASCII 文本，不包含二进制结构。BLE Write 中不需要附加 `NUL`、`CR` 或 `LF`。

### 5.2 开始采集

格式：

```text
START+<session_id>
```

示例：

```text
START+15
```

`session_id`：

- 类型为无符号 32 位十进制整数。
- 范围为 0～4294967295。
- 建议每次新采集使用新的编号。
- 同一次采集的 START 和 STOP 必须使用相同编号。

ESP32 转发给 Linux 的 UART 帧为：

```text
CMD+START+SESSION=15+END\r\n
```

### 5.3 停止采集

格式：

```text
STOP+<session_id>
```

示例：

```text
STOP+15
```

ESP32 转发给 Linux：

```text
CMD+STOP+SESSION=15+END\r\n
```

STOP 被视为安全命令。即使 ESP32 本地认为当前处于空闲状态，也会将 STOP 转发给 Linux，以便 Linux 确保相机已经停止。

### 5.4 查询状态

格式：

```text
STATUS
```

STATUS 不带会话编号。ESP32 使用当前会话编号向 Linux 转发：

```text
CMD+STATUS+SESSION=15+END\r\n
```

同时，ESP32 会通过 Gateway Status 返回自己的当前状态。

### 5.5 控制命令发送频率

控制命令均为事件触发，不应周期性发送：

- 用户开始一次采集时发送一次 START。
- 用户停止一次采集时发送一次 STOP。
- 连接建立、异常恢复或界面主动刷新时发送一次 STATUS。
- 不要以固定频率持续发送 START 或 STOP。

固件等待 Linux ACK 的超时时间为 3 秒。

推荐 Windows 重试策略：

- START 写入后，先等待 `FORWARDED`，再等待 Linux 的 `ACK+START`。
- 如果收到 `FORWARDED` 但没有收到 Linux ACK，不要盲目重复 START，因为 Linux 相机可能已经启动；应先发送 STATUS 或提示人工确认。
- STOP 可以使用相同 `session_id` 再次发送，以确保 Linux 最终进入停止状态。
- BLE 断线重连后先订阅 Notify，再发送 STATUS，不要自动重新发送上一条 START。

## 6. Gateway Status 返回协议

特征值 UUID：

```text
12345678-1234-1234-1234-123456789ac0
```

数据为 UTF-8/ASCII 文本，不带 `CR/LF`。Windows 应同时支持：

- Notify：实时接收新状态。
- Read：读取最近一次保存的状态文本。

### 6.1 ESP32 本地状态

格式：

```text
GW+DEVICE=<68|69|70>+STATE=<state>+SESSION=<session_id>+SYNC=<YES|NO>+END
```

示例：

```text
GW+DEVICE=68+STATE=IDLE+SESSION=0+SYNC=NO+END
GW+DEVICE=69+STATE=RUNNING+SESSION=15+SYNC=YES+END
```

状态值：

| 状态 | 含义 |
|---|---|
| `IDLE` | 空闲 |
| `WAIT_START_ACK` | START 已发给 Linux，等待启动确认 |
| `RUNNING` | Linux 已确认相机正在采集 |
| `WAIT_STOP_ACK` | STOP 已发给 Linux，等待停止确认 |
| `ERROR` | 命令、UART 或 ACK 超时错误 |

`DEVICE` 必须与当前连接的 BLE 名称 IP 尾号一致。如果连接名为 `ESP32S3-Gateway-68`，但状态返回 `DEVICE=69`，Windows 应禁止下发采集命令并报告固件烧录或设备映射错误。

### 6.2 命令已转发

```text
GW+START+SESSION=15+FORWARDED+END
GW+STOP+SESSION=15+FORWARDED+END
```

这只表示命令已经从 ESP32 写入 UART，不能作为相机实际启动或停止的最终依据。

### 6.3 重复命令

```text
GW+START+SESSION=15+ALREADY_RUNNING+END
GW+STOP+SESSION=15+ALREADY_FORWARDED+END
```

### 6.4 Linux 返回结果

Linux 通过 UART 返回的完整文本帧会被 ESP32 原样放入 Gateway Status Notify。

Linux 启动成功：

```text
ACK+START+SESSION=15+OK+END
```

Linux 停止成功：

```text
ACK+STOP+SESSION=15+OK+FRAMES=12345+END
```

Linux 启动失败：

```text
ACK+START+SESSION=15+ERROR+CAMERA_OPEN_FAILED+END
```

Linux 状态响应：

```text
STATUS+SESSION=15+RUNNING+FRAMES=3560+END
```

Windows 只有收到对应会话的 `ACK+START+...+OK+END` 后，才能将界面更新为“相机已开始采集”；收到 `ACK+STOP+...+OK+...+END` 后，才能认为本次采集正常结束。

### 6.5 ESP32 错误消息

可能返回：

| 消息 | 含义 |
|---|---|
| `GW+ERROR+INVALID_COMMAND+END` | 控制文本无法识别 |
| `GW+ERROR+INVALID_SESSION+END` | 会话编号格式错误 |
| `GW+ERROR+CONTROL_QUEUE_FULL+END` | 控制队列已满 |
| `GW+ERROR+TIME_LENGTH+END` | 授时数据不是 8 或 12 字节 |
| `GW+ERROR+TIME_USEC_RANGE+END` | 微秒值超出 0～999999 |
| `GW+ERROR+TIME_QUEUE_FULL+END` | 授时队列已满 |
| `GW+ERROR+BUSY+END` | 当前状态不允许新的 START |
| `GW+ERROR+UART_TX_BUSY+END` | UART 发送互斥锁超时 |
| `GW+ERROR+UART_LINE_TOO_LONG+END` | Linux 返回帧超过缓冲区 |
| `GW+ERROR+LINUX_ACK_TIMEOUT+END` | 3 秒内没有收到 Linux ACK |

## 7. Windows 推荐控制流程

### 7.1 建立连接

```text
扫描主服务 UUID，并读取 Scan Response
    ↓
按名称识别 Gateway-68、Gateway-69、Gateway-70
    ↓
分别建立三条 BLE 连接
    ↓
每条连接分别发现四个 GATT 特征值
    ↓
每条连接分别订阅 Time Status 和 Gateway Status
    ↓
分别发送 12 字节 UTC
    ↓
等待每台设备各自的 32 字节授时状态
    ↓
分别发送 STATUS，并建立三台设备的状态记录
```

### 7.2 开始采集

```text
为本次实验生成一个新的 session_id
    ↓
向 68、69、70 的 Control 特征值分别写入
"START+同一session_id"
    ↓
分别等待三台设备的 GW+START+...+FORWARDED
    ↓
分别等待三块 Linux 返回 ACK+START+...+OK
    ↓
三台均确认后，界面进入“全部采集中”
```

Windows 应分别显示每台设备的结果。如果某台失败，不应把另外两台的成功状态覆盖掉。建议同一次三机采集使用同一个 `session_id`，这样三台设备的日志和采集文件可以关联到同一次实验。

当前协议的 START 是“收到后立即转发并执行”，Windows 对三条 BLE 连接的写入存在先后顺序，因此本协议不保证三台 Linux 在完全相同的时刻开始。如果后续提出严格同步启动要求，需要新增带目标 UTC 的 `START_AT` 协议，不能仅依靠三个立即 START。

### 7.3 停止采集

```text
向 68、69、70 分别写入 "STOP+同一session_id"
    ↓
分别等待三台设备的 GW+STOP+...+FORWARDED
    ↓
分别等待三块 Linux 的 ACK+STOP+...+OK
    ↓
按设备分别记录帧数/文件信息
    ↓
三台均停止后，界面回到“全部空闲”
```

## 8. Windows 端实现检查表

- 使用主服务 UUID 过滤候选设备，并按三个完整 BLE 名称建立 68/69/70 固定映射。
- 正确读取 Scan Response；不能假设完整设备名一定在主 Advertising 数据包中。
- 连接后读取 Gateway Status，用 `DEVICE=68/69/70` 校验 BLE 名称与固件身份一致。
- 为三台设备分别保存连接、特征值、Notify、授时、状态和超时对象。
- 连接后重新发现特征值，不缓存失效的 GATT 对象。
- 先订阅 Notify，再进行授时和控制。
- 12 字节授时结构使用 Little Endian。
- 控制命令不附加 `\0`、`\r` 或 `\n`。
- 每次采集生成并保存一个 `uint32 session_id`。
- 同一次三机采集向 68、69、70 发送相同的 `session_id`，START 和 STOP 使用同一个编号。
- 分别处理“ESP32 已转发”和“Linux 已执行”两个阶段。
- 分别显示三台设备的 ACK，不能用单一全局状态覆盖某一台设备的失败。
- BLE 重连后先 STATUS，不自动重放 START。
- 将所有 Gateway Status 文本写入上位机日志，便于排查。
- 正式采集前检查 `SYNC=YES`。
- 对 3 秒 Linux ACK 超时给出明确提示。

## 9. 与 Linux UART 协议的边界

Windows 不直接解析 UART 字节流，但需要识别 ESP32 转发的 Linux 文本结果。Linux UART 的固定配置为：

```text
115200 baud
8 data bits
no parity
1 stop bit
RX GPIO41 / TX GPIO42（ESP32侧）
```

Linux 每条返回帧必须：

- 使用 ASCII/UTF-8 文本。
- 以 `+END` 结束有效内容。
- 再附加 `\r\n` 作为 UART 分帧标志。
- 单帧保持在 175 字节以内，以适配 BLE MTU 185 的 Notify。
- START/STOP ACK 必须携带正确的 `SESSION=<id>`。

协议后续若增加文件名、实验编号等字段，应继续保证单帧长度限制，或升级为带分片编号的二进制协议。

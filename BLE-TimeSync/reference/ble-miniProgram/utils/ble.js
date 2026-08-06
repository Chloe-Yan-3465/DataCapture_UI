/**
 * BLE 蓝牙通信封装模块
 * 
 * 与 ESP32S3-TimeSync 设备通信，匹配 esp32master.cpp 中的协议:
 * - Service UUID:  12345678-1234-1234-1234-123456789abc
 * - Write Char:    12345678-1234-1234-1234-123456789abd  (小程序写入时间)
 * - Notify Char:   12345678-1234-1234-1234-123456789abe  (ESP32返回状态)
 * 
 * ESP32 写入格式: 8字节 uint64 sec + 4字节 uint32 usec (共12字节)
 * ESP32 返回格式: ble_time_status_t (32字节)
 *   - phone_us:       uint64 (8B) — 手机发送的时间(us)
 *   - offset_us:      int64  (8B) — ESP32计算的偏移(us)
 *   - esp_receive_us:  uint64 (8B) — ESP32收到时的 esp_timer 值
 *   - esp_process_us:  uint64 (8B) — ESP32处理完的 esp_timer 值
 */

// 与 esp32master.cpp 匹配的 UUID
const SERVICE_UUID = '12345678-1234-1234-1234-123456789ABC';
const TIME_SYNC_CHAR_UUID = '12345678-1234-1234-1234-123456789ABD';
const TIME_STATUS_CHAR_UUID = '12345678-1234-1234-1234-123456789ABE';
const DEVICE_NAME_PREFIX = 'ESP32S3-TimeSync';

class BLEManager {
  constructor() {
    this.deviceId = '';
    this.serviceId = '';
    this.writeCharId = '';
    this.notifyCharId = '';
    this.connected = false;
    this.discovering = false;

    // 回调函数
    this.onDeviceFound = null;      // (device) => {}
    this.onConnected = null;        // () => {}
    this.onDisconnected = null;     // () => {}
    this.onDataReceived = null;     // (data: ble_time_status_t) => {}
    this.onError = null;            // (errMsg) => {}

    // 写入队列
    this._writeQueue = [];
    this._writing = false;

    // 绑定连接状态监听
    this._bindConnectionListener();
  }

  /**
   * 监听 BLE 连接状态变化（断连自动通知）
   */
  _bindConnectionListener() {
    wx.onBLEConnectionStateChange((res) => {
      console.log(`[BLE] 连接状态变化: deviceId=${res.deviceId}, connected=${res.connected}`);
      if (!res.connected && this.deviceId === res.deviceId) {
        this.connected = false;
        this.serviceId = '';
        this.writeCharId = '';
        this.notifyCharId = '';
        if (this.onDisconnected) this.onDisconnected();
      }
    });
  }

  /**
   * 初始化蓝牙适配器
   */
  initAdapter() {
    return new Promise((resolve, reject) => {
      wx.openBluetoothAdapter({
        mode: 'central',
        success: (res) => {
          console.log('[BLE] 适配器已开启');
          resolve(res);
        },
        fail: (err) => {
          console.error('[BLE] 适配器开启失败:', err);
          let msg = '蓝牙初始化失败';
          if (err.errCode === 10001) {
            msg = '请先开启手机蓝牙';
          }
          if (this.onError) this.onError(msg);
          reject(err);
        }
      });
    });
  }

  /**
   * 开始扫描 BLE 设备
   * 优先通过 Service UUID 过滤广播，直接锁定目标 ESP32 节点
   */
  startDiscovery() {
    return new Promise((resolve, reject) => {
      this.discovering = true;
      this._fallbackTriggered = false;

      // 监听发现的设备
      wx.onBluetoothDeviceFound((res) => {
        res.devices.forEach((device) => {
          // 通过 UUID 过滤后，设备名可能为空（某些安卓手机）
          // 只要被 Service UUID 匹配出来的就是目标设备
          const name = device.name || device.localName || '';
          const isTarget = name.indexOf(DEVICE_NAME_PREFIX) >= 0;
          const hasServiceUUID = device.advertisServiceUUIDs &&
            device.advertisServiceUUIDs.some(uuid =>
              uuid.toUpperCase() === SERVICE_UUID.toUpperCase()
            );

          if (isTarget || hasServiceUUID) {
            const displayName = name || 'ESP32-TimeSync';
            console.log('[BLE] 发现目标设备:', displayName, device.deviceId, 'RSSI:', device.RSSI);
            if (this.onDeviceFound) {
              this.onDeviceFound({
                deviceId: device.deviceId,
                name: displayName,
                RSSI: device.RSSI,
              });
            }
          }
        });
      });

      // 第一轮：通过 Service UUID 过滤扫描，精确锁定广播节点
      wx.startBluetoothDevicesDiscovery({
        services: [SERVICE_UUID],
        allowDuplicatesKey: false,
        powerLevel: 'high',
        success: (res) => {
          console.log('[BLE] 开始扫描 (UUID 过滤模式)');

          // 3 秒后如果没找到设备，降级为全扫描
          this._fallbackTimer = setTimeout(() => {
            if (this.discovering && !this._fallbackTriggered) {
              this._fallbackTriggered = true;
              console.log('[BLE] UUID 过滤未发现设备，降级为全扫描');
              wx.stopBluetoothDevicesDiscovery({
                complete: () => {
                  wx.startBluetoothDevicesDiscovery({
                    allowDuplicatesKey: false,
                    powerLevel: 'high',
                    success: () => console.log('[BLE] 全扫描已启动'),
                    fail: (err) => console.warn('[BLE] 全扫描启动失败:', err)
                  });
                }
              });
            }
          }, 3000);

          resolve(res);
        },
        fail: (err) => {
          console.error('[BLE] UUID 过滤扫描失败，尝试全扫描:', err);
          // UUID 过滤不支持时直接全扫描
          wx.startBluetoothDevicesDiscovery({
            allowDuplicatesKey: false,
            powerLevel: 'high',
            success: (res) => {
              console.log('[BLE] 全扫描已启动 (降级)');
              resolve(res);
            },
            fail: (err2) => {
              console.error('[BLE] 全扫描也失败:', err2);
              this.discovering = false;
              reject(err2);
            }
          });
        }
      });
    });
  }

  /**
   * 停止扫描
   */
  stopDiscovery() {
    return new Promise((resolve) => {
      this.discovering = false;
      if (this._fallbackTimer) {
        clearTimeout(this._fallbackTimer);
        this._fallbackTimer = null;
      }
      wx.stopBluetoothDevicesDiscovery({
        success: () => console.log('[BLE] 停止扫描'),
        complete: resolve
      });
    });
  }

  /**
   * 连接指定设备
   */
  async connect(deviceId) {
    this.deviceId = deviceId;
    console.log('[BLE] 正在连接:', deviceId);

    // 停止扫描
    await this.stopDiscovery();

    // 创建连接
    await new Promise((resolve, reject) => {
      wx.createBLEConnection({
        deviceId: deviceId,
        timeout: 10000,
        success: (res) => {
          console.log('[BLE] 连接成功');
          this.connected = true;
          resolve(res);
        },
        fail: (err) => {
          console.error('[BLE] 连接失败:', err);
          if (this.onError) this.onError('连接设备失败: ' + (err.errMsg || ''));
          reject(err);
        }
      });
    });

    // 延迟以确保连接稳定
    await this._delay(500);

    // 尝试协商 MTU
    try {
      await this._setBLEMTU(deviceId, 100);
    } catch (e) {
      console.warn('[BLE] MTU 协商失败，使用默认值:', e);
    }

    // 发现服务和特征
    await this._discoverServices(deviceId);

    // 开启通知
    if (this.notifyCharId) {
      await this._enableNotify(deviceId, this.serviceId, this.notifyCharId);
    }

    if (this.onConnected) this.onConnected();
    return true;
  }

  /**
   * 断开连接
   */
  disconnect() {
    return new Promise((resolve) => {
      if (!this.deviceId) {
        resolve();
        return;
      }
      wx.closeBLEConnection({
        deviceId: this.deviceId,
        success: () => {
          console.log('[BLE] 已断开连接');
          this.connected = false;
        },
        complete: resolve
      });
    });
  }

  /**
   * 发送时间同步数据到 ESP32
   * 格式匹配 esp32master.cpp: 8字节 sec (uint64) + 4字节 usec (uint32)
   * 
   * @param {number} timestampMs - 毫秒级时间戳 (Date.now())
   * @returns {number} 发送时的精确时间戳 (T1)
   */
  sendTimeSync(timestampMs) {
    if (!this.connected || !this.writeCharId) {
      console.warn('[BLE] 未连接或写入特征不可用');
      return null;
    }

    // 将毫秒时间戳转为 秒(uint64) + 微秒(uint32) 格式
    const sec = Math.floor(timestampMs / 1000);
    const usec = (timestampMs % 1000) * 1000;

    // 构建 12 字节的 ArrayBuffer: 8B sec + 4B usec
    const buffer = new ArrayBuffer(12);
    const view = new DataView(buffer);

    // 写入 sec 为 uint64 (小端序，匹配 ESP32 的 memcpy)
    // JavaScript 没有原生 uint64，用两个 uint32 表示
    const secLow = sec & 0xFFFFFFFF;
    const secHigh = Math.floor(sec / 0x100000000) & 0xFFFFFFFF;
    view.setUint32(0, secLow, true);     // 低32位
    view.setUint32(4, secHigh, true);    // 高32位

    // 写入 usec 为 uint32
    view.setUint32(8, usec, true);

    // 记录发送时刻 T1
    const t1 = Date.now();

    // 加入写入队列
    this._enqueueWrite(buffer);

    return t1;
  }

  /**
   * 关闭蓝牙适配器
   */
  closeAdapter() {
    return new Promise((resolve) => {
      wx.closeBluetoothAdapter({
        complete: resolve
      });
    });
  }

  // ==================== 内部方法 ====================

  /**
   * 设置 MTU
   */
  _setBLEMTU(deviceId, mtu) {
    return new Promise((resolve, reject) => {
      wx.setBLEMTU({
        deviceId: deviceId,
        mtu: mtu,
        success: (res) => {
          console.log('[BLE] MTU 设置成功:', res);
          resolve(res);
        },
        fail: (err) => {
          reject(err);
        }
      });
    });
  }

  /**
   * 发现服务和特征
   */
  async _discoverServices(deviceId) {
    // 获取服务列表
    const services = await new Promise((resolve, reject) => {
      wx.getBLEDeviceServices({
        deviceId: deviceId,
        success: (res) => {
          console.log('[BLE] 发现服务:', res.services.map(s => s.uuid));
          resolve(res.services);
        },
        fail: reject
      });
    });

    // 查找目标服务（UUID 比较时忽略大小写）
    const targetService = services.find(s =>
      s.uuid.toUpperCase() === SERVICE_UUID.toUpperCase()
    );

    if (!targetService) {
      const err = '未找到目标 BLE 服务: ' + SERVICE_UUID;
      console.error('[BLE]', err);
      if (this.onError) this.onError(err);
      throw new Error(err);
    }

    this.serviceId = targetService.uuid;
    console.log('[BLE] 找到服务:', this.serviceId);

    // 获取特征列表
    const characteristics = await new Promise((resolve, reject) => {
      wx.getBLEDeviceCharacteristics({
        deviceId: deviceId,
        serviceId: this.serviceId,
        success: (res) => {
          console.log('[BLE] 发现特征:', res.characteristics.map(c => c.uuid));
          resolve(res.characteristics);
        },
        fail: reject
      });
    });

    // 匹配写入和通知特征
    for (const char of characteristics) {
      const uuid = char.uuid.toUpperCase();
      if (uuid === TIME_SYNC_CHAR_UUID.toUpperCase()) {
        this.writeCharId = char.uuid;
        console.log('[BLE] 写入特征:', this.writeCharId);
      }
      if (uuid === TIME_STATUS_CHAR_UUID.toUpperCase()) {
        this.notifyCharId = char.uuid;
        console.log('[BLE] 通知特征:', this.notifyCharId);
      }
    }

    if (!this.writeCharId) {
      const err = '未找到写入特征';
      if (this.onError) this.onError(err);
      throw new Error(err);
    }
  }

  /**
   * 开启特征通知
   */
  async _enableNotify(deviceId, serviceId, characteristicId) {
    // 先注册监听
    wx.onBLECharacteristicValueChange((res) => {
      if (res.characteristicId.toUpperCase() === TIME_STATUS_CHAR_UUID.toUpperCase()) {
        const data = this._parseStatusResponse(res.value);
        if (data && this.onDataReceived) {
          this.onDataReceived(data);
        }
      }
    });

    // 开启通知
    await new Promise((resolve, reject) => {
      wx.notifyBLECharacteristicValueChange({
        deviceId: deviceId,
        serviceId: serviceId,
        characteristicId: characteristicId,
        state: true,
        success: (res) => {
          console.log('[BLE] 通知已开启');
          resolve(res);
        },
        fail: (err) => {
          console.warn('[BLE] 开启通知失败:', err);
          reject(err);
        }
      });
    });
  }

  /**
   * 解析 ESP32 返回的 ble_time_status_t 结构体 (32字节, 小端序)
   * 
   * typedef struct {
   *   uint64_t phone_us;        // 0-7
   *   int64_t  offset_us;       // 8-15
   *   uint64_t esp_receive_us;  // 16-23
   *   uint64_t esp_process_us;  // 24-31
   * } ble_time_status_t;
   */
  _parseStatusResponse(buffer) {
    if (buffer.byteLength < 32) {
      console.warn('[BLE] 状态响应数据长度不足:', buffer.byteLength);
      return null;
    }

    const view = new DataView(buffer);

    // 读取 uint64: 低32位 + 高32位（小端序）
    const readUint64 = (offset) => {
      const low = view.getUint32(offset, true);
      const high = view.getUint32(offset + 4, true);
      return high * 0x100000000 + low;
    };

    // 读取 int64
    const readInt64 = (offset) => {
      const low = view.getUint32(offset, true);
      const high = view.getInt32(offset + 4, true);  // 注意：有符号
      return high * 0x100000000 + low;
    };

    return {
      phone_us: readUint64(0),
      offset_us: readInt64(8),
      esp_receive_us: readUint64(16),
      esp_process_us: readUint64(24),
      // 记录收到响应的时间
      receivedAt: Date.now()
    };
  }

  /**
   * 写入队列管理 — 确保串行写入
   */
  _enqueueWrite(buffer) {
    this._writeQueue.push(buffer);
    if (!this._writing) {
      this._processWriteQueue();
    }
  }

  async _processWriteQueue() {
    if (this._writeQueue.length === 0) {
      this._writing = false;
      return;
    }

    this._writing = true;
    const buffer = this._writeQueue.shift();

    try {
      await new Promise((resolve, reject) => {
        wx.writeBLECharacteristicValue({
          deviceId: this.deviceId,
          serviceId: this.serviceId,
          characteristicId: this.writeCharId,
          value: buffer,
          writeType: 'write',
          success: resolve,
          fail: reject
        });
      });
    } catch (err) {
      console.error('[BLE] 写入失败:', err);
    }

    // 短延迟后处理下一个
    await this._delay(50);
    this._processWriteQueue();
  }

  _delay(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

// 导出单例
const bleManager = new BLEManager();
module.exports = { bleManager, SERVICE_UUID, DEVICE_NAME_PREFIX };

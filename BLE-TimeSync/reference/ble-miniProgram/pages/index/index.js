// pages/index/index.js

const { bleManager } = require('../../utils/ble');
const { timeSyncEngine } = require('../../utils/time-sync');

Page({
  data: {
    // BLE 状态
    bleStatus: 'disconnected',   // disconnected | scanning | connecting | connected
    statusText: '未连接',
    bleConnected: false,
    scanning: false,
    discoveredDevices: [],       // [{deviceId, name, RSSI}]
    connectedDeviceName: '',

    // 同步控制
    syncRunning: false,
    syncModeText: '已停止',
    currentTime: '--:--:--',
    lastSyncTime: '',
    intervalIndex: 2,           // 默认选中 10 秒
    intervalOptions: [
      { label: '3 秒', value: 3000 },
      { label: '5 秒', value: 5000 },
      { label: '10 秒', value: 10000 },
      { label: '30 秒', value: 30000 },
      { label: '60 秒', value: 60000 },
    ],

    // 精度指标显示
    lastRttDisplay: '--',
    lastRttClass: '',
    lastDelayDisplay: '--',
    lastOffsetDisplay: '--',
    accuracyDisplay: '--',
    rttMinDisplay: '--',
    rttMeanDisplay: '--',
    rttMaxDisplay: '--',
    rttStdDisplay: '--',
    successRateDisplay: '--',
    accuracyReport: null,

    // 统计
    stats: {
      totalSyncs: 0,
      successSyncs: 0,
      failSyncs: 0,
    },

    // 日志
    logs: [],
    logScrollId: '',
  },

  _logIdCounter: 0,
  _clockTimer: null,

  onLoad() {
    // 注册 BLE 回调
    bleManager.onDeviceFound = (device) => this._onDeviceFound(device);
    bleManager.onConnected = () => this._onBleConnected();
    bleManager.onDisconnected = () => this._onBleDisconnected();
    bleManager.onError = (msg) => this._showError(msg);

    // 注册时间同步回调
    timeSyncEngine.onSyncResult = (result) => this._onSyncResult(result);
    timeSyncEngine.onStatsUpdate = (stats) => this._onStatsUpdate(stats);
    timeSyncEngine.onModeChange = (mode) => {
      let txt = '已停止';
      if (mode === 'CALIBRATING') txt = '校准中...';
      else if (mode === 'SYNCING') txt = '运行中';
      this.setData({ syncModeText: txt });
    };

    // 启动时钟更新
    this._startClock();
  },

  onUnload() {
    this._stopClock();
    timeSyncEngine.stop();
    bleManager.disconnect();
    bleManager.closeAdapter();
  },

  onShow() {
    this._startClock();
  },

  onHide() {
    // 不停止同步，只停止时钟显示更新
    this._stopClock();
  },

  // ==================== 时钟 ====================
  _startClock() {
    this._stopClock();
    this._updateClock();
    this._clockTimer = setInterval(() => this._updateClock(), 500);
  },

  _stopClock() {
    if (this._clockTimer) {
      clearInterval(this._clockTimer);
      this._clockTimer = null;
    }
  },

  _updateClock() {
    const now = new Date();
    const h = String(now.getHours()).padStart(2, '0');
    const m = String(now.getMinutes()).padStart(2, '0');
    const s = String(now.getSeconds()).padStart(2, '0');
    const ms = String(now.getMilliseconds()).padStart(3, '0');
    this.setData({ currentTime: `${h}:${m}:${s}.${ms}` });
  },

  // ==================== BLE 事件 ====================

  /**
   * 搜索设备
   */
  async onScanTap() {
    this.setData({
      scanning: true,
      bleStatus: 'scanning',
      statusText: '正在搜索...',
      discoveredDevices: [],
    });

    try {
      await bleManager.initAdapter();
      await bleManager.startDiscovery();

      // 10 秒后自动停止扫描
      setTimeout(() => {
        if (this.data.scanning) {
          bleManager.stopDiscovery();
          if (this.data.discoveredDevices.length === 0) {
            this.setData({
              scanning: false,
              bleStatus: 'disconnected',
              statusText: '未找到设备 (超时)'
            });
            this._diagnoseScanFailure();
          } else {
            this.setData({
              scanning: false,
              bleStatus: 'disconnected',
              statusText: `找到 ${this.data.discoveredDevices.length} 个设备`
            });
          }
        }
      }, 10000);
    } catch (err) {
      this.setData({
        scanning: false,
        bleStatus: 'disconnected',
        statusText: '搜索失败',
      });
      let errorDetail = err.errMsg || '';
      if (err.errCode === 10001) {
        errorDetail = '请先开启手机蓝牙';
      }
      this._showError('搜索设备失败: ' + errorDetail);
    }
  },

  /**
   * 现场环境 Debug 诊断函数
   */
  _diagnoseScanFailure() {
    let reasons = [];
    
    // 1. 系统设置检查 (蓝牙开关、GPS定位)
    try {
      const sysSetting = wx.getSystemSetting();
      if (sysSetting.bluetoothEnabled === false) {
        reasons.push('【错误】手机系统“蓝牙开关”未开启');
      }
      if (sysSetting.locationEnabled === false) {
        reasons.push('【错误】手机系统“定位(GPS)”未开启 (安卓系统限制: 扫描低功耗蓝牙必须开启定位)');
      }
    } catch (e) {
      console.warn('无法获取系统设置', e);
    }

    // 2. 微信系统权限检查
    try {
      const authSetting = wx.getAppAuthorizeSetting();
      if (authSetting.bluetoothAuthorized !== 'authorized') {
        reasons.push('【错误】微信未被授予系统“蓝牙”权限');
      }
      if (authSetting.locationAuthorized !== 'authorized') {
        reasons.push('【错误】微信未被授予系统“精确定位”权限');
      }
    } catch (e) {
      console.warn('无法获取授权设置', e);
    }

    // 3. 结果汇总呈现
    if (reasons.length > 0) {
      wx.showModal({
        title: '现场 Debug: 发现环境问题',
        content: '扫描失败，检测到以下确切问题：\n\n' + reasons.map(r => '❌ ' + r).join('\n') + '\n\n请退出小程序，前往手机“设置”中修复上述问题后重试。',
        showCancel: false,
        confirmText: '去修复'
      });
    } else {
      // 所有的硬性条件都满足，说明是纯粹没有收到信号
      wx.showModal({
        title: '现场 Debug: 环境正常，信号异常',
        content: '✅ 手机环境：蓝牙开关、定位、权限均正常！\n\n❌ 仍未收到 ESP32 的广播信号。原因推断：\n1. 【最可能】ESP32 已在“设置->蓝牙”中被手机系统配对！系统霸占了通道，导致微信搜不到。请立刻前往系统设置取消配对！\n2. ESP32 未开机，或者串口未打印出“BLE 启动”日志\n3. 距离太远被严重干扰',
        showCancel: false,
        confirmText: '我去检查'
      });
    }
  },

  /**
   * 发现设备回调
   */
  _onDeviceFound(device) {
    const devices = this.data.discoveredDevices;
    // 去重
    const exists = devices.findIndex(d => d.deviceId === device.deviceId);
    if (exists >= 0) {
      devices[exists] = device;
    } else {
      devices.push(device);
    }
    this.setData({
      discoveredDevices: devices,
      statusText: `找到 ${devices.length} 个设备`,
    });
  },

  /**
   * 点击设备 → 连接
   */
  async onDeviceTap(e) {
    const device = e.currentTarget.dataset.device;
    this.setData({
      bleStatus: 'connecting',
      statusText: '正在连接...',
      scanning: false,
    });

    try {
      await bleManager.connect(device.deviceId);
    } catch (err) {
      this.setData({
        bleStatus: 'disconnected',
        statusText: '连接失败',
      });
    }
  },

  /**
   * BLE 连接成功
   */
  _onBleConnected() {
    this.setData({
      bleStatus: 'connected',
      statusText: '已连接',
      bleConnected: true,
      connectedDeviceName: bleManager.deviceId ? 'ESP32S3-TimeSync' : '',
      discoveredDevices: [],
    });
    wx.showToast({ title: '连接成功', icon: 'success' });
  },

  /**
   * BLE 断开连接
   */
  _onBleDisconnected() {
    // 停止同步
    if (this.data.syncRunning) {
      timeSyncEngine.stop();
    }

    this.setData({
      bleStatus: 'disconnected',
      statusText: '连接已断开',
      bleConnected: false,
      syncRunning: false,
      connectedDeviceName: '',
    });
    wx.showToast({ title: '连接已断开', icon: 'none' });
  },

  /**
   * 主动断开
   */
  async onDisconnectTap() {
    timeSyncEngine.stop();
    await bleManager.disconnect();
    this.setData({
      bleStatus: 'disconnected',
      statusText: '未连接',
      bleConnected: false,
      syncRunning: false,
      connectedDeviceName: '',
    });
  },

  /**
   * 模拟连接 (测试用)
   */
  onMockConnectTap() {
    this.setData({
      bleStatus: 'connected',
      statusText: '已连接 (模拟模式)',
      bleConnected: true,
      connectedDeviceName: 'Mock-ESP32',
      discoveredDevices: [],
    });
    
    // 拦截 bleManager 的发送方法
    bleManager.connected = true;
    bleManager._originalSend = bleManager.sendTimeSync; // 备份
    bleManager._originalDisconnect = bleManager.disconnect;

    bleManager.sendTimeSync = (timestampMs) => {
      const t1 = Date.now();
      
      // 模拟 10~50ms 的网络往返延迟
      const mockDelay = 10 + Math.random() * 40;
      
      setTimeout(() => {
        const offset = 1500000; // 模拟 1.5s 的时间差
        const espProcess = 2000; // 模拟 2ms 处理时间
        
        const data = {
          phone_us: timestampMs * 1000,
          offset_us: offset,
          esp_receive_us: 1000000,
          esp_process_us: 1000000 + espProcess,
          receivedAt: Date.now()
        };
        if (bleManager.onDataReceived) {
          bleManager.onDataReceived(data);
        }
      }, mockDelay);
      
      return t1;
    };
    
    // 拦截断开连接
    bleManager.disconnect = () => {
      bleManager.connected = false;
      // 恢复原方法
      if (bleManager._originalSend) bleManager.sendTimeSync = bleManager._originalSend;
      if (bleManager._originalDisconnect) bleManager.disconnect = bleManager._originalDisconnect;
      return Promise.resolve();
    };

    wx.showToast({ title: '已进入模拟模式', icon: 'none' });
  },

  // ==================== 同步控制 ====================

  /**
   * 开始/暂停同步
   */
  onSyncToggle() {
    if (this.data.syncRunning) {
      timeSyncEngine.stop();
      this.setData({ syncRunning: false });
    } else {
      const interval = this.data.intervalOptions[this.data.intervalIndex].value;
      timeSyncEngine.start(interval);
      this.setData({ syncRunning: true });
    }
  },

  /**
   * 单次同步
   */
  onSyncOnce() {
    timeSyncEngine.syncOnce();
  },

  /**
   * 同步间隔变更
   */
  onIntervalChange(e) {
    const idx = parseInt(e.detail.value);
    this.setData({ intervalIndex: idx });

    // 如果正在运行，重启以应用新间隔
    if (this.data.syncRunning) {
      timeSyncEngine.stop();
      const interval = this.data.intervalOptions[idx].value;
      timeSyncEngine.start(interval);
    }
  },

  // ==================== 同步结果 ====================

  /**
   * 收到同步结果
   */
  _onSyncResult(result) {
    const logId = ++this._logIdCounter;

    // 构建日志条目
    const logEntry = {
      id: logId,
      time: result.time,
      success: result.success,
      error: result.error || '',
      totalRttMs: result.totalRttMs ? result.totalRttMs.toFixed(1) : '',
      oneWayDelayMs: result.oneWayDelayMs ? result.oneWayDelayMs.toFixed(1) : '',
      offsetMs: result.offsetMs ? result.offsetMs.toFixed(2) : '',
    };

    const logs = [...this.data.logs, logEntry];
    // 保留最近 50 条
    if (logs.length > 50) logs.shift();

    const updateData = {
      logs: logs,
      logScrollId: 'log-' + logId,
    };

    if (result.success) {
      updateData.lastSyncTime = result.time;

      // RTT 颜色分级
      let rttClass = 'rtt-good';
      if (result.totalRttMs > 100) rttClass = 'rtt-bad';
      else if (result.totalRttMs > 50) rttClass = 'rtt-ok';

      updateData.lastRttDisplay = result.totalRttMs.toFixed(1);
      updateData.lastRttClass = rttClass;
      updateData.lastDelayDisplay = result.oneWayDelayMs.toFixed(1);
      updateData.lastOffsetDisplay = result.offsetUs.toFixed(0);
      updateData.accuracyDisplay = '±' + result.oneWayDelayMs.toFixed(1);
    }

    this.setData(updateData);
  },

  /**
   * 统计更新
   */
  _onStatsUpdate(stats) {
    const n = stats.successSyncs;
    const updateData = {
      stats: {
        totalSyncs: stats.totalSyncs,
        successSyncs: stats.successSyncs,
        failSyncs: stats.failSyncs,
      },
    };

    if (n > 0) {
      const rttMean = stats.rttSum / n;
      const rttVariance = (stats.rttSumSq / n) - (rttMean * rttMean);
      const rttStdDev = Math.sqrt(Math.max(0, rttVariance));

      updateData.rttMinDisplay = stats.rttMin.toFixed(1);
      updateData.rttMeanDisplay = rttMean.toFixed(1);
      updateData.rttMaxDisplay = stats.rttMax.toFixed(1);
      updateData.rttStdDisplay = rttStdDev.toFixed(1);
      updateData.successRateDisplay = (stats.successSyncs / stats.totalSyncs * 100).toFixed(1) + '%';

      // 获取精度报告
      const report = timeSyncEngine.getAccuracyReport();
      if (report) {
        updateData.accuracyReport = {
          sampleCount: report.sampleCount,
          bestDisplay: report.estimatedAccuracyBestMs.toFixed(1),
          typicalDisplay: report.estimatedAccuracyTypicalMs.toFixed(1),
          worstDisplay: report.estimatedAccuracyWorstMs.toFixed(1),
          offsetDriftDisplay: report.offsetDriftMs.toFixed(2),
          offsetDriftMs: report.offsetDriftMs
        };
      }
    }

    this.setData(updateData);
  },

  // ==================== 操作 ====================

  onResetStats() {
    wx.showModal({
      title: '确认',
      content: '确定要重置所有统计数据吗？',
      success: (res) => {
        if (res.confirm) {
          timeSyncEngine.resetStats();
          this.setData({
            lastRttDisplay: '--',
            lastRttClass: '',
            lastDelayDisplay: '--',
            lastOffsetDisplay: '--',
            accuracyDisplay: '--',
            rttMinDisplay: '--',
            rttMeanDisplay: '--',
            rttMaxDisplay: '--',
            rttStdDisplay: '--',
            successRateDisplay: '--',
            accuracyReport: null,
          });
        }
      }
    });
  },

  onClearLogs() {
    this.setData({ logs: [], logScrollId: '' });
  },

  // ==================== 工具 ====================

  _showError(msg) {
    wx.showToast({
      title: msg,
      icon: 'none',
      duration: 3000,
    });
  },
});

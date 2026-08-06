/**
 * 时间同步引擎
 * 
 * 实现基于 BLE 的时间同步协议，与 esp32master.cpp 配合工作。
 * 
 * 同步流程：
 * 1. 小程序记录 T1 = Date.now()，计算预补偿时间 sentTime = T1 + compensationMs，发送给 ESP32
 * 2. ESP32 收到后记录 esp_receive_us(T2)，处理后记录 esp_process_us(T3)
 * 3. ESP32 通过 notify 返回 {phone_us, offset_us, esp_receive_us, esp_process_us}
 * 4. 小程序收到 notify 记录 T4 = Date.now()
 * 5. 计算 RTT 和单向延迟
 * 
 * 精度测量设计：
 * - 引入“校准模式”(CALIBRATING)：快速发送 N 次不带补偿的包，测算平均单向延迟。
 * - 进入“同步模式”(SYNCING)：使用校准出的平均延迟进行预补偿。
 */

const { bleManager } = require('./ble');

class TimeSyncEngine {
  constructor() {
    // 引擎状态
    this.running = false;
    this.timerId = null;
    this.syncIntervalMs = 10000;  // 默认 10 秒
    
    // 运行模式: 'IDLE' | 'CALIBRATING' | 'SYNCING'
    this.mode = 'IDLE'; 
    this.maxCalibration = 5;      // 校准次数
    this.calibrationCount = 0;    // 当前已校准次数
    this.currentCompensationMs = 0; // 当前使用的预补偿值

    // 当前同步会话
    this._pendingT1 = null;       // 发送请求的本地时刻 (ms)
    this._pendingSentMs = null;   // 实际发给 ESP32 的时间戳 (ms)
    this._syncTimeout = null;

    // 同步结果历史
    this.history = [];            // 最近 N 次同步结果
    this.maxHistory = 200;

    // 精度统计
    this.stats = {
      totalSyncs: 0,
      successSyncs: 0,
      failSyncs: 0,
      rttMin: Infinity,
      rttMax: 0,
      rttSum: 0,
      rttSumSq: 0,
      lastRtt: null,
      lastOneWayDelay: null,
      lastOffset: null,
      lastSyncTime: null,
      estimatedAccuracyMs: null,
    };

    // 回调
    this.onSyncResult = null;     // (result) => {}
    this.onStatsUpdate = null;    // (stats) => {}
    this.onModeChange = null;     // (mode) => {}
    this.onError = null;          // (errMsg) => {}

    // 注册 BLE 数据接收回调
    bleManager.onDataReceived = (data) => this._onEspResponse(data);
  }

  /**
   * 开始同步（含校准流程）
   * @param {number} intervalMs - 同步间隔（毫秒），默认 10000
   */
  start(intervalMs) {
    if (this.running) return;

    this.syncIntervalMs = intervalMs || this.syncIntervalMs;
    this.running = true;
    
    // 进入校准模式
    this._setMode('CALIBRATING');
    this.calibrationCount = 0;
    this.currentCompensationMs = 0;
    this.resetStats();

    console.log(`[TimeSync] 进入校准模式，准备发送 ${this.maxCalibration} 次测试`);
    this._doSync();
  }

  /**
   * 停止同步
   */
  stop() {
    this.running = false;
    this._setMode('IDLE');
    if (this.timerId) {
      clearTimeout(this.timerId);
      this.timerId = null;
    }
    if (this._syncTimeout) {
      clearTimeout(this._syncTimeout);
      this._syncTimeout = null;
    }
    this._pendingT1 = null;
    console.log('[TimeSync] 已停止同步');
  }

  /**
   * 手动触发一次同步
   */
  syncOnce() {
    this._doSync();
  }

  /**
   * 重置统计数据
   */
  resetStats() {
    this.history = [];
    this.stats = {
      totalSyncs: 0,
      successSyncs: 0,
      failSyncs: 0,
      rttMin: Infinity,
      rttMax: 0,
      rttSum: 0,
      rttSumSq: 0,
      lastRtt: null,
      lastOneWayDelay: null,
      lastOffset: null,
      lastSyncTime: null,
      estimatedAccuracyMs: null,
    };
    if (this.onStatsUpdate) this.onStatsUpdate(this.stats);
  }

  /**
   * 获取精度报告
   */
  getAccuracyReport() {
    const s = this.stats;
    const n = s.successSyncs;
    if (n === 0) return null;

    const rttMean = s.rttSum / n;
    const rttVariance = (s.rttSumSq / n) - (rttMean * rttMean);
    const rttStdDev = Math.sqrt(Math.max(0, rttVariance));

    // 计算 offset 漂移（最近10次的标准差）
    const recentOffsets = this.history.slice(-10).map(h => h.offsetUs);
    let offsetDriftUs = 0;
    if (recentOffsets.length >= 2) {
      const offMean = recentOffsets.reduce((a, b) => a + b, 0) / recentOffsets.length;
      const offVar = recentOffsets.reduce((a, b) => a + (b - offMean) ** 2, 0) / recentOffsets.length;
      offsetDriftUs = Math.sqrt(offVar);
    }

    return {
      sampleCount: n,
      rttMeanMs: rttMean,
      rttStdDevMs: rttStdDev,
      rttMinMs: s.rttMin,
      rttMaxMs: s.rttMax,
      estimatedAccuracyBestMs: s.rttMin / 2,
      estimatedAccuracyTypicalMs: rttMean / 2,
      estimatedAccuracyWorstMs: s.rttMax / 2,
      offsetDriftUs: offsetDriftUs,
      offsetDriftMs: offsetDriftUs / 1000,
      successRate: (s.successSyncs / s.totalSyncs * 100).toFixed(1) + '%',
    };
  }

  // ==================== 内部方法 ====================

  _setMode(mode) {
    this.mode = mode;
    if (this.onModeChange) this.onModeChange(mode);
  }

  /**
   * 执行一次同步
   */
  _doSync() {
    if (!bleManager.connected) {
      console.warn('[TimeSync] BLE 未连接，跳过同步');
      return;
    }

    if (this._pendingT1 !== null) {
      console.warn('[TimeSync] 上一次同步尚未完成，跳过');
      return;
    }

    this.stats.totalSyncs++;

    // T1: 本地发起时刻
    const nowMs = Date.now();
    this._pendingT1 = nowMs;
    
    // 加入预补偿：如果要发 12:00:00，但已知路上要花 20ms，那就发 12:00:00.020 过去
    const compensatedTime = nowMs + this.currentCompensationMs;
    this._pendingSentMs = compensatedTime;

    // 发送给 ESP32
    const actualT1 = bleManager.sendTimeSync(compensatedTime);
    if (actualT1) {
      this._pendingT1 = actualT1; 
    }

    // 设置超时（3秒）
    this._syncTimeout = setTimeout(() => {
      if (this._pendingT1 !== null) {
        console.warn('[TimeSync] 同步超时');
        this.stats.failSyncs++;
        this._pendingT1 = null;

        const result = {
          success: false,
          error: 'timeout',
          time: new Date().toLocaleTimeString(),
        };
        if (this.onSyncResult) this.onSyncResult(result);
        if (this.onStatsUpdate) this.onStatsUpdate(this.stats);
        
        this._scheduleNext();
      }
    }, 3000);
  }

  /**
   * 收到 ESP32 返回的状态数据
   */
  _onEspResponse(data) {
    const t4 = data.receivedAt;

    if (this._pendingT1 === null) return;

    if (this._syncTimeout) {
      clearTimeout(this._syncTimeout);
      this._syncTimeout = null;
    }

    const t1 = this._pendingT1;
    this._pendingT1 = null;

    const espReceiveUs = data.esp_receive_us;
    const espProcessUs = data.esp_process_us;
    const offsetUs = data.offset_us;
    
    const espProcessingUs = espProcessUs - espReceiveUs;
    const espProcessingMs = espProcessingUs / 1000;

    // 计算总 RTT 和单向延迟
    const totalRttMs = t4 - t1;
    const netRttMs = totalRttMs - espProcessingMs;
    const oneWayDelayMs = netRttMs / 2;

    // 更新统计
    this.stats.successSyncs++;
    this.stats.lastRtt = totalRttMs;
    this.stats.lastOneWayDelay = oneWayDelayMs;
    this.stats.lastOffset = offsetUs;
    this.stats.lastSyncTime = new Date().toLocaleTimeString();
    this.stats.estimatedAccuracyMs = oneWayDelayMs;

    if (totalRttMs < this.stats.rttMin) this.stats.rttMin = totalRttMs;
    if (totalRttMs > this.stats.rttMax) this.stats.rttMax = totalRttMs;
    this.stats.rttSum += totalRttMs;
    this.stats.rttSumSq += totalRttMs * totalRttMs;

    const record = {
      time: new Date().toLocaleTimeString(),
      timestamp: Date.now(),
      totalRttMs: totalRttMs,
      netRttMs: netRttMs,
      oneWayDelayMs: oneWayDelayMs,
      offsetUs: offsetUs,
      offsetMs: offsetUs / 1000,
      compensationMs: this.currentCompensationMs,
      success: true,
    };

    this.history.push(record);
    if (this.history.length > this.maxHistory) this.history.shift();

    if (this.onSyncResult) this.onSyncResult(record);
    if (this.onStatsUpdate) this.onStatsUpdate(this.stats);

    // 状态机流转
    if (this.mode === 'CALIBRATING') {
      this.calibrationCount++;
      if (this.calibrationCount >= this.maxCalibration) {
        // 校准结束，计算历史平均延迟作为固定补偿值
        this.currentCompensationMs = (this.stats.rttSum / this.stats.successSyncs) / 2;
        console.log(`[TimeSync] 校准完成，应用预补偿: +${this.currentCompensationMs.toFixed(2)} ms`);
        this._setMode('SYNCING');
      }
    } else if (this.mode === 'SYNCING') {
      // 可以在这里做动态移动平均更新，这里保持固定补偿
    }

    this._scheduleNext();
  }

  _scheduleNext() {
    if (!this.running) return;
    
    if (this.timerId) clearTimeout(this.timerId);

    // 校准模式 500ms 一次，同步模式按用户设定的 interval
    const delay = (this.mode === 'CALIBRATING') ? 500 : this.syncIntervalMs;
    this.timerId = setTimeout(() => {
      this._doSync();
    }, delay);
  }
}

const timeSyncEngine = new TimeSyncEngine();
module.exports = { timeSyncEngine };

// app.js — 小程序入口
App({
  globalData: {
    // BLE 连接状态
    bleConnected: false,
    deviceId: '',
    // 时间同步状态
    timeSynced: false,
    syncRunning: false,
  },

  onLaunch() {
    console.log('[App] 数采系统授时小程序启动');
  },

  onShow() {
    console.log('[App] 小程序进入前台');
  },

  onHide() {
    console.log('[App] 小程序进入后台');
  }
});

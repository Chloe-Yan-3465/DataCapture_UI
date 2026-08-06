const phaseLabels = {
  idle: "准备就绪",
  binding_trackers: "绑定 Tracker 角色中",
  stopping_binding: "正在取消角色绑定",
  starting_ble: "BLE 启动中",
  ble_ready: "BLE 持续授时中",
  stopping_ble: "BLE 停止中",
  starting_tracker: "Tracker 启动中",
  recording: "正在采集",
  stopping_capture: "正在停止采集",
  error: "采集异常",
};

const processLabels = {
  stopped: "未运行",
  starting: "启动中",
  running: "运行中",
  exited: "已退出",
  error: "异常退出",
};

let lastSeq = { ble: 0, vive: 0 };
let pollBusy = false;

const byId = (id) => document.getElementById(id);
const bleButton = byId("ble-button");
const startButton = byId("start-button");
const bindButton = byId("bind-button");
const stopButton = byId("stop-button");
const rateInput = byId("tracker-rate");
const notice = byId("notice");
let previousPhase = null;

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function appendLogs(key, items) {
  const terminal = byId(`${key}-log`);
  const nearBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 80;
  for (const item of items) {
    const line = document.createElement("span");
    line.className = `log-line ${item.stream === "system" ? "log-system" : ""}`;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = `[${item.time}] `;
    line.append(time, document.createTextNode(item.text));
    terminal.appendChild(line);
  }
  while (terminal.children.length > 2500) terminal.firstChild.remove();
  if (nearBottom) terminal.scrollTop = terminal.scrollHeight;
}

function renderProcess(key, process) {
  const pill = byId(`${key}-status`);
  pill.textContent = process.pid ? `${processLabels[process.status]} · PID ${process.pid}` : processLabels[process.status];
  pill.className = `pill ${process.status === "running" ? "running" : process.status === "error" ? "error" : "stopped"}`;
}

function gatewayStateClass(state) {
  if (state === "IDLE") return "idle";
  if (["WAIT_START_ACK", "WAIT_STOP_ACK"].includes(state)) return "waiting";
  if (state === "RUNNING") return "running";
  if (state === "ERROR") return "error";
  if (state.includes("OTA")) return "ota";
  return "unknown";
}

function renderGateways(gateways) {
  const list = byId("gateway-list");
  if (!gateways.length) {
    const empty = document.createElement("span");
    empty.className = "gateway-empty";
    empty.textContent = "暂无已连接设备";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...gateways.map((gateway) => {
    const chip = document.createElement("span");
    chip.className = "gateway-chip";
    chip.title = `网关 ${gateway.device_id} · ${gateway.state} · 更新于 ${gateway.updated_at}`;
    const dot = document.createElement("i");
    dot.className = `gateway-dot ${gatewayStateClass(gateway.state)}`;
    chip.append(dot, document.createTextNode(`[${gateway.device_id}] ${gateway.state}`));
    return chip;
  }));
}

function renderState(state) {
  byId("phase-label").textContent = phaseLabels[state.phase] || state.phase;
  byId("phase-message").textContent = state.message;
  const dot = byId("phase-dot");
  const preparingPhases = ["binding_trackers", "stopping_binding", "starting_ble", "stopping_ble", "starting_tracker", "stopping_capture"];
  const dotPhase = preparingPhases.includes(state.phase)
    ? "preparing"
    : state.phase === "ble_ready"
      ? "ready"
      : state.phase;
  dot.className = `status-dot ${dotPhase}`;
  const controls = state.controls;
  const bleRunning = Boolean(state.processes.ble.pid);
  bleButton.textContent = bleRunning ? "停止 BLE 授时" : "启动 BLE 授时";
  bleButton.disabled = bleRunning ? !controls.can_stop_ble : !controls.can_start_ble;
  bindButton.textContent = controls.can_cancel_binding ? "取消角色绑定" : "绑定 Tracker 角色";
  bindButton.disabled = !(controls.can_bind_trackers || controls.can_cancel_binding);
  startButton.disabled = !controls.can_start_capture;
  stopButton.disabled = !controls.can_stop_capture;
  rateInput.disabled = ["starting_tracker", "recording", "stopping_capture"].includes(state.phase);
  if (state.error) showNotice(state.message);
  else if (!notice.dataset.manual) showNotice("");
  renderProcess("ble", state.processes.ble);
  renderProcess("vive", state.processes.vive);
  renderGateways(state.gateways || []);
  appendLogs("ble", state.logs.ble.items);
  appendLogs("vive", state.logs.vive.items);
  lastSeq.ble = state.logs.ble.last_seq;
  lastSeq.vive = state.logs.vive.last_seq;
  byId("output-path").textContent = state.vive_output_dir
    ? `VIVE 输出目录：${state.vive_output_dir}`
    : "VIVE 输出目录：尚未开始";
  if (previousPhase === "binding_trackers" && state.phase !== "binding_trackers") loadPreflight();
  previousPhase = state.phase;
}

async function pollState() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const state = await request(`/api/state?after_ble=${lastSeq.ble}&after_vive=${lastSeq.vive}`);
    try {
      renderState(state);
    } catch (error) {
      console.error(error);
      showNotice(`界面渲染错误：${error.message}`);
    }
  } catch (error) {
    showNotice(`无法连接本地 UI 服务：${error.message}`);
  } finally {
    pollBusy = false;
  }
}

async function loadPreflight() {
  try {
    const data = await request("/api/preflight");
    const container = byId("preflight");
    container.replaceChildren(...data.checks.map((check) => {
      const item = document.createElement("span");
      item.className = `check ${check.ok ? "ok" : "bad"}`;
      item.textContent = check.name;
      item.title = check.detail;
      return item;
    }));
    if (!data.ok) showNotice("启动检查未通过；请把鼠标移到红色项目上查看详情。");
  } catch (error) {
    showNotice(error.message);
  }
}

startButton.addEventListener("click", async () => {
  showNotice("");
  delete notice.dataset.manual;
  try {
    await request("/api/capture/start", {
      method: "POST",
      body: JSON.stringify({ tracker_rate: Number(rateInput.value) }),
    });
    await pollState();
  } catch (error) {
    notice.dataset.manual = "true";
    showNotice(error.message);
  }
});

bindButton.addEventListener("click", async () => {
  showNotice("");
  delete notice.dataset.manual;
  try {
    const path = previousPhase === "binding_trackers" ? "/api/cancel-bind" : "/api/bind-trackers";
    await request(path, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await pollState();
  } catch (error) {
    notice.dataset.manual = "true";
    showNotice(error.message);
  }
});

bleButton.addEventListener("click", async () => {
  showNotice("");
  delete notice.dataset.manual;
  try {
    const path = bleButton.textContent.startsWith("停止") ? "/api/ble/stop" : "/api/ble/start";
    await request(path, { method: "POST", body: "{}" });
    await pollState();
  } catch (error) {
    notice.dataset.manual = "true";
    showNotice(error.message);
  }
});

stopButton.addEventListener("click", async () => {
  showNotice("");
  delete notice.dataset.manual;
  try {
    await request("/api/capture/stop", { method: "POST", body: "{}" });
    await pollState();
  } catch (error) {
    notice.dataset.manual = "true";
    showNotice(error.message);
  }
});

byId("clear-button").addEventListener("click", async () => {
  try {
    await request("/api/clear-logs", { method: "POST", body: "{}" });
    byId("ble-log").replaceChildren();
    byId("vive-log").replaceChildren();
    lastSeq = { ble: 0, vive: 0 };
  } catch (error) {
    showNotice(error.message);
  }
});

function updateClock() {
  byId("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

updateClock();
setInterval(updateClock, 1000);
loadPreflight();
pollState();
setInterval(pollState, 600);

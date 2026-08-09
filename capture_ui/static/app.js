const phaseLabels = {
  idle: "准备就绪",
  binding_trackers: "绑定 Tracker 角色中",
  stopping_binding: "正在取消角色绑定",
  starting_ble: "BLE 启动中",
  ble_ready: "BLE 持续授时中",
  stopping_ble: "BLE 停止中",
  starting_tracker: "Tracker 启动中",
  recording: "正在录制",
  stopping_capture: "正在停止录制",
  error: "录制异常",
};

const processLabels = {
  stopped: "未运行",
  starting: "启动中",
  running: "运行中",
  exited: "已退出",
  error: "异常退出",
};

let lastSeq = { ble_timesync: 0, ble_control: 0, vive: 0 };
let pollBusy = false;

const byId = (id) => document.getElementById(id);
const bleButton = byId("ble-button");
const scanButton = byId("scan-button");
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

function mode2StateClass(state) {
  if (["IDLE", "SYNCED", "LOCKED"].includes(state)) return "idle";
  if (["CONNECTING", "SYNCING", "RETRYING", "STARTING", "STOPPING"].includes(state)) return "waiting";
  if (state === "RUNNING") return "running";
  if (["ERROR", "FAULT"].includes(state)) return "error";
  return "unknown";
}

function renderControlResult(result = "OFFLINE") {
  const states = {
    OFFLINE: ["未连接", "offline"],
    STANDBY: ["待机", "standby"],
    STARTING: ["START 中", "pending"],
    START_OK: ["START 成功", "start-ok"],
    STOPPING: ["STOP 中", "pending"],
    STOP_OK: ["STOP 成功", "stop-ok"],
    ERROR: ["控制失败", "failed"],
  };
  const [label, className] = states[result] || states.OFFLINE;
  const badge = byId("control-result");
  badge.textContent = label;
  badge.className = `pill control-result ${className}`;
}

function renderMode2(mode2 = {}) {
  const name = mode2.name || "Mode2Coordinator";
  const serial = mode2.serial || "未连接";
  byId("coordinator-name").textContent = `${name} · ${serial}`;
  const summary = [
    ["timesync-chip", `授时 ${mode2.time_sync_state || "STOPPED"}`, mode2.time_sync_state],
    ["control-chip", `控制 ${mode2.control_state || "OFFLINE"}`, mode2.control_state],
    ["utc-chip", `UTC ${mode2.utc_map_state || "UNKNOWN"}`, mode2.utc_map_state],
  ];
  for (const [id, label, state] of summary) {
    const chip = byId(id);
    chip.textContent = label;
    chip.className = `mode2-chip ${mode2StateClass(state || "UNKNOWN")}`;
  }
  renderControlResult(mode2.control_result);

  const nodes = mode2.nodes || [];
  const list = byId("node-list");
  if (!nodes.length) {
    const empty = document.createElement("span");
    empty.className = "node-empty";
    empty.textContent = "尚未收到 wearable 节点状态";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...nodes.map((node) => {
    const chip = document.createElement("span");
    const visualState = node.connected ? node.state : "OFFLINE";
    chip.className = "node-chip";
    chip.title = `Node ${node.node_id} · ${node.connected ? "已连接" : "未连接"} · ${node.state} · ${node.details || "无详情"} · 更新于 ${node.updated_at}`;
    const dot = document.createElement("i");
    dot.className = `mode2-dot ${mode2StateClass(visualState)}`;
    chip.append(dot, document.createTextNode(`Node ${node.node_id} · ${node.connected ? node.state : "OFFLINE"}`));
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
  bleButton.textContent = bleRunning ? "停止常驻授时" : "启动常驻授时";
  bleButton.disabled = bleRunning ? !controls.can_stop_ble : !controls.can_start_ble;
  scanButton.disabled = !controls.can_scan_wearables;
  bindButton.textContent = controls.can_cancel_binding ? "取消角色绑定" : "绑定 Tracker 角色";
  bindButton.disabled = !(controls.can_bind_trackers || controls.can_cancel_binding);
  startButton.disabled = !controls.can_start_capture;
  stopButton.disabled = !controls.can_stop_capture;
  rateInput.disabled = ["starting_tracker", "recording", "stopping_capture"].includes(state.phase);
  if (state.error) showNotice(state.message);
  else if (!notice.dataset.manual) showNotice("");
  renderProcess("ble", state.processes.ble);
  renderProcess("vive", state.processes.vive);
  renderMode2(state.mode2);
  appendLogs("ble-timesync", state.logs.ble_timesync.items);
  appendLogs("ble-control", state.logs.ble_control.items);
  appendLogs("vive", state.logs.vive.items);
  lastSeq.ble_timesync = state.logs.ble_timesync.last_seq;
  lastSeq.ble_control = state.logs.ble_control.last_seq;
  lastSeq.vive = state.logs.vive.last_seq;
  byId("output-path").textContent = state.vive_output_dir
    ? `VIVE 输出目录：${state.vive_output_dir}`
    : "VIVE 输出目录：尚未开始";
  previousPhase = state.phase;
}

async function pollState() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const state = await request(`/api/state?after_ble_timesync=${lastSeq.ble_timesync}&after_ble_control=${lastSeq.ble_control}&after_vive=${lastSeq.vive}`);
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

scanButton.addEventListener("click", async () => {
  showNotice("");
  delete notice.dataset.manual;
  try {
    await request("/api/ble/scan", { method: "POST", body: "{}" });
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
    byId("ble-timesync-log").replaceChildren();
    byId("ble-control-log").replaceChildren();
    byId("vive-log").replaceChildren();
    lastSeq = { ble_timesync: 0, ble_control: 0, vive: 0 };
  } catch (error) {
    showNotice(error.message);
  }
});

function updateClock() {
  byId("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

updateClock();
setInterval(updateClock, 1000);
pollState();
setInterval(pollState, 600);

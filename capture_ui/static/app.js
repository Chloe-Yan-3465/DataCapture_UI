const phaseLabels = {
  idle: "准备就绪",
  starting_ble: "授时与数据流启动中",
  ble_ready: "持续授时中",
  stopping_ble: "BLE 停止中",
  starting_streams: "数据流启动中",
  streams_ready: "授时与数据流就绪",
  stopping_streams: "数据流停止中",
  starting_capture: "联合录制启动中",
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

let lastSeq = { ble_timesync: 0, ble_control: 0, manus: 0 };
let pollBusy = false;

const byId = (id) => document.getElementById(id);
const bleButton = byId("ble-button");
const streamsButton = byId("streams-button");
const scanButton = byId("scan-button");
const startButton = byId("start-button");
const stopButton = byId("stop-button");
const notice = byId("notice");
const taskNameSelect = byId("task-name-select");
const complexLevelSelect = byId("complex-level-select");
const customTaskControl = byId("custom-task-control");
const customTaskInput = byId("custom-task-input");
const addTaskButton = byId("add-task-button");
let captureOptionsSignature = "";

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

function renderFrameReport(report = {}) {
  const status = report.status || "idle";
  const session = report.session_id;
  const expected = (report.expected_nodes || []).map(String);
  const returned = new Map((report.nodes || []).map((item) => [String(item.node_id), item]));
  const nodeIds = [...new Set([...expected, ...returned.keys()])].sort((a, b) => Number(a) - Number(b));
  const labels = { "1": "头部", "2": "左手", "3": "右手" };
  const statusLabels = {
    idle: "尚无数据",
    recording: "本轮采集中",
    waiting: "等待 NanoPi 回传",
    receiving: "部分节点已回传",
    complete: `Episode ${session || "-"} · 回传完成`,
  };
  const badge = byId("frame-report-status");
  badge.textContent = statusLabels[status] || status;
  badge.className = `pill frame-report-badge ${status}`;

  const list = byId("frame-report-list");
  if (!nodeIds.length) {
    const empty = document.createElement("span");
    empty.className = "frame-report-empty";
    empty.textContent = status === "recording"
      ? "本轮正在采集，STOP 后显示实际落盘帧数"
      : "STOP 后将在这里显示各节点实际落盘帧数";
    list.replaceChildren(empty);
    return;
  }

  list.replaceChildren(...nodeIds.map((nodeId) => {
    const item = returned.get(nodeId);
    const frames = item ? Number(item.frames) : null;
    const card = document.createElement("div");
    card.className = `frame-node ${frames === null ? "pending" : frames <= 0 ? "error" : "ok"}`;
    const name = document.createElement("span");
    name.className = "frame-node-name";
    name.textContent = `Node ${nodeId}${labels[nodeId] ? ` · ${labels[nodeId]}` : ""}`;
    const value = document.createElement("strong");
    value.textContent = frames === null ? "等待回传" : frames < 0 ? "不可用" : frames.toLocaleString("zh-CN");
    const unit = document.createElement("span");
    unit.className = "frame-node-unit";
    unit.textContent = frames === null ? "" : "帧";
    card.append(name, value, unit);
    return card;
  }));
}

function renderCaptureOptions(options = {}, phase = "idle") {
  const taskNames = options.task_names || [];
  const levels = options.complex_levels || [];
  const signature = JSON.stringify([taskNames, levels]);
  if (signature !== captureOptionsSignature) {
    const previousTask = taskNameSelect.value;
    const previousLevel = complexLevelSelect.value;
    const taskItems = taskNames.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      return option;
    });
    const addOption = document.createElement("option");
    addOption.value = "__add__";
    addOption.textContent = "+ 添加任务";
    taskNameSelect.replaceChildren(...taskItems, addOption);
    complexLevelSelect.replaceChildren(...levels.map((level) => {
      const option = document.createElement("option");
      option.value = level;
      option.textContent = level;
      return option;
    }));
    taskNameSelect.value = taskNames.includes(previousTask)
      ? previousTask
      : options.selected_task_name || taskNames[0] || "";
    complexLevelSelect.value = levels.includes(previousLevel)
      ? previousLevel
      : options.selected_complex_level || levels[0] || "";
    captureOptionsSignature = signature;
  }
  const locked = ["starting_capture", "recording", "stopping_capture"].includes(phase);
  taskNameSelect.disabled = locked;
  complexLevelSelect.disabled = locked;
  customTaskInput.disabled = locked;
  addTaskButton.disabled = locked;
}

function renderTrackers(trackers = []) {
  const list = byId("tracker-status-list");
  if (!trackers.length) {
    const empty = document.createElement("span");
    empty.className = "tracker-empty";
    empty.textContent = "数据流启动后显示 Tracker 状态";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...trackers.map((tracker) => {
    const healthy = Boolean(tracker.connected && tracker.tracking);
    const chip = document.createElement("span");
    chip.className = `tracker-chip ${healthy ? "ok" : "lost"}`;
    chip.title = `${tracker.serial} · ${tracker.connected ? "在线" : "掉线"} · ${tracker.tracking ? "追踪正常" : "追踪丢失"} · 更新于 ${tracker.updated_at}`;
    const dot = document.createElement("i");
    dot.className = "tracker-dot";
    chip.append(dot, document.createTextNode(`${tracker.role} · ${healthy ? "正常" : "丢失"}`));
    return chip;
  }));
}

function renderState(state) {
  byId("phase-label").textContent = phaseLabels[state.phase] || state.phase;
  byId("phase-message").textContent = state.message;
  const dot = byId("phase-dot");
  const preparingPhases = ["starting_ble", "stopping_ble", "starting_streams", "stopping_streams", "starting_capture", "stopping_capture"];
  const dotPhase = preparingPhases.includes(state.phase)
    ? "preparing"
    : ["ble_ready", "streams_ready"].includes(state.phase)
      ? "ready"
      : state.phase;
  dot.className = `status-dot ${dotPhase}`;
  const controls = state.controls;
  const bleRunning = Boolean(state.processes.ble.pid);
  bleButton.textContent = bleRunning ? "停止常驻授时" : "开启授时 + 数据流";
  bleButton.disabled = bleRunning ? !controls.can_stop_ble : !controls.can_start_ble;
  const streamsRunning = Boolean(state.processes.manus.pid || state.processes.manus_client.pid);
  streamsButton.textContent = streamsRunning
    ? "停止 Tracker & MANUS 数据流"
    : "重新启动 Tracker & MANUS 数据流";
  streamsButton.disabled = streamsRunning ? !controls.can_stop_streams : !controls.can_start_streams;
  scanButton.disabled = !controls.can_scan_wearables;
  startButton.disabled = !controls.can_start_capture;
  stopButton.disabled = !controls.can_stop_capture;
  if (state.error) showNotice(state.message);
  else if (!notice.dataset.manual) showNotice("");
  renderProcess("ble", state.processes.ble);
  renderProcess("manus", state.processes.manus);
  renderProcess("manus-client", state.processes.manus_client);
  renderMode2(state.mode2);
  renderFrameReport(state.mode2.frame_report);
  renderCaptureOptions(state.capture_options, state.phase);
  startButton.disabled = !controls.can_start_capture || taskNameSelect.value === "__add__";
  renderTrackers(state.trackers);
  appendLogs("ble-timesync", state.logs.ble_timesync.items);
  appendLogs("ble-control", state.logs.ble_control.items);
  appendLogs("manus", state.logs.manus.items);
  lastSeq.ble_timesync = state.logs.ble_timesync.last_seq;
  lastSeq.ble_control = state.logs.ble_control.last_seq;
  lastSeq.manus = state.logs.manus.last_seq;
  byId("output-path").textContent = state.capture_output_dir
    ? `联合采集输出目录：${state.capture_output_dir}`
    : "联合采集输出目录：尚未开始";
}

async function pollState() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const state = await request(`/api/state?after_ble_timesync=${lastSeq.ble_timesync}&after_ble_control=${lastSeq.ble_control}&after_manus=${lastSeq.manus}`);
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
      body: JSON.stringify({
        task_name: taskNameSelect.value,
        complex_level: complexLevelSelect.value,
      }),
    });
    await pollState();
  } catch (error) {
    notice.dataset.manual = "true";
    showNotice(error.message);
  }
});

taskNameSelect.addEventListener("change", () => {
  const adding = taskNameSelect.value === "__add__";
  customTaskControl.hidden = !adding;
  if (adding) startButton.disabled = true;
  if (adding) customTaskInput.focus();
});

addTaskButton.addEventListener("click", async () => {
  showNotice("");
  delete notice.dataset.manual;
  try {
    const result = await request("/api/task-options", {
      method: "POST",
      body: JSON.stringify({ task_name: customTaskInput.value }),
    });
    captureOptionsSignature = "";
    await pollState();
    taskNameSelect.value = result.task_name;
    customTaskInput.value = "";
    customTaskControl.hidden = true;
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

streamsButton.addEventListener("click", async () => {
  showNotice("");
  delete notice.dataset.manual;
  try {
    const running = streamsButton.textContent.startsWith("停止");
    await request(running ? "/api/streams/stop" : "/api/streams/start", {
      method: "POST",
      body: "{}",
    });
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
    byId("manus-log").replaceChildren();
    lastSeq = { ble_timesync: 0, ble_control: 0, manus: 0 };
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

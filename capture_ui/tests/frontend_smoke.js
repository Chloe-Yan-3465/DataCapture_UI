"use strict";

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.children = [];
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.clientHeight = 0;
    this.value = "120";
  }

  addEventListener() {}
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren(...items) { this.children = items; }
}

const elements = new Map();
global.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new FakeElement(id));
    return elements.get(id);
  },
  createElement() { return new FakeElement(); },
  createTextNode(text) { return { textContent: text }; },
};

global.setInterval = () => 0;
global.fetch = async (url) => ({
  ok: true,
  status: 200,
  async json() {
    if (String(url).startsWith("/api/preflight")) {
      return { ok: true, checks: [] };
    }
    return {
      phase: "idle",
      message: "准备就绪",
      error: null,
      vive_output_dir: null,
      mode2: {
        name: "Mode2Coordinator",
        serial: "COM14@115200",
        time_sync_state: "SYNCED",
        control_state: "IDLE",
        control_result: "STANDBY",
        utc_map_state: "LOCKED",
        nodes: [
          { node_id: "1", connected: true, state: "IDLE", details: "session=0", updated_at: "2026-08-08T12:00:00+08:00" },
        ],
        frame_report: {
          status: "complete",
          session_id: "42",
          expected_nodes: ["1"],
          nodes: [{ node_id: "1", frames: 18000, updated_at: "2026-08-08T12:10:00+08:00" }],
        },
      },
      controls: {
        can_start_ble: true,
        can_stop_ble: false,
        can_scan_wearables: false,
        can_start_capture: false,
        can_stop_capture: false,
        can_bind_trackers: true,
        can_cancel_binding: false,
      },
      processes: {
        ble: { status: "stopped", pid: null },
        vive: { status: "stopped", pid: null },
      },
      logs: {
        ble_timesync: { items: [], last_seq: 0 },
        ble_control: { items: [], last_seq: 0 },
        vive: { items: [], last_seq: 0 },
      },
    };
  },
});

require("../static/app.js");

setTimeout(() => {
  const notice = elements.get("notice");
  if (notice.textContent.includes("错误") || notice.textContent.includes("not defined")) {
    throw new Error(`Frontend smoke test failed: ${notice.textContent}`);
  }
  if (!elements.has("bind-button")) {
    throw new Error("bind-button control was not initialized");
  }
  if (!elements.has("scan-button")) {
    throw new Error("scan-button control was not initialized");
  }
  const nodeList = elements.get("node-list");
  if (!nodeList || nodeList.children.length !== 1) {
    throw new Error("Mode2 node indicator was not rendered");
  }
  if (elements.get("control-chip").textContent !== "控制 IDLE") {
    throw new Error("Mode2 control state was not rendered");
  }
  if (elements.get("control-result").textContent !== "待机") {
    throw new Error("Mode2 control result badge was not rendered");
  }
  if (elements.get("frame-report-status").textContent !== "Episode 42 · 回传完成") {
    throw new Error("Episode frame report status was not rendered");
  }
  const frameList = elements.get("frame-report-list");
  if (!frameList || frameList.children.length !== 1) {
    throw new Error("Episode frame count was not rendered");
  }
  process.stdout.write("frontend runtime smoke: OK\n");
}, 20);

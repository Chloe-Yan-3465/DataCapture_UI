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
      gateways: [
        { device_id: "68", state: "IDLE", updated_at: "2026-08-06T12:00:00+08:00" },
      ],
      controls: {
        can_start_ble: true,
        can_stop_ble: false,
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
        ble: { items: [], last_seq: 0 },
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
  const gatewayList = elements.get("gateway-list");
  if (!gatewayList || gatewayList.children.length !== 1) {
    throw new Error("connected gateway indicator was not rendered");
  }
  process.stdout.write("frontend runtime smoke: OK\n");
}, 20);

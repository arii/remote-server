import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createPairing, INPUT_GRACE_MS, MAX_BACKOFF_MS, RECONNECT_ALARM } from "../pairing.js";
import { DEFAULT_ORIGIN } from "../config.js";

const event = () => {
  const listeners = [];
  return {addListener: vi.fn((listener) => listeners.push(listener)), emit: (...args) => Promise.all(listeners.map((listener) => listener(...args)))};
};
const store = () => {
  const values = {};
  return {
    values,
    get: vi.fn(async (keys) => typeof keys === "string" ? {[keys]: values[keys]} : {...keys, ...values}),
    set: vi.fn(async (patch) => Object.assign(values, patch)),
    remove: vi.fn(async (key) => { delete values[key]; })
  };
};
class Socket {
  static OPEN = 1;
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.send = vi.fn();
    this.close = vi.fn(() => { this.readyState = 3; void this.onclose?.({code: 1000, reason: ""}); });
    Socket.instances.push(this);
  }
  async open() { this.readyState = 1; await this.onopen(); }
  async message(message) { this.onmessage({data: JSON.stringify(message)}); await settle(); }
  async drop(code = 1006, reason = "") { this.readyState = 3; await this.onclose({code, reason}); }
  messages() { return this.send.mock.calls.map(([raw]) => JSON.parse(raw)); }
}
async function settle() { for (let i = 0; i < 50; i++) await Promise.resolve(); }

describe("Buttons pair service worker", () => {
  let api, pairing, sender;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    Socket.instances = [];
    api = {
      runtime: {id: "buttons", getURL: (path) => `chrome-extension://buttons/${path}`, onMessage: event()},
      storage: {local: store(), session: store()},
      alarms: {create: vi.fn(async () => {}), clear: vi.fn(async () => {}), onAlarm: event()},
      tabs: {create: vi.fn(async ({url}) => ({id: 7, windowId: 1, url})), get: vi.fn(async () => ({id: 7, url: DEFAULT_ORIGIN})), group: vi.fn(async () => 12), onRemoved: event(), onUpdated: event()},
      tabGroups: {query: vi.fn(async () => []), update: vi.fn(async () => {})},
      debugger: {attach: vi.fn(async () => {}), detach: vi.fn(async () => {}), sendCommand: vi.fn(async () => ({ok: true})), onEvent: event(), onDetach: event()},
      scripting: {executeScript: vi.fn(async () => [])}
    };
    vi.stubGlobal("chrome", api);
    vi.stubGlobal("WebSocket", Socket);
    pairing = createPairing(chrome, WebSocket);
    sender = {id: "buttons", url: "chrome-extension://buttons/popup.html"};
  });
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });
  async function connect() {
    await pairing.start();
    await pairing.handleMessage({type: "connect"}, sender);
    const socket = Socket.instances.at(-1);
    await socket.open();
    return socket;
  }
  async function open(socket) { await socket.message({id: 1, op: "open", url: DEFAULT_ORIGIN}); }
  const human = (kind = "key", extra = {}) => ({type: "human_input", kind, ...extra});
  const fromTab = () => ({id: "buttons", tab: {id: 7}});

  it("does not pair on installation; opt-in and Disconnect persist", async () => {
    await pairing.start();
    expect(Socket.instances).toHaveLength(0);
    const socket = await connect();
    expect(socket.url).toBe("wss://dev-onasafari.ddns.net:8443/buttons/api/browser/ext");
    expect(api.storage.local.values.optedIn).toBe(true);
    expect((await pairing.handleMessage({type: "status"}, sender)).status).toBe("Paired");
    await pairing.handleMessage({type: "disconnect"}, sender);
    expect(api.storage.local.values.optedIn).toBe(false);
    expect(socket.close).toHaveBeenCalledOnce();
    expect(api.alarms.clear).toHaveBeenCalledWith(RECONNECT_ALARM);
    await api.alarms.onAlarm.emit({name: RECONNECT_ALARM}); await settle();
    expect(Socket.instances).toHaveLength(1);
  });

  it("restores opt-in, but only detaches the tab previously created by the extension", async () => {
    api.storage.local.values.optedIn = true;
    api.storage.session.values.agentTabId = 6;
    await pairing.start();
    expect(Socket.instances).toHaveLength(1);
    expect(api.debugger.detach).toHaveBeenCalledWith({tabId: 6});
    expect(api.debugger.attach).not.toHaveBeenCalled();
  });

  it("creates and groups a new tab before attaching and injecting only that tab", async () => {
    const socket = await connect(); await open(socket);
    expect(api.tabs.create).toHaveBeenCalledWith({url: `${DEFAULT_ORIGIN}/`, active: true});
    expect(api.tabs.group).toHaveBeenCalledWith({tabIds: [7]});
    expect(api.tabGroups.update).toHaveBeenCalledWith(12, {title: "Buttons agent"});
    expect(api.tabs.group.mock.invocationCallOrder[0]).toBeLessThan(api.debugger.attach.mock.invocationCallOrder[0]);
    expect(api.debugger.attach).toHaveBeenCalledWith({tabId: 7}, "1.3");
    expect(api.scripting.executeScript).toHaveBeenCalledWith({target: {tabId: 7}, files: ["takeover.js"]});
    expect(socket.messages()).toContainEqual({id: 1, result: {}});
  });

  it("reuses the agent group in the tab's window, not an existing tab", async () => {
    api.tabGroups.query.mockResolvedValue([{id: 99}]);
    const socket = await connect(); await open(socket);
    expect(api.tabGroups.query).toHaveBeenCalledWith({title: "Buttons agent", windowId: 1});
    expect(api.tabs.group).toHaveBeenCalledWith({tabIds: [7], groupId: 99});
    expect(api.tabs.create).toHaveBeenCalledOnce();
  });

  it("matches the backend's about:blank open and waits for a navigable page to inject", async () => {
    api.tabs.get.mockResolvedValue({id: 7, url: "about:blank"});
    const socket = await connect();
    await socket.message({id: 1, op: "open", url: "about:blank"});
    expect(socket.messages()).toContainEqual({id: 1, result: {}});
    expect(api.scripting.executeScript).not.toHaveBeenCalled();
    api.tabs.get.mockResolvedValue({id: 7, url: DEFAULT_ORIGIN});
    await api.tabs.onUpdated.emit(7, {status: "complete"}); await settle();
    expect(api.scripting.executeScript).toHaveBeenCalledOnce();
  });

  it("never attaches to or sends CDP to an unrelated tab or target session", async () => {
    const socket = await connect();
    await socket.message({id: 2, op: "cdp", method: "Page.enable", params: {}});
    expect(socket.messages().at(-1).error).toBe("No agent tab");
    await open(socket);
    for (const extra of [{tabId: 8}, {params: {tabId: 8}}, {sessionId: "other"}, {method: "Target.attachToTarget"}, {method: "Browser.close"}]) {
      await socket.message({id: 3, op: "cdp", method: "Page.enable", params: {}, ...extra});
      expect(socket.messages().at(-1)).toHaveProperty("error");
    }
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
    expect(api.debugger.attach).toHaveBeenCalledTimes(1);
    await socket.message({id: 4, op: "cdp", method: "Runtime.evaluate", params: {expression: "document.title"}});
    expect(api.debugger.sendCommand).toHaveBeenCalledWith({tabId: 7}, "Runtime.evaluate", {expression: "document.title"});
    expect(socket.messages().at(-1)).toEqual({id: 4, result: {ok: true}});
  });

  it("forwards only the agent tab's CDP events", async () => {
    const socket = await connect(); await open(socket); socket.send.mockClear();
    await api.debugger.onEvent.emit({tabId: 8}, "Page.loadEventFired", {timestamp: 1});
    await api.debugger.onEvent.emit({tabId: 7}, "Page.loadEventFired", {timestamp: 2});
    expect(socket.messages()).toEqual([{event: "cdp", method: "Page.loadEventFired", params: {timestamp: 2}}]);
  });

  it.each(["removal", "detach"])("sends tab_closed exactly once on %s", async (kind) => {
    const socket = await connect(); await open(socket); socket.send.mockClear();
    if (kind === "removal") await api.tabs.onRemoved.emit(7);
    else await api.debugger.onDetach.emit({tabId: 7}, "canceled_by_user");
    await api.tabs.onRemoved.emit(7);
    await api.debugger.onDetach.emit({tabId: 8});
    expect(socket.messages()).toEqual([{event: "tab_closed"}]);
  });

  it("uses a heartbeat every 20 seconds without new protocol fields", async () => {
    const socket = await connect();
    await vi.advanceTimersByTimeAsync(40000);
    expect(socket.messages()).toEqual([{}, {}]);
    await pairing.disconnect();
    await vi.advanceTimersByTimeAsync(20000);
    expect(socket.messages()).toEqual([{}, {}]);
  });

  it("Disconnect during the opening handshake cannot restart the keepalive", async () => {
    await pairing.start(); await pairing.handleMessage({type: "connect"}, sender);
    const socket = Socket.instances[0];
    let finish;
    api.alarms.clear.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const opened = socket.open(); await settle();
    await pairing.disconnect();
    finish(); await opened;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("uses one reconnect path with persisted exponential, capped alarm backoff", async () => {
    await connect();
    for (const delay of [60000, 120000, 240000, MAX_BACKOFF_MS, MAX_BACKOFF_MS]) {
      const socket = Socket.instances.at(-1);
      const count = Socket.instances.length;
      const now = Date.now();
      await socket.drop();
      expect(api.alarms.create).toHaveBeenLastCalledWith(RECONNECT_ALARM, {when: now + delay});
      await api.alarms.onAlarm.emit({name: RECONNECT_ALARM}); await settle();
      expect(Socket.instances).toHaveLength(count);
      await vi.advanceTimersByTimeAsync(delay);
      await api.alarms.onAlarm.emit({name: RECONNECT_ALARM}); await settle();
      expect(Socket.instances).toHaveLength(count + 1);
    }
    await Socket.instances.at(-1).open();
    expect(api.storage.local.values.retryAttempt).toBe(0);
  });

  it("reports OAuth rejection and does not fight a replacement pairing", async () => {
    await pairing.start(); await pairing.handleMessage({type: "connect"}, sender);
    await Socket.instances[0].drop();
    expect((await pairing.handleMessage({type: "status"}, sender)).status).toBe("sign in to Buttons first");
    await pairing.handleMessage({type: "connect"}, sender); await Socket.instances[1].open();
    await Socket.instances[1].drop(1000, "replaced by a new paired Chrome");
    expect(api.storage.local.values.optedIn).toBe(false);
  });

  it("debounces human input, rejects unrelated senders, and never forwards key values", async () => {
    const socket = await connect(); await open(socket); socket.send.mockClear();
    await pairing.handleMessage(human("key", {key: "password-probe", code: "KeyP", text: "secret"}), fromTab());
    await pairing.handleMessage(human("pointer"), fromTab());
    await pairing.handleMessage(human("key"), {id: "buttons", tab: {id: 8}});
    await pairing.handleMessage(human("key"), {id: "foreign", tab: {id: 7}});
    await vi.advanceTimersByTimeAsync(601);
    await pairing.handleMessage(human("touch"), fromTab());
    expect(socket.messages()).toEqual([{event: "human_input", kind: "key"}, {event: "human_input", kind: "touch"}]);
    expect(JSON.stringify(socket.messages())).not.toMatch(/password-probe|KeyP|secret/);
  });

  it("agent clicks do not self-pause while in flight or during the grace window", async () => {
    const socket = await connect(); await open(socket); socket.send.mockClear();
    let finish;
    api.debugger.sendCommand.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await socket.message({id: 8, op: "cdp", method: "Input.dispatchMouseEvent", params: {type: "mousePressed"}});
    await pairing.handleMessage(human("pointer"), fromTab());
    expect(socket.messages()).toEqual([]);
    finish({}); await settle();
    await pairing.handleMessage(human("pointer"), fromTab());
    expect(socket.messages()).toEqual([{id: 8, result: {}}]);
    await vi.advanceTimersByTimeAsync(INPUT_GRACE_MS + 1);
    await pairing.handleMessage(human("pointer"), fromTab());
    expect(socket.messages().at(-1)).toEqual({event: "human_input", kind: "pointer"});
  });

  it("fails closed if takeover injection is denied and retries injection on navigation", async () => {
    api.scripting.executeScript.mockRejectedValue(new Error("no host access"));
    const socket = await connect(); await open(socket);
    await socket.message({id: 2, op: "cdp", method: "Input.insertText", params: {text: "secret"}});
    expect(socket.messages().at(-1).error).toContain("Allow this site's access");
    expect(api.debugger.sendCommand).not.toHaveBeenCalled();
    api.scripting.executeScript.mockResolvedValue([]);
    await api.tabs.onUpdated.emit(8, {status: "complete"});
    expect(api.scripting.executeScript).toHaveBeenCalledTimes(1);
    await api.tabs.onUpdated.emit(7, {status: "complete"}); await settle();
    await socket.message({id: 3, op: "cdp", method: "Input.insertText", params: {text: "allowed"}});
    expect(socket.messages().at(-1)).toEqual({id: 3, result: {ok: true}});
  });

  it("returns command errors, including failed attach, rather than success", async () => {
    api.debugger.attach.mockRejectedValueOnce(new Error("attach denied"));
    const socket = await connect(); await open(socket);
    expect(socket.messages().at(-1)).toEqual({id: 1, error: "attach denied"});
    await open(socket);
    api.debugger.sendCommand.mockRejectedValueOnce(new Error("detached"));
    await socket.message({id: 4, op: "cdp", method: "Page.enable", params: {}});
    expect(socket.messages().at(-1)).toEqual({id: 4, error: "detached"});
  });
});

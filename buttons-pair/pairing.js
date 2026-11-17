import { DEFAULT_ORIGIN, serverOrigin, socketUrl } from "./config.js";

export const RECONNECT_ALARM = "buttons-pair-reconnect";
export const INPUT_GRACE_MS = 300;
export const HUMAN_DEBOUNCE_MS = 600;
export const MAX_BACKOFF_MS = 300000;

export function createPairing(api, Socket) {
  let socket = null;
  let ping = null;
  let agentTabId = null;
  let observerReady = false;
  let observerVersion = 0;
  let inputInFlight = 0;
  let suppressUntil = 0;
  let lastHuman = -Infinity;
  let status = "Not paired";
  let error = "";
  let opening = false;
  let connecting = null;

  const settings = () => api.storage.local.get({optedIn: false, serverOrigin: DEFAULT_ORIGIN, retryAttempt: 0, retryAt: 0});
  const send = (message, channel = socket) => {
    if (channel && channel === socket && channel.readyState === Socket.OPEN) channel.send(JSON.stringify(message));
  };
  const current = (channel) => channel === socket && channel.readyState === Socket.OPEN;
  const stopPing = () => {
    clearInterval(ping);
    ping = null;
  };

  async function releaseTab() {
    const tabId = agentTabId;
    agentTabId = null;
    observerReady = false;
    await api.storage.session.remove("agentTabId");
    if (tabId !== null) {
      try {
        await api.debugger.detach({tabId});
      } catch {
        // Already detached or removed tabs need no further cleanup.
      }
    }
  }

  async function inject(tabId) {
    if (tabId !== agentTabId) return;
    const version = ++observerVersion;
    observerReady = false;
    const tab = await api.tabs.get(tabId);
    if (!/^https?:/.test(tab.url || "")) return;
    try {
      await api.scripting.executeScript({target: {tabId, allFrames: true}, files: ["takeover.js"]});
      if (tabId === agentTabId && version === observerVersion) observerReady = true;
      error = "";
    } catch {
      error = "Allow this site's access in Buttons pair options before driving it.";
    }
  }

  async function openTab(command, channel) {
    if (opening) throw new Error("Agent tab is already opening");
    const url = new URL(command.url);
    if (!["https:", "http:"].includes(url.protocol) && url.href !== "about:blank") throw new Error("Agent tab needs an HTTP(S) URL or about:blank");
    opening = true;
    try {
      await releaseTab();
      if (!current(channel)) throw new Error("Pairing disconnected");
      const tab = await api.tabs.create({url: url.href, active: true});
      agentTabId = tab.id;
      await api.storage.session.set({agentTabId});
      const groups = await api.tabGroups.query({title: "Buttons agent", windowId: tab.windowId});
      const groupId = await api.tabs.group({tabIds: [tab.id], ...(groups.length ? {groupId: groups[0].id} : {})});
      await api.tabGroups.update(groupId, {title: "Buttons agent"});
      if (!current(channel) || tab.id !== agentTabId) throw new Error("Pairing disconnected");
      await api.debugger.attach({tabId: tab.id}, "1.3");
      if (!current(channel) || tab.id !== agentTabId) throw new Error("Pairing disconnected");
      await inject(tab.id);
      return {};
    } catch (cause) {
      await releaseTab();
      throw cause;
    } finally {
      opening = false;
    }
  }

  async function command(message, channel) {
    if (!Number.isInteger(message.id)) return;
    try {
      if ("tabId" in message || "sessionId" in message || (message.params && ("tabId" in message.params || "sessionId" in message.params))) throw new Error("CDP is restricted to the agent tab");
      let result;
      if (message.op === "open") {
        result = await openTab(message, channel);
      } else if (message.op === "cdp") {
        if (agentTabId === null) throw new Error("No agent tab");
        if (typeof message.method !== "string" || !/^[A-Za-z]+\.[A-Za-z]+$/.test(message.method) || /^(Target|Browser)\./.test(message.method)) throw new Error("CDP method is not agent-tab scoped");
        const input = message.method.startsWith("Input.");
        if (input && !observerReady) throw new Error(error || "Wait for takeover detection on the agent page");
        if (input) inputInFlight += 1;
        try {
          result = await api.debugger.sendCommand({tabId: agentTabId}, message.method, message.params || {});
        } finally {
          if (input) {
            inputInFlight -= 1;
            suppressUntil = Date.now() + INPUT_GRACE_MS;
          }
        }
      } else {
        throw new Error("Unknown Buttons pair operation");
      }
      send({id: message.id, result: result || {}}, channel);
    } catch (cause) {
      send({id: message.id, error: cause.message}, channel);
    }
  }

  async function scheduleReconnect() {
    const config = await settings();
    if (!config.optedIn) return;
    const retryAt = Date.now() + Math.min(60000 * 2 ** Math.min(config.retryAttempt, 4), MAX_BACKOFF_MS);
    await api.storage.local.set({retryAttempt: config.retryAttempt + 1, retryAt});
    await api.alarms.create(RECONNECT_ALARM, {when: retryAt});
  }

  async function connect() {
    const config = await settings();
    if (!config.optedIn || socket) return;
    if (config.retryAt > Date.now()) {
      await api.alarms.create(RECONNECT_ALARM, {when: config.retryAt});
      return;
    }
    const origin = serverOrigin(config.serverOrigin);
    const channel = new Socket(socketUrl(origin));
    socket = channel;
    let opened = false;
    channel.onopen = async () => {
      if (socket !== channel) return;
      opened = true;
      status = "Paired";
      error = "";
      await api.storage.local.set({retryAttempt: 0, retryAt: 0});
      await api.alarms.clear(RECONNECT_ALARM);
      if (socket !== channel) return;
      stopPing();
      // Empty JSON is a heartbeat the step-1 receiver safely ignores.
      ping = setInterval(() => send({}, channel), 20000);
    };
    channel.onmessage = (event) => {
      if (!current(channel)) return;
      try {
        const message = JSON.parse(event.data);
        if (message && typeof message === "object") void command(message, channel);
      } catch {
        error = "Invalid JSON from Buttons";
      }
    };
    channel.onclose = async (event) => {
      if (socket !== channel) return;
      socket = null;
      stopPing();
      status = !opened || event.code === 1008 ? "sign in to Buttons first" : "Not paired";
      await releaseTab();
      if (event.reason?.includes("replaced")) {
        await api.storage.local.set({optedIn: false});
      } else {
        await scheduleReconnect();
      }
    };
    channel.onerror = () => {
      if (socket === channel && !opened) status = "sign in to Buttons first";
    };
  }

  function ensureConnection() {
    if (!connecting) connecting = connect().catch((cause) => {
      status = "Not paired";
      error = cause.message;
    }).finally(() => { connecting = null; });
    return connecting;
  }

  async function disconnect() {
    await api.storage.local.set({optedIn: false, retryAttempt: 0, retryAt: 0});
    await api.alarms.clear(RECONNECT_ALARM);
    const channel = socket;
    socket = null;
    stopPing();
    if (channel) channel.close(1000, "disconnected by user");
    await releaseTab();
    status = "Not paired";
    error = "";
  }

  async function handleMessage(message, sender) {
    if (sender.id !== api.runtime.id) return;
    if (message.type === "human_input") {
      if (sender.tab?.id !== agentTabId || !["pointer", "key", "touch"].includes(message.kind)) return;
      const now = Date.now();
      if (inputInFlight || now < suppressUntil) return;
      const quiet = now - lastHuman >= HUMAN_DEBOUNCE_MS;
      lastHuman = now;
      if (quiet) send({event: "human_input", kind: message.kind});
      return;
    }
    if (sender.tab || !sender.url?.startsWith(api.runtime.getURL(""))) return;
    if (message.type === "connect") {
      await api.storage.local.set({optedIn: true, retryAttempt: 0, retryAt: 0});
      await ensureConnection();
    } else if (message.type === "disconnect") {
      await disconnect();
    }
    return {status, error, ...(await settings())};
  }

  api.runtime.onMessage.addListener((message, sender, reply) => {
    handleMessage(message, sender).then(reply, (cause) => reply({status, error: cause.message}));
    return true;
  });
  api.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === RECONNECT_ALARM) void ensureConnection();
  });
  api.debugger.onEvent.addListener((source, method, params) => {
    if (source.tabId === agentTabId) send({event: "cdp", method, params: params || {}});
  });
  function tabClosed(tabId) {
    if (tabId !== agentTabId) return;
    agentTabId = null;
    observerReady = false;
    void api.storage.session.remove("agentTabId");
    send({event: "tab_closed"});
  }
  api.debugger.onDetach.addListener((source) => tabClosed(source.tabId));
  api.tabs.onRemoved.addListener(tabClosed);
  api.tabs.onUpdated.addListener((tabId, change) => {
    if (tabId !== agentTabId) return;
    if (change.status === "loading") { observerReady = false; observerVersion += 1; }
    if (change.status === "complete") void inject(tabId).catch((cause) => { error = cause.message; });
  });

  api.webNavigation.onBeforeNavigate.addListener(({tabId}) => {
    if (tabId !== agentTabId) return;
    observerReady = false;
    observerVersion += 1;
  });
  api.webNavigation.onCompleted.addListener(({tabId}) => {
    if (tabId === agentTabId) void inject(tabId).catch((cause) => { error = cause.message; });
  });

  async function start() {
    const saved = await api.storage.session.get("agentTabId");
    agentTabId = saved.agentTabId ?? null;
    await releaseTab();
    await ensureConnection();
  }
  return {start, disconnect, handleMessage};
}

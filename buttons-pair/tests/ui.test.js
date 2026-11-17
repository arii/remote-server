import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_ORIGIN, serverOrigin, socketUrl } from "../config.js";

async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
const html = (name) => readFileSync(join(import.meta.dirname, `../${name}.html`), "utf8");
const render = (name) => {
  const markup = html(name).replace(/<link\b[^>]*>/g, "").replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "");
  const page = new DOMParser().parseFromString(markup, "text/html");
  document.body.innerHTML = page.body.innerHTML;
};

beforeEach(() => { vi.useFakeTimers(); vi.resetModules(); });
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); document.body.innerHTML = ""; });

it("uses the requested manifest permissions and injects no always-on content scripts", () => {
  const manifest = JSON.parse(readFileSync(join(import.meta.dirname, "../manifest.json"), "utf8"));
  expect(manifest.manifest_version).toBe(3);
  expect(manifest.permissions).toEqual(["debugger", "tabs", "tabGroups", "storage", "alarms", "scripting"]);
  expect(manifest.host_permissions).toEqual([`${DEFAULT_ORIGIN}/*`]);
  expect(manifest.content_scripts).toBeUndefined();
  expect(manifest.background).toEqual({service_worker: "service-worker.js", type: "module"});
});

it("accepts HTTPS origins and derives only the fixed backend WebSocket path", () => {
  expect(serverOrigin("https://other.example:9443/")).toBe("https://other.example:9443");
  expect(socketUrl("https://other.example:9443")).toBe("wss://other.example:9443/buttons/api/browser/ext");
  for (const invalid of ["", "http://other.example", "https://user:pass@other.example", "https://other.example/path", "https://other.example/?q=1"]) {
    expect(() => serverOrigin(invalid)).toThrow();
  }
});

describe("popup", () => {
  let api;
  beforeEach(() => {
    render("popup");
    api = {runtime: {sendMessage: vi.fn(async () => ({status: "Not paired", error: "", optedIn: false, serverOrigin: DEFAULT_ORIGIN})), openOptionsPage: vi.fn()}};
    vi.stubGlobal("chrome", api);
  });

  it("renders connection states, sign-in link and Connect/Disconnect controls", async () => {
    await import("../popup.js"); await settle();
    expect(document.querySelector("#status").textContent).toBe("Not paired");
    expect(document.querySelector("#signin").href).toBe(`${DEFAULT_ORIGIN}/buttons`);
    expect(document.querySelector("#disconnect").disabled).toBe(true);
    api.runtime.sendMessage.mockResolvedValue({status: "Paired", error: "", optedIn: true, serverOrigin: DEFAULT_ORIGIN});
    document.querySelector("#connect").click(); await settle();
    expect(api.runtime.sendMessage).toHaveBeenLastCalledWith({type: "connect"});
    expect(document.querySelector("#status").textContent).toBe("Paired");
    expect(document.querySelector("#connect").disabled).toBe(true);
    api.runtime.sendMessage.mockResolvedValue({status: "sign in to Buttons first", error: "", optedIn: true, serverOrigin: DEFAULT_ORIGIN});
    await vi.advanceTimersByTimeAsync(1000);
    expect(document.querySelector("#status").textContent).toBe("sign in to Buttons first");
    document.querySelector("#disconnect").click(); await settle();
    expect(api.runtime.sendMessage).toHaveBeenLastCalledWith({type: "disconnect"});
    document.querySelector("#options").click();
    expect(api.runtime.openOptionsPage).toHaveBeenCalledOnce();
  });

  it("displays worker failures as plain text", async () => {
    api.runtime.sendMessage.mockRejectedValue(new Error("<b>worker unavailable</b>"));
    await import("../popup.js"); await settle();
    expect(document.querySelector("#error").textContent).toBe("<b>worker unavailable</b>");
    expect(document.querySelector("#error b")).toBeNull();
  });
});

describe("options", () => {
  let api;
  beforeEach(() => {
    render("options");
    api = {
      permissions: {contains: vi.fn(async () => false), request: vi.fn(async () => true)},
      storage: {local: {get: vi.fn(async (defaults) => defaults), set: vi.fn(async () => {})}},
      runtime: {sendMessage: vi.fn(async () => ({}))}
    };
    vi.stubGlobal("chrome", api);
  });
  const submit = () => document.querySelector("form").dispatchEvent(new Event("submit", {cancelable: true}));

  it("defaults to S5; saves a permitted origin and disconnects before changing it", async () => {
    await import("../options.js"); await settle();
    expect(document.querySelector("#origin").value).toBe(DEFAULT_ORIGIN);
    document.querySelector("#origin").value = "https://other.example:9443";
    submit(); await settle();
    expect(api.permissions.request).toHaveBeenCalledWith({origins: ["https://other.example:9443/*"]});
    expect(api.runtime.sendMessage).toHaveBeenCalledWith({type: "disconnect"});
    expect(api.storage.local.set).toHaveBeenCalledWith({serverOrigin: "https://other.example:9443"});
    expect(document.querySelector("#message").textContent).toContain("Saved");
  });

  it("does not save or disconnect on invalid origin or denied permission", async () => {
    await import("../options.js"); await settle();
    document.querySelector("#origin").value = ""; submit(); await settle();
    expect(api.permissions.request).not.toHaveBeenCalled();
    expect(api.storage.local.set).not.toHaveBeenCalled();
    document.querySelector("#origin").value = DEFAULT_ORIGIN;
    api.permissions.request.mockResolvedValue(false); submit(); await settle();
    expect(document.querySelector("#message").textContent).toContain("not granted");
    expect(api.storage.local.set).not.toHaveBeenCalled();
    expect(api.runtime.sendMessage).not.toHaveBeenCalled();
  });

  it("requests broader host access only with explicit user selection", async () => {
    await import("../options.js"); await settle();
    expect(api.permissions.request).not.toHaveBeenCalled();
    document.querySelector("#sites").checked = true; submit(); await settle();
    expect(api.permissions.request).toHaveBeenCalledWith({origins: ["http://*/*", "https://*/*"]});
  });
});

// Single-writer storage tests (OD-19 D3): the service worker is the only
// writer of userContextVault and pendingDeltas. The first block mechanically
// enforces the boundary against the source; the second drives the real worker
// message handler over a mocked chrome.storage and proves a panel mutation
// and an observer merge cannot lose each other's writes.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  applyDeltaApprove,
  applyDeltaApproveAll,
  applyDeltaQueue,
  applyVaultDelete,
  applyVaultUpsert,
  createMutationQueue
} from "../extension/lib/storage-mutations.js";
const here = path.dirname(fileURLToPath(import.meta.url));
const sidepanelSrc = readFileSync(path.join(here, "../extension/sidepanel.js"), "utf8");
const workerSrc = readFileSync(path.join(here, "../extension/service-worker.js"), "utf8");

// ---------------------------------------------------------------------------
// Anti-regression guard: no panel write site for vault/delta keys.
// Extracts every chrome.storage.local.set({...}) key list and every
// chrome.storage.local.remove([...]) key list from sidepanel.js.
// ---------------------------------------------------------------------------
function extractSetKeys(source) {
  const keys = [];
  const re = /chrome\.storage\.local\.set\(\s*\{([^}]*)\}/g;
  let m;
  while ((m = re.exec(source))) {
    for (const part of m[1].split(",")) {
      const key = part.split(":")[0].trim();
      if (key) keys.push(key);
    }
  }
  return keys;
}

function extractRemoveKeys(source) {
  const keys = [];
  const re = /chrome\.storage\.local\.remove\(\s*(?:\[([^\]]*)\]|"([^"]+)")/g;
  let m;
  while ((m = re.exec(source))) {
    if (m[1]) {
      for (const part of m[1].split(",")) {
        const key = part.trim().replace(/^"|"$/g, "");
        if (key) keys.push(key);
      }
    } else if (m[2]) {
      keys.push(m[2]);
    }
  }
  return keys;
}

const PROTECTED_KEYS = ["userContextVault", "pendingDeltas"];

describe("single-writer boundary (source guard)", () => {
  it("never writes or removes userContextVault/pendingDeltas from the side panel", () => {
    const written = extractSetKeys(sidepanelSrc);
    const removed = extractRemoveKeys(sidepanelSrc);
    for (const key of PROTECTED_KEYS) {
      expect(written, `sidepanel.js chrome.storage.local.set(${key})`).not.toContain(key);
      expect(removed, `sidepanel.js chrome.storage.local.remove(${key})`).not.toContain(key);
    }
  });

  it("still leaves the single-owner keys in the panel (out of D3 scope)", () => {
    // observerDomainAllowlist and activeSessionContext stay panel-owned; if
    // they vanish from this list the guard's extraction broke, not the code.
    const written = extractSetKeys(sidepanelSrc);
    expect(written).toContain("activeSessionContext");
    expect(written).toContain("observerDomainAllowlist");
  });

  it("keeps the vault/delta writes in the service worker", () => {
    const written = extractSetKeys(workerSrc);
    expect(written).toContain("userContextVault");
    expect(written).toContain("pendingDeltas");
  });
});

// ---------------------------------------------------------------------------
// Pure transition units
// ---------------------------------------------------------------------------
describe("storage-mutations", () => {
  it("vault:upsert updates an existing id in place and unshifts a new one", () => {
    const a = { id: "identity.1", key: "Name", value: "A", aliases: [] };
    const b = { id: "identity.2", key: "Email", value: "b@x.co", aliases: [] };

    const edited = applyVaultUpsert({ userContextVault: [a, b] }, { id: "identity.2", value: "b2@x.co" });
    expect(edited.userContextVault).toHaveLength(2);
    expect(edited.userContextVault[1].value).toBe("b2@x.co");

    const added = applyVaultUpsert({ userContextVault: [a] }, { id: "identity.2", value: "b@x.co" });
    expect(added.userContextVault).toHaveLength(2);
    expect(added.userContextVault[0].id).toBe("identity.2");
  });

  it("vault:delete removes only the matching id", () => {
    const state = { userContextVault: [{ id: "a" }, { id: "b" }] };
    expect(applyVaultDelete(state, "a").userContextVault.map((e) => e.id)).toEqual(["b"]);
  });

  it("delta:queue prepends and caps the queue at 100", () => {
    const existing = Array.from({ length: 98 }, (_, i) => ({ deltaId: `d${i}` }));
    const queued = [{ deltaId: "new" }];
    const next = applyDeltaQueue({ pendingDeltas: existing }, queued);
    expect(next.pendingDeltas).toHaveLength(99);
    expect(next.pendingDeltas[0].deltaId).toBe("new");
    expect(applyDeltaQueue({ pendingDeltas: existing }, queued).pendingDeltas).toHaveLength(99);

    const full = Array.from({ length: 100 }, (_, i) => ({ deltaId: `d${i}` }));
    expect(applyDeltaQueue({ pendingDeltas: full }, queued).pendingDeltas).toHaveLength(100);
    expect(applyDeltaQueue({ pendingDeltas: full }, queued).pendingDeltas[0].deltaId).toBe("new");
  });

  it("batch approve gives every created entry a distinct id and stamps updatedAt", () => {
    const state = {
      userContextVault: [{ id: "identity.1", key: "Name", value: "old", category: "identity", aliases: [] }],
      pendingDeltas: [
        { deltaId: "d1", action: "create", category: "custom", key: "Alpha", proposedValue: "va" },
        { deltaId: "d2", action: "create", category: "custom", key: "Beta", proposedValue: "vb" },
        { deltaId: "d3", action: "create", category: "custom", key: "Gamma", proposedValue: "vc" },
        { deltaId: "d4", action: "update", targetEntryId: "identity.1", proposedValue: "new" }
      ]
    };
    const next = applyDeltaApproveAll(state);
    const ids = next.userContextVault.map((e) => e.id);
    // All 4 ids unique — a batch of same-millisecond creates must not collide
    // (delete/update lookups by id would hit the wrong entry).
    expect(new Set(ids).size).toBe(ids.length);
    // Bulk update stamps updatedAt exactly as single approve does.
    const updated = next.userContextVault.find((e) => e.id === "identity.1");
    expect(updated.value).toBe("new");
    expect(Number.isNaN(Date.parse(updated.updatedAt))).toBe(false);
  });

  it("single approve disambiguates its id when the vault already holds the same-ms id", () => {
    const collidingId = `custom.${Date.now()}`;
    const state = {
      userContextVault: [{ id: collidingId, key: "Held", value: "v0", category: "custom", aliases: [] }],
      pendingDeltas: [{ deltaId: "d1", action: "create", category: "custom", key: "New", proposedValue: "v1" }]
    };
    const next = applyDeltaApprove(state, "d1");
    const ids = next.userContextVault.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(next.userContextVault.find((e) => e.id === collidingId).value).toBe("v0"); // untouched
    expect(next.userContextVault.find((e) => e.key === "New").id).not.toBe(collidingId);
  });

  it("approveAll applies every delta and empties the queue", () => {
    const state = {
      userContextVault: [{ id: "identity.1", key: "Name", value: "old", category: "identity", aliases: [] }],
      pendingDeltas: [
        { deltaId: "d1", action: "update", targetEntryId: "identity.1", proposedValue: "new" },
        { deltaId: "d2", action: "create", category: "custom", key: "Bio", proposedValue: "hello" }
      ]
    };
    const next = applyDeltaApproveAll(state);
    expect(next.pendingDeltas).toEqual([]);
    expect(next.userContextVault.find((e) => e.id === "identity.1").value).toBe("new");
    expect(next.userContextVault.find((e) => e.key === "Bio").value).toBe("hello");
  });

  it("mutation queue serializes tasks and surfaces errors without poisoning the queue", async () => {
    const enqueue = createMutationQueue();
    const order = [];
    const t1 = enqueue(async () => {
      order.push("a-start");
      await new Promise((r) => setTimeout(r, 20));
      order.push("a-end");
      return "a";
    });
    const t2 = enqueue(async () => {
      order.push("b");
      return "b";
    });
    expect(await t1).toBe("a");
    expect(await t2).toBe("b");
    // b ran strictly after a's read-modify-write window closed
    expect(order).toEqual(["a-start", "a-end", "b"]);

    const failing = enqueue(async () => {
      throw new Error("boom");
    });
    const after = enqueue(async () => "recovered");
    await expect(failing).rejects.toThrow("boom");
    expect(await after).toBe("recovered");
  });
});

// ---------------------------------------------------------------------------
// Worker-handler interleaving: the real service-worker message handler over a
// mocked chrome.storage. A panel mutation and a simulated observer merge are
// issued concurrently; the serialized queue must apply BOTH.
// ---------------------------------------------------------------------------
function makeChromeMock() {
  const store = new Map();
  const listeners = { installed: [], startup: [], message: [] };
  const badge = [];
  const chromeMock = {
    storage: {
      local: {
        get: async (keys) =>
          Object.fromEntries([].concat(keys).filter((k) => store.has(k)).map((k) => [k, store.get(k)])),
        set: async (obj) => {
          for (const [k, v] of Object.entries(obj)) store.set(k, v);
        },
        remove: async (keys) => {
          for (const k of [].concat(keys)) store.delete(k);
        }
      }
    },
    sidePanel: { setPanelBehavior: async () => {} },
    runtime: {
      onInstalled: { addListener: (fn) => listeners.installed.push(fn) },
      onStartup: { addListener: (fn) => listeners.startup.push(fn) },
      onMessage: { addListener: (fn) => listeners.message.push(fn) },
      sendMessage: async () => {}
    },
    action: {
      setBadgeText: (o) => badge.push(o.text),
      setBadgeBackgroundColor: () => {}
    }
  };
  return { chromeMock, store, listeners, badge };
}

async function loadWorker(mock) {
  vi.resetModules();
  vi.stubGlobal("chrome", mock.chromeMock);
  await import("../extension/service-worker.js");
  return mock.listeners.message[0];
}

describe("worker mutation handler", () => {
  it("interleaves a panel delta:approve with an observer merge and loses neither", async () => {
    const mock = makeChromeMock();
    mock.store.set("userContextVault", [
      { id: "identity.full_name", key: "Full Name", value: "Ariel Anders", category: "identity", aliases: ["full name"] }
    ]);
    mock.store.set("pendingDeltas", [
      { deltaId: "delta_panel", action: "update", targetEntryId: "identity.full_name", key: "Full Name", proposedValue: "Ariel A. Anders" }
    ]);
    const listener = await loadWorker(mock);

    // Panel approves the queued delta while the observer simultaneously
    // reports a submitted form value that must become a NEW delta.
    const panelMutation = sendUpdate(listener, { type: "delta:approve", deltaId: "delta_panel" });
    const observerMerge = sendUpdate(listener, {
      action: "FORM_SUBMIT_DETECTED",
      payload: {
        title: "Application|Acme",
        url: "https://acme.example/apply",
        filledFormFields: [{ query: "name", value: "Avery Distinct Value", locator: { name: "name" } }]
      }
    });
    const [approveRes, mergeRes] = await Promise.all([panelMutation, observerMerge]);

    expect(approveRes.error).toBeUndefined();
    expect(mergeRes.error).toBeUndefined();

    const vault = mock.store.get("userContextVault");
    const pending = mock.store.get("pendingDeltas");

    // The panel's mutation applied...
    const updated = vault.find((e) => e.id === "identity.full_name");
    expect(updated.value).toBe("Ariel A. Anders");
    // ...and the observer's merge applied too — its delta survived.
    expect(pending.some((d) => d.proposedValue === "Avery Distinct Value")).toBe(true);
    expect(pending.some((d) => d.deltaId === "delta_panel")).toBe(false);
    // The approve response reported the queue state at its own completion
    // (before the merge landed behind it in the serialized queue).
    expect(approveRes.pendingDeltas).toBe(0);
    expect(pending).toHaveLength(1);
  });

  it("interleaves a panel vault:upsert with an observer merge and loses neither", async () => {
    const mock = makeChromeMock();
    mock.store.set("userContextVault", []);
    mock.store.set("pendingDeltas", []);
    const listener = await loadWorker(mock);

    const panelMutation = sendUpdate(listener, {
      type: "vault:upsert",
      entry: { id: "custom.1", category: "custom", key: "Bio", aliases: ["bio"], value: "Manual bio", tags: ["manual"] }
    });
    const observerMerge = sendUpdate(listener, {
      action: "FORM_SUBMIT_DETECTED",
      payload: {
        title: "Form|Corp",
        url: "https://corp.example/",
        filledFormFields: [{ query: "email", value: "found@corp.example", locator: { name: "email" } }]
      }
    });
    await Promise.all([panelMutation, observerMerge]);

    const vault = mock.store.get("userContextVault");
    const pending = mock.store.get("pendingDeltas");
    expect(vault.some((e) => e.id === "custom.1" && e.value === "Manual bio")).toBe(true);
    expect(pending.some((d) => d.proposedValue === "found@corp.example")).toBe(true);
  });

  it("leaves storage untouched for an unrecognized message type (same as before D3)", async () => {
    const mock = makeChromeMock();
    mock.store.set("userContextVault", [{ id: "a" }]);
    mock.store.set("pendingDeltas", []);
    const listener = await loadWorker(mock);

    const res = await sendUpdate(listener, { type: "delta:explode" });
    expect(res).toBeUndefined();
    expect(mock.store.get("userContextVault")).toEqual([{ id: "a" }]);
    expect(mock.store.get("pendingDeltas")).toEqual([]);
  });
});

// The worker's FORM_SUBMIT_DETECTED branch keeps the channel open (returns
// true) and resolves via sendResponse — route both shapes through one helper
// that only resolves once a response arrives.
function sendUpdate(listener, message) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no response for ${message.type || message.action}`)), 1000);
    const maybe = listener(message, { tab: { id: 1 } }, (res) => {
      clearTimeout(timer);
      resolve(res);
    });
    if (maybe !== true) {
      // Synchronous branch that did not respond: treat as an empty ack.
      Promise.resolve().then(() => {
        clearTimeout(timer);
        resolve(undefined);
      });
    }
  });
}

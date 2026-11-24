// Vault onboarding: prompt builder, strict import validator, and the
// queue-then-approve path through the real worker handler.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import { PROFILE_CATEGORIES } from "../extension/lib/profile-ingest.js";
import { MAX_IMPORT_FACTS, MAX_EXISTING_KEYS, MAX_KEY_CHARS, MAX_VALUE_CHARS, buildVaultPrompt, existingKeysForPrompt, parseVaultImport } from "../extension/lib/vault-onboarding.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "../extension/sidepanel.html"), "utf8");
const panelSrc = readFileSync(path.join(here, "../extension/sidepanel.js"), "utf8");

const fact = (over = {}) => ({ category: "identity", key: "Full Name", value: "Jane Example", ...over });
const parse = (v) => parseVaultImport(JSON.stringify(v));
const rejects = (v) => {
  const r = typeof v === "string" ? parseVaultImport(v) : parse(v);
  expect(r.ok).toBe(false);
  expect(r.errors.length).toBeGreaterThan(0);
  return r;
};

describe("parseVaultImport", () => {
  it("accepts a valid array", () => {
    const r = parse([fact(), fact({ category: "social", key: "GitHub", value: "https://github.com/jane" })]);
    expect(r.ok).toBe(true);
    expect(r.facts).toEqual([
      { key: "Full Name", proposedValue: "Jane Example", category: "identity" },
      { key: "GitHub", proposedValue: "https://github.com/jane", category: "social" }
    ]);
  });

  it("accepts a fenced reply", () => {
    const r = parseVaultImport("```json\n" + JSON.stringify([fact()]) + "\n```");
    expect(r.ok).toBe(true);
    expect(r.facts).toHaveLength(1);
  });

  it("rejects non-JSON, non-array and empty input", () => {
    rejects("here are your facts: Jane");
    rejects("");
    rejects({ category: "identity", key: "a", value: "b" });
    rejects([]);
  });

  it("an empty array gets a friendly message", () => {
    expect(parse([])).toEqual({ ok: false, errors: ["The chatbot found nothing new to add."] });
  });

  it("rejects more than MAX_IMPORT_FACTS", () => {
    rejects(Array.from({ length: MAX_IMPORT_FACTS + 1 }, (_, i) => fact({ key: `K${i}`, value: `v${i}` })));
  });

  it("rejects unknown fields", () => {
    for (const extra of [{ id: "x" }, { aliases: ["a"] }, { tags: ["t"] }]) rejects([fact(extra)]);
  });

  it("rejects non-string or missing fields", () => {
    rejects([fact({ value: 42 })]);
    rejects([{ category: "identity", key: "Full Name" }]);
    rejects([fact({ key: null })]);
    rejects(["text"]);
    rejects([fact({ value: "  " })]);
  });

  it("rejects a bad category", () => {
    rejects([fact({ category: "secrets" })]);
  });

  it("rejects over-length key and value", () => {
    rejects([fact({ key: "k".repeat(MAX_KEY_CHARS + 1) })]);
    rejects([fact({ value: "v".repeat(MAX_VALUE_CHARS + 1) })]);
  });

  it("rejects sensitive keys and values", () => {
    rejects([fact({ key: "Password", value: "hunter2" })]);
    rejects([fact({ key: "Tax ID", value: "123-45-6789" })]);
    rejects([fact({ key: "Payment", value: "4111 1111 1111 1111" })]);
  });

  it("accepts a non-Luhn long number and the name Lessner", () => {
    expect(parse([fact({ key: "Phone", category: "contact", value: "1234567890123" })]).ok).toBe(true);
    expect(parse([fact({ value: "Jane Lessner" })]).ok).toBe(true);
  });

  it("is all-or-nothing", () => {
    const r = rejects([fact(), fact({ key: "Password", value: "x" })]);
    expect(r.facts).toBeUndefined();
  });
});

describe("buildVaultPrompt", () => {
  it("lists every category and warns about secrets", () => {
    const p = buildVaultPrompt();
    for (const c of PROFILE_CATEGORIES) expect(p).toContain(`"${c}"`);
    expect(p).toMatch(/NEVER include passwords/);
  });

  it("default output is unchanged", () => {
    const sha = (t) => createHash("sha256").update(t).digest("hex");
    expect(sha(buildVaultPrompt())).toBe("b30dd4af90d093a5303e7ad368d6282921c0199b80cc1f35d16feb051cb37fe7");
    expect(buildVaultPrompt({ existing: [] })).toBe(buildVaultPrompt());
  });

  it("lists existing field names but never values", () => {
    const vault = [
      { category: "identity", key: "Full Name", value: "Jane Example" },
      { category: "social", key: "GitHub", value: "https://github.com/jane" }
    ];
    const p = buildVaultPrompt({ existing: existingKeysForPrompt(vault) });
    expect(p).toContain("Already in my profile (field names only, values omitted):");
    expect(p).toContain("- identity: Full Name");
    expect(p).toContain("- social: GitHub");
    expect(p).toMatch(/empty JSON array \[\]/);
    expect(p).not.toContain("Jane Example");
    expect(p).not.toContain("github.com/jane");
  });
});

describe("existingKeysForPrompt", () => {
  it("dedupes, trims, and skips sensitive keys", () => {
    const out = existingKeysForPrompt([
      { category: "identity", key: " Full Name ", value: "a" },
      { category: "identity", key: "full name", value: "b" },
      { category: "identity", key: "Password", value: "x" },
      { category: "identity", key: "SSN", value: "x" },
      { category: "identity", key: "", value: "x" },
      null
    ]);
    expect(out).toEqual([{ category: "identity", key: "Full Name" }]);
  });

  it("caps the list and key length", () => {
    const vault = Array.from({ length: MAX_EXISTING_KEYS + 20 }, (_, i) => ({ category: "other", key: `Field ${i}`, value: "v" }));
    expect(existingKeysForPrompt(vault)).toHaveLength(MAX_EXISTING_KEYS);
    const long = existingKeysForPrompt([{ category: "other", key: "k".repeat(MAX_KEY_CHARS + 50), value: "v" }]);
    expect(long[0].key).toHaveLength(MAX_KEY_CHARS);
  });
});

describe("onboarding queue then approve", () => {
  it("queues in pendingDeltas, leaves the vault alone, then approveAll writes entries", async () => {
    const store = new Map();
    const listeners = [];
    const chromeMock = {
      storage: {
        local: {
          get: async (keys) => Object.fromEntries([].concat(keys).filter((k) => store.has(k)).map((k) => [k, store.get(k)])),
          set: async (o) => Object.entries(o).forEach(([k, v]) => store.set(k, v)),
          remove: async (keys) => [].concat(keys).forEach((k) => store.delete(k))
        }
      },
      sidePanel: { setPanelBehavior: async () => {} },
      runtime: {
        onInstalled: { addListener() {} },
        onStartup: { addListener() {} },
        onMessage: { addListener: (fn) => listeners.push(fn) },
        sendMessage: async () => {}
      },
      action: { setBadgeText() {}, setBadgeBackgroundColor() {} }
    };
    vi.resetModules();
    vi.stubGlobal("chrome", chromeMock);
    await import("../extension/service-worker.js");
    const send = (message) =>
      new Promise((resolve) => {
        listeners[0](message, { tab: { id: 1 } }, resolve);
      });

    const parsed = parse([fact(), fact({ category: "contact", key: "Email", value: "jane@example.com" })]);
    expect(parsed.ok).toBe(true);
    const deltas = parsed.facts.map((f, i) => ({
      deltaId: `d${i}`,
      action: "create",
      category: f.category,
      key: f.key,
      proposedValue: f.proposedValue,
      evidenceSnippet: "Pasted from my AI chatbot",
      sourceUrl: "",
      confidence: 0.92
    }));
    await send({ type: "delta:queue", deltas });
    expect(store.get("pendingDeltas")).toHaveLength(2);
    expect(store.get("userContextVault") || []).toHaveLength(0);

    await send({ type: "delta:approveAll" });
    const vault = store.get("userContextVault");
    expect(vault).toHaveLength(2);
    for (const e of vault) {
      expect(e.id).toBeTruthy();
      expect(Array.isArray(e.aliases)).toBe(true);
      expect(Array.isArray(e.tags)).toBe(true);
      expect(e.updatedAt).toBeTruthy();
    }
    expect(store.get("pendingDeltas") || []).toHaveLength(0);
  });
});

describe("sidepanel wiring", () => {
  it("html has every id the onboarding JS looks up", () => {
    const ids = [...panelSrc.matchAll(/getElementById\("(vault-onboarding[\w-]*|btn-copy-vault-prompt|btn-vault-onboarding-import)"\)/g)].map((m) => m[1]);
    expect(ids.length).toBeGreaterThanOrEqual(6);
    for (const id of new Set(ids)) expect(html).toContain(`id="${id}"`);
  });

  it("the card is always present as a details element with the include checkbox", () => {
    expect(html).toMatch(/<details class="card" id="vault-onboarding-card">/);
    expect(html).not.toMatch(/id="vault-onboarding-card"[^>]*display:\s*none/);
    expect(html).toContain('id="vault-onboarding-include-existing"');
    expect(html).toContain("Include my existing field names so the chatbot skips them");
  });
});

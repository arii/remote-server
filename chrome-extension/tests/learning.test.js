import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  MAX_LEARNING_CASES,
  aggregateProposals,
  casesFromDeltas,
  parseRefinement,
  promoteAlias,
  recordCase,
  verifyProposal
} from "../extension/lib/learning.js";
import { SENSITIVE_PATTERN, isSensitiveLabel } from "../extension/lib/sensitive.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(path.join(here, p), "utf8");

const phone = { id: "identity.phone", key: "Phone", value: "555-0100", aliases: ["phone"] };
const mobile = { id: "identity.mobile", key: "Mobile", value: "555-0199", aliases: ["mobile"] };
const vault = [phone, mobile];
const approved = (label, entryId = "identity.phone") => ({ label, entryId, entryKey: "Phone", outcome: "approved" });

describe("sensitive guardrail", () => {
  it("matches the classic content-script pattern exactly", () => {
    const m = read("../extension/content-script.js").match(/SENSITIVE_PATTERNS = \/(.+)\/i;/);
    expect(m[1]).toBe(SENSITIVE_PATTERN.source);
  });
  it("flags password/cvv/ssn", () => {
    expect(isSensitiveLabel("Your Password")).toBe(true);
    expect(isSensitiveLabel("SSN")).toBe(true);
    expect(isSensitiveLabel("Phone")).toBe(false);
  });
});

describe("recordCase", () => {
  it("caps at 200 with FIFO eviction", () => {
    let cases = [];
    for (let i = 0; i < MAX_LEARNING_CASES + 5; i++) cases = recordCase(cases, approved(`label ${i}`));
    expect(cases).toHaveLength(200);
    expect(cases[0].label).toBe("label 5");
    expect(cases.at(-1).label).toBe("label 204");
  });
  it("never records sensitive labels or sensitive entry keys", () => {
    expect(recordCase([], approved("Account Password"))).toEqual([]);
    expect(recordCase([], { ...approved("Number"), entryKey: "Card-Number" })).toEqual([]);
  });
  it("skips unknown outcomes and cases without entry or label", () => {
    expect(recordCase([], { ...approved("x"), outcome: "weird" })).toEqual([]);
    expect(recordCase([], { label: "x", outcome: "approved" })).toEqual([]);
    expect(recordCase([], approved("   "))).toEqual([]);
  });
  it("derives cases only from deltas with a label and target entry", () => {
    const cases = casesFromDeltas(
      [
        { fieldLabel: "Cell", targetEntryId: "identity.phone" },
        { fieldLabel: "No entry" },
        { targetEntryId: "identity.phone" }
      ],
      "approved",
      vault
    );
    expect(cases).toHaveLength(1);
    expect(cases[0]).toMatchObject({ label: "Cell", entryId: "identity.phone", entryKey: "Phone", outcome: "approved" });
  });
});

describe("aggregateProposals", () => {
  it("proposes only at the threshold", () => {
    const one = [approved("Cell number")];
    expect(aggregateProposals(one, vault)).toEqual([]);
    const two = [approved("Cell number"), approved("cell  number")];
    expect(aggregateProposals(two, vault)).toEqual([
      { label: "cell number", entryId: "identity.phone", alias: "cell number", count: 2 }
    ]);
  });
  it("skips labels that already resolve to the entry and sensitive labels", () => {
    expect(aggregateProposals([approved("Phone"), approved("Phone")], vault)).toEqual([]);
    expect(aggregateProposals([approved("cvv"), approved("cvv")], vault)).toEqual([]);
  });
});

describe("verifyProposal", () => {
  const cases = [approved("Cell number"), approved("Cell number"), approved("Phone", "identity.phone")];
  const proposal = { label: "cell number", entryId: "identity.phone", alias: "cell number" };

  it("accepts a proposal that fixes a failure and breaks nothing", () => {
    expect(verifyProposal(proposal, cases, vault)).toEqual({ ok: true, fixed: 2 });
  });
  it("rejects a proposal that fixes no recorded failure", () => {
    expect(verifyProposal(proposal, [], vault).reason).toBe("fixes-no-recorded-failure");
  });
  it("rejects when the label already matches a different entry (conflict)", () => {
    const v = [phone, { ...mobile, aliases: ["mobile", "cell number"] }];
    expect(verifyProposal(proposal, cases, v).reason).toBe("conflict-label-matches-other-entry");
  });
  it("rejects when the alias collides with another entry's key", () => {
    const v = [phone, { ...mobile, key: "Cell Number" }];
    // resolves to the other entry first
    expect(verifyProposal(proposal, cases, v).ok).toBe(false);
  });
  it("rejects when it would break a recorded success", () => {
    // "contact cell number" currently resolves to the later entry via word-boundary alias.
    const later = { id: "identity.cellphone", key: "Cell phone", value: "x", aliases: ["contact cell number"] };
    const v = [phone, later];
    const longProposal = { label: "contact cell number", entryId: "identity.phone", alias: "contact cell number" };
    const c = [
      { ...approved("contact cell number"), outcome: "approved" },
      { label: "contact cell number", entryId: "identity.cellphone", entryKey: "Cell phone", outcome: "fill-pass" }
    ];
    expect(verifyProposal(longProposal, c, v).ok).toBe(false);
  });
  it("rejects when it would resolve a label the user discarded for that entry", () => {
    const c = [...cases, { label: "cell number", entryId: "identity.phone", entryKey: "Phone", outcome: "discarded" }];
    expect(verifyProposal(proposal, c, vault).reason).toBe("breaks-discarded-case");
  });
  it("rejects sensitive, empty, unknown-entry and non-label aliases", () => {
    expect(verifyProposal({ ...proposal, label: "password", alias: "password" }, cases, vault).reason).toBe("sensitive");
    expect(verifyProposal({ ...proposal, alias: "" }, cases, vault).reason).toBe("empty-label");
    expect(verifyProposal({ ...proposal, entryId: "nope" }, cases, vault).reason).toBe("unknown-entry");
    expect(verifyProposal({ ...proposal, alias: "something else" }, cases, vault).reason).toBe("alias-not-label-form");
  });
});

describe("promoteAlias", () => {
  it("adds only the alias; value and key unchanged; idempotent", () => {
    const next = promoteAlias(vault, { entryId: "identity.phone", alias: "Cell Number" });
    const e = next.find((x) => x.id === "identity.phone");
    expect(e.aliases).toEqual(["phone", "cell number"]);
    expect(e.value).toBe("555-0100");
    expect(e.key).toBe("Phone");
    expect(next.find((x) => x.id === "identity.mobile")).toBe(mobile);
    expect(promoteAlias(next, { entryId: "identity.phone", alias: "cell number" })).toEqual(next);
  });

  it("stamps updatedAt on the changed entry so Drive sync sees the new alias", () => {
    const e = promoteAlias(vault, { entryId: "identity.phone", alias: "Cell Number" }).find((x) => x.id === "identity.phone");
    expect(Date.parse(e.updatedAt)).toBeGreaterThan(Date.parse("2026-01-01"));
  });
});

describe("parseRefinement (untrusted LLM output)", () => {
  const offered = [{ label: "cell number *", entryId: "identity.phone", alias: "cell number *", count: 2 }];
  const cases = [approved("cell number *"), approved("cell number *")];
  const ok = JSON.stringify({ proposals: [{ entryId: "identity.phone", alias: "Cell number" }] });

  it("accepts a clean, verified refinement", () => {
    const r = parseRefinement(ok, offered, cases, vault);
    expect(r).toHaveLength(1);
    expect(r[0].alias).toBe("cell number");
  });
  it("rejects bad JSON, extra keys, unknown entryId, wrong shape, non-label alias", () => {
    expect(parseRefinement("not json", offered, cases, vault)).toBeNull();
    expect(parseRefinement('{"proposals":[],"x":1}', offered, cases, vault)).toBeNull();
    expect(parseRefinement('{"proposals":[{"entryId":"identity.phone","alias":"cell number","extra":1}]}', offered, cases, vault)).toBeNull();
    expect(parseRefinement('{"proposals":[{"entryId":"identity.mobile","alias":"cell number"}]}', offered, cases, vault)).toBeNull();
    expect(parseRefinement('{"proposals":[{"entryId":"identity.phone","alias":"home address"}]}', offered, cases, vault)).toBeNull();
    expect(parseRefinement("[]", offered, cases, vault)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Worker: record on approve/discard, promote re-verifies, panel cannot bypass
// ---------------------------------------------------------------------------
function makeMock() {
  const store = new Map();
  const listeners = { message: [] };
  const chromeMock = {
    storage: {
      local: {
        get: async (keys) => Object.fromEntries([].concat(keys).filter((k) => store.has(k)).map((k) => [k, store.get(k)])),
        set: async (o) => Object.entries(o).forEach(([k, v]) => store.set(k, v)),
        remove: async (k) => [].concat(k).forEach((x) => store.delete(x))
      }
    },
    sidePanel: { setPanelBehavior: async () => {} },
    runtime: {
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      onMessage: { addListener: (fn) => listeners.message.push(fn) }
    },
    action: { setBadgeText() {}, setBadgeBackgroundColor() {} }
  };
  return { chromeMock, store, listeners };
}
const send = (listener, message) => new Promise((resolve) => listener(message, { tab: { id: 1 } }, resolve));

async function boot(deltas) {
  const mock = makeMock();
  mock.store.set("userContextVault", JSON.parse(JSON.stringify(vault)));
  mock.store.set("pendingDeltas", deltas);
  vi.resetModules();
  vi.stubGlobal("chrome", mock.chromeMock);
  await import("../extension/service-worker.js");
  return { mock, listener: mock.listeners.message[0] };
}
const delta = (id, label) => ({
  deltaId: id,
  action: "update",
  targetEntryId: "identity.phone",
  key: "Phone",
  fieldLabel: label,
  proposedValue: `v-${id}`
});

describe("worker learning flow", () => {
  it("records approve/discard cases, proposes, and promotes alias only", async () => {
    const { mock, listener } = await boot([delta("d1", "Cell number"), delta("d2", "Cell number"), delta("d3", "Fax")]);
    await send(listener, { type: "delta:approve", deltaId: "d1" });
    await send(listener, { type: "delta:approve", deltaId: "d2" });
    await send(listener, { type: "delta:discard", deltaId: "d3" });
    const cases = mock.store.get("learningCases");
    expect(cases.map((c) => c.outcome)).toEqual(["approved", "approved", "discarded"]);

    const { proposals } = await send(listener, { type: "learning:getProposals" });
    expect(proposals).toHaveLength(1);
    const before = mock.store.get("userContextVault").find((e) => e.id === "identity.phone");
    const res = await send(listener, { type: "learning:promote", proposal: proposals[0] });
    expect(res.ok).toBe(true);
    const after = mock.store.get("userContextVault").find((e) => e.id === "identity.phone");
    expect(after.aliases).toContain("cell number");
    expect(after.value).toBe(before.value);
    expect(after.key).toBe(before.key);
    expect(mock.store.get("learnedAliases")[0]).toMatchObject({ alias: "cell number", entryId: "identity.phone" });
  });

  it("rejects a forged promote that conflicts with a different entry", async () => {
    const { mock, listener } = await boot([delta("d1", "Mobile"), delta("d2", "Mobile")]);
    await send(listener, { type: "delta:approveAll" });
    const res = await send(listener, {
      type: "learning:promote",
      proposal: { label: "mobile", entryId: "identity.phone", alias: "mobile" }
    });
    expect(res.ok).toBe(false);
    expect(res.reason).toBe("conflict-label-matches-other-entry");
    expect(mock.store.get("userContextVault").find((e) => e.id === "identity.phone").aliases).toEqual(["phone"]);
  });

  it("never records sensitive labels from deltas or verdicts", async () => {
    const { mock, listener } = await boot([delta("d1", "Account password")]);
    await send(listener, { type: "delta:approve", deltaId: "d1" });
    await send(listener, { type: "learning:recordVerdicts", verdicts: [{ label: "cvv", entryId: "identity.phone", ok: false }] });
    expect(mock.store.get("learningCases") || []).toEqual([]);
  });

  it("records verdicts attributed to a vault entry", async () => {
    const { mock, listener } = await boot([]);
    await send(listener, {
      type: "learning:recordVerdicts",
      verdicts: [
        { label: "Phone", entryId: "identity.phone", ok: true },
        { label: "Phone", entryId: "ghost", ok: false }
      ]
    });
    expect(mock.store.get("learningCases").map((c) => c.outcome)).toEqual(["fill-pass"]);
  });
});

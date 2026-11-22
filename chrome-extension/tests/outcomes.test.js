import { describe, expect, it, vi } from "vitest";
import {
  MAX_FILL_OUTCOMES,
  buildOutcomeRecords,
  classifyOutcome,
  fillStats,
  recordOutcomes,
  remapTarget,
  shouldRecord
} from "../extension/lib/outcomes.js";

const vault = [
  { id: "identity.email", key: "Email", value: "ada@example.test" },
  { id: "identity.phone", key: "Phone", value: "555-0100" },
  { id: "secret.pw", key: "Password", value: "hunter2-synthetic" }
];

const field = (over = {}) => ({
  sig: { label: "Email", name: "email", autocomplete: "email", type: "email" },
  filled: "ada@example.test",
  submitted: "ada@example.test",
  entryKey: "Email",
  entryId: "identity.email",
  pass: "autocomplete",
  confidence: 0.99,
  ...over
});

describe("classifyOutcome", () => {
  it("kept, edited, cleared", () => {
    expect(classifyOutcome("a@b.co", "a@b.co")).toBe("kept");
    expect(classifyOutcome("a@b.co", "c@d.co")).toBe("edited");
    expect(classifyOutcome("a@b.co", "")).toBe("cleared");
    expect(classifyOutcome("a@b.co", "   ")).toBe("cleared");
  });
  it("whitespace and case only differences are kept", () => {
    expect(classifyOutcome("Ada  Lovelace", " ada lovelace ")).toBe("kept");
  });
});

describe("remapTarget", () => {
  it("finds a different entry with the same value", () => {
    expect(remapTarget("555-0100", vault, "identity.email")).toBe("identity.phone");
  });
  it("excludes the filled entry, empty values, and sensitive entries", () => {
    expect(remapTarget("ada@example.test", vault, "identity.email")).toBeNull();
    expect(remapTarget("", vault, "x")).toBeNull();
    expect(remapTarget("hunter2-synthetic", vault, "x")).toBeNull();
  });
});

describe("shouldRecord", () => {
  it("records only for allowlisted hosts", () => {
    expect(shouldRecord("a.test", ["a.test"])).toBe(true);
    expect(shouldRecord("b.test", ["a.test"])).toBe(false);
    expect(shouldRecord("a.test", undefined)).toBe(false);
    expect(shouldRecord("", [""])).toBe(false);
  });
});

describe("buildOutcomeRecords", () => {
  it("stores classes and ids, never values", () => {
    const recs = buildOutcomeRecords(
      {
        fillId: "f1",
        host: "a.test",
        fields: [field(), field({ filled: "ada@example.test", submitted: "555-0100" }), field({ submitted: "" })]
      },
      vault,
      1
    );
    expect(recs.map((r) => r.outcome)).toEqual(["kept", "edited", "cleared"]);
    expect(recs[1].remappedEntryId).toBe("identity.phone");
    const json = JSON.stringify(recs);
    for (const v of ["ada@example.test", "555-0100", "hunter2-synthetic"]) expect(json).not.toContain(v);
  });
  it("skips sensitive and unknown-pass fields", () => {
    const recs = buildOutcomeRecords(
      {
        fillId: "f1",
        host: "a.test",
        fields: [
          field({ sig: { label: "Password", name: "pw", autocomplete: "", type: "text" } }),
          field({ sig: { label: "x", name: "x", autocomplete: "", type: "password" } }),
          field({ pass: "guess" })
        ]
      },
      vault
    );
    expect(recs).toEqual([]);
  });
  it("rejects malformed messages", () => {
    expect(buildOutcomeRecords({ fillId: "", host: "a", fields: [] }, vault)).toEqual([]);
    expect(buildOutcomeRecords({ fillId: "f", host: "a", fields: "no" }, vault)).toEqual([]);
  });
});

describe("recordOutcomes cap", () => {
  it("drops the oldest beyond the cap", () => {
    const mk = (n) => Array.from({ length: n }, (_, i) => ({ fillId: `f${i}`, outcome: "kept" }));
    const next = recordOutcomes(mk(MAX_FILL_OUTCOMES), [{ fillId: "new", outcome: "kept" }]);
    expect(next).toHaveLength(MAX_FILL_OUTCOMES);
    expect(next[0].fillId).toBe("f1");
    expect(next.at(-1).fillId).toBe("new");
  });
});

describe("fillStats", () => {
  it("is empty without data", () => {
    expect(fillStats([])).toEqual({ last: null, keptRate: null, fills: 0 });
  });
  it("counts the last fill and the kept-rate over 20 distinct fills", () => {
    const rows = [];
    for (let i = 0; i < 25; i++) rows.push({ fillId: `f${i}`, outcome: i < 5 ? "edited" : "kept" });
    rows.push({ fillId: "f24", outcome: "edited" }, { fillId: "f24", outcome: "cleared" });
    const s = fillStats(rows);
    expect(s.last).toEqual({ kept: 1, edited: 1, cleared: 1 });
    expect(s.fills).toBe(20);
    // f5..f24: 20 fills, 22 rows, 19 kept(f5..f23)+1 kept(f24)=20 kept
    expect(s.keptRate).toBeCloseTo(20 / 22);
  });
});

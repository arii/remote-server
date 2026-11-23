import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  MAX_MAPPING_HOSTS,
  MAX_MAPPINGS,
  QUARANTINE_CLEAN_USES,
  applyOutcomeToMappings,
  confirmMapping,
  learnFromOutcomes,
  learnedRows,
  removeMapping
} from "../extension/lib/mappings.js";
import { fieldSignature, lookupMapping, matchField } from "../extension/lib/matcher.js";
import { runReplay } from "../eval/run-eval.mjs";

const vault = [
  { id: "billing.zip", key: "Billing ZIP", value: "N1 0AA", aliases: ["billing zip"] },
  { id: "shipping.zip", key: "Shipping ZIP", value: "LS1 0AA", aliases: ["zip code"] },
  { id: "secret.pw", key: "Password", value: "hunter2-synthetic" }
];
const snapshot = JSON.stringify(vault);

const sigObj = { label: "Zip code", name: "zip", autocomplete: "", type: "text" };
const SIG = fieldSignature(sigObj);
const rec = (over = {}) => ({
  fillId: "f1",
  ts: 1,
  host: "a.example.test",
  sig: sigObj,
  entryKey: "Shipping ZIP",
  pass: "alias",
  confidence: 0.98,
  outcome: "edited",
  remappedEntryId: "billing.zip",
  ...over
});
const field = { label: "Zip code", name: "zip", autocomplete: "", type: "text" };

// A quarantined mapping from one correction on host a.
const learn = () => learnFromOutcomes([], [rec()], vault, 10);
const kept = (over = {}) => rec({ outcome: "kept", entryKey: "Billing ZIP", remappedEntryId: null, ...over });

describe("learning from a correction", () => {
  it("starts quarantined and stores ids, keys and counters only", () => {
    const [m] = learn();
    expect(m).toMatchObject({ sig: SIG, entryId: "billing.zip", entryKey: "Billing ZIP", status: "quarantined", cleanUses: 0 });
    expect(m.hosts).toEqual(["a.example.test"]);
    const text = JSON.stringify(m);
    expect(text).not.toContain("N1 0AA");
    expect(text).not.toContain("LS1 0AA");
  });

  it("does not change the vault", () => {
    learnFromOutcomes([], [rec(), kept(), kept()], vault, 10);
    expect(JSON.stringify(vault)).toBe(snapshot);
  });

  it("never stores sensitive signatures or keys", () => {
    const pw = { label: "Password", name: "pw", autocomplete: "", type: "text" };
    expect(learnFromOutcomes([], [rec({ sig: pw })], vault)).toEqual([]);
    expect(learnFromOutcomes([], [rec({ sig: { ...sigObj, type: "password" } })], vault)).toEqual([]);
    expect(learnFromOutcomes([], [rec({ remappedEntryId: "secret.pw" })], vault)).toEqual([]);
  });

  it("caps hosts and records", () => {
    let list = learn();
    for (let i = 0; i < 15; i++) list = applyOutcomeToMappings(list, rec({ host: `h${i}.example.test` }), vault, 20 + i);
    expect(list).toHaveLength(1);
    expect(list[0].hosts).toHaveLength(MAX_MAPPING_HOSTS);
    let many = [];
    for (let i = 0; i < MAX_MAPPINGS + 5; i++) {
      many = applyOutcomeToMappings(many, rec({ sig: { ...sigObj, name: `z${i}` } }), vault, i);
    }
    expect(many).toHaveLength(MAX_MAPPINGS);
    expect(many.some((m) => m.sig.includes("|z0|"))).toBe(false);
  });
});

describe("quarantine", () => {
  it("a quarantined mapping is not applied before confirmation", () => {
    const m = learn();
    expect(lookupMapping(m, SIG, "a.example.test")).toBeNull();
    const match = matchField(field, vault, { learned: m, host: "a.example.test" });
    expect(match.entry.id).toBe("shipping.zip");
    expect(match.pass).toBe("alias");
  });

  it("counts clean uses without applying, confirms at the threshold", () => {
    let m = learn();
    for (let i = 1; i < QUARANTINE_CLEAN_USES; i++) {
      m = applyOutcomeToMappings(m, kept(), vault, 20 + i);
      expect(m[0]).toMatchObject({ status: "quarantined", cleanUses: i });
      expect(matchField(field, vault, { learned: m, host: "a.example.test" }).pass).toBe("alias");
    }
    m = applyOutcomeToMappings(m, kept(), vault, 30);
    expect(m[0].status).toBe("confirmed");
    expect(matchField(field, vault, { learned: m, host: "a.example.test" })).toMatchObject({ pass: "learned" });
  });

  it("re-correcting to the mapped entry counts and confirms at the threshold", () => {
    let m = learn();
    for (let i = 1; i < QUARANTINE_CLEAN_USES; i++) {
      m = applyOutcomeToMappings(m, rec(), vault, 20 + i);
      expect(m).toHaveLength(1);
      expect(m[0]).toMatchObject({ status: "quarantined", cleanUses: i });
      expect(matchField(field, vault, { learned: m, host: "a.example.test" }).pass).toBe("alias");
    }
    m = applyOutcomeToMappings(m, rec(), vault, 30);
    expect(m[0]).toMatchObject({ status: "confirmed", entryId: "billing.zip" });
    expect(matchField(field, vault, { learned: m, host: "a.example.test" })).toMatchObject({ pass: "learned" });
  });

  it("a kept fill with a different key is not a clean use", () => {
    const m = applyOutcomeToMappings(learn(), kept({ entryKey: "Shipping ZIP" }), vault, 20);
    expect(m[0].cleanUses).toBe(0);
  });

  it("a clean use on another site does not count", () => {
    const m = applyOutcomeToMappings(learn(), kept({ host: "b.example.test" }), vault, 20);
    expect(m[0].cleanUses).toBe(0);
  });

  it("Confirm makes it usable and beats an alias", () => {
    const m = confirmMapping(learn(), SIG);
    const match = matchField(field, vault, { learned: m, host: "a.example.test" });
    expect(match).toMatchObject({ pass: "learned", confidence: 0.995 });
    expect(match.entry.id).toBe("billing.zip");
  });

  it("ignores a mapping whose vault entry is gone", () => {
    const m = confirmMapping(learn(), SIG);
    const match = matchField(field, vault.filter((e) => e.id !== "billing.zip"), { learned: m, host: "a.example.test" });
    expect(match.pass).toBe("alias");
  });
});

describe("demotion on first overwrite", () => {
  it("deletes a quarantined mapping that would have applied", () => {
    const m = applyOutcomeToMappings(learn(), rec({ outcome: "edited", entryKey: "Billing ZIP", remappedEntryId: null }), vault, 20);
    expect(m).toEqual([]);
  });

  it("deletes a confirmed mapping on edit or clear", () => {
    const confirmed = confirmMapping(learn(), SIG);
    const edited = applyOutcomeToMappings(confirmed, rec({ pass: "learned", entryKey: "Billing ZIP", remappedEntryId: null }), vault, 20);
    expect(edited).toEqual([]);
    const cleared = applyOutcomeToMappings(confirmed, rec({ outcome: "cleared", pass: "learned", entryKey: "Billing ZIP", remappedEntryId: null }), vault, 20);
    expect(cleared).toEqual([]);
  });

  it("re-learning from the same correction starts in quarantine again", () => {
    const confirmed = confirmMapping(learn(), SIG);
    const gone = applyOutcomeToMappings(confirmed, rec({ outcome: "cleared", pass: "learned", entryKey: "Billing ZIP", remappedEntryId: null }), vault, 20);
    const again = applyOutcomeToMappings(gone, rec(), vault, 30);
    expect(again[0]).toMatchObject({ status: "quarantined", cleanUses: 0 });
  });

  it("an overwrite on a site where the mapping does not apply leaves it", () => {
    const m = applyOutcomeToMappings(confirmMapping(learn(), SIG), rec({ host: "b.example.test", outcome: "cleared", remappedEntryId: null }), vault, 20);
    expect(m).toHaveLength(1);
    expect(m[0].status).toBe("confirmed");
  });
});

describe("a correction on a site where the mapping did not apply", () => {
  it("does not replace or demote a confirmed mapping learned elsewhere", () => {
    const confirmed = confirmMapping(learn(), SIG);
    const other = applyOutcomeToMappings(confirmed, rec({ host: "b.example.test", remappedEntryId: "shipping.zip", entryKey: "Billing ZIP" }), vault, 20);
    expect(other).toEqual(confirmed);
  });

  it("does not replace a quarantined mapping learned elsewhere", () => {
    const q = learn();
    const other = applyOutcomeToMappings(q, rec({ host: "b.example.test", remappedEntryId: "shipping.zip", entryKey: "Billing ZIP" }), vault, 20);
    expect(other).toEqual(q);
  });
});

describe("cross-site promotion", () => {
  it("applies on its own site only until a second site repeats the correction", () => {
    let m = confirmMapping(learn(), SIG);
    expect(matchField(field, vault, { learned: m, host: "a.example.test" }).pass).toBe("learned");
    expect(matchField(field, vault, { learned: m, host: "b.example.test" }).pass).toBe("alias");
    m = applyOutcomeToMappings(m, rec({ host: "b.example.test" }), vault, 20);
    expect(m).toHaveLength(1);
    expect(m[0].hosts).toEqual(["a.example.test", "b.example.test"]);
    expect(matchField(field, vault, { learned: m, host: "c.example.test" }).pass).toBe("learned");
  });

  it("a quarantined mapping on two sites still does not apply", () => {
    const m = applyOutcomeToMappings(learn(), rec({ host: "b.example.test" }), vault, 20);
    expect(m[0].status).toBe("quarantined");
    expect(matchField(field, vault, { learned: m, host: "c.example.test" }).pass).toBe("alias");
  });
});

describe("Delete and the flat list", () => {
  it("Delete removes the mapping", () => {
    expect(removeMapping(learn(), SIG)).toEqual([]);
  });

  it("rows merge aliases (added by you) and mappings with origin text", () => {
    const rows = learnedRows(
      [{ alias: "postcode", entryId: "billing.zip", entryKey: "Billing ZIP" }],
      [...learn(), ...confirmMapping(learn(), SIG).map((m) => ({ ...m, sig: "other|x||text" }))]
    );
    expect(rows.map((r) => r.origin)).toEqual(["added by you", "learned, unconfirmed", "learned from your edit"]);
    expect(rows.map((r) => r.unconfirmed)).toEqual([false, true, false]);
    expect(rows[1]).toMatchObject({ from: "zip code", to: "Billing ZIP", kind: "mapping" });
    for (const r of rows) expect(Object.keys(r).join()).not.toMatch(/hits|confidence|cleanUses/);
  });
});

describe("corrections replay (eval)", () => {
  it("accuracy rises only after mappings are confirmed", () => {
    const r = runReplay();
    expect(r.quarantined.total).toEqual(r.before.total);
    expect(r.after.total.accuracy).toBeGreaterThan(r.before.total.accuracy);
    expect(r.after.total.wrongFill).toBe(0);
    expect(r.confirmed).toBe(r.corrections);
  });
});

describe("source guards", () => {
  it("lib/mappings.js uses no chrome.* APIs", () => {
    const src = readFileSync(join(import.meta.dirname, "..", "extension", "lib", "mappings.js"), "utf8");
    expect(src.replace(/^\s*\/\/.*$/gm, "")).not.toMatch(/chrome\./);
  });
});

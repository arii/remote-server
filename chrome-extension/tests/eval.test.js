// Fill-accuracy baseline gate (#1538). runEval is the same code `npm run eval`
// runs; eval/last-run.json is the committed baseline. A change that lowers
// accuracy (a broken alias, a matcher regression) fails here. When accuracy
// improves, run `npm run eval` and commit the new last-run.json.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { CASE_TYPES, LAST_RUN_PATH, VAULT_PATH, readBaseline, runEval, runReplay, scoreOutcome } from "../eval/run-eval.mjs";

describe("fill accuracy eval", () => {
  it("accuracy is at least the committed baseline", () => {
    const run = runEval();
    expect(run.total.expected).toBeGreaterThan(0);
    expect(run.total.accuracy).toBeGreaterThanOrEqual(readBaseline().total.accuracy);
  });

  it("is deterministic and matches the committed last-run.json", () => {
    const { replay, ...base } = JSON.parse(readFileSync(LAST_RUN_PATH, "utf8"));
    expect(runEval()).toEqual(base);
    expect(runReplay()).toEqual(replay);
  });

  it("a deliberately broken alias drops accuracy below baseline", () => {
    const vault = JSON.parse(readFileSync(VAULT_PATH, "utf8"));
    const entry = vault.find((e) => e.id === "basics.email");
    entry.aliases = entry.aliases.map((a) => (a === "email" ? "zzz-broken" : a));
    const broken = runEval({ vault });
    expect(broken.total.accuracy).toBeLessThan(readBaseline().total.accuracy);
  });

  it("scores the four outcomes", () => {
    expect(scoreOutcome("A", "A")).toBe("correctFill");
    expect(scoreOutcome("A", "B")).toBe("wrongFill");
    expect(scoreOutcome("A", null)).toBe("wrongAbstain");
    expect(scoreOutcome(null, null)).toBe("correctAbstain");
    expect(scoreOutcome(null, "A")).toBe("wrongFill");
  });

  it("counts a correct abstain as correct", () => {
    const run = runEval();
    const abstain = run.results.filter((r) => r.type === "abstain" && r.expected === null);
    expect(abstain.length).toBeGreaterThan(0);
    for (const r of abstain) {
      expect(r.outcome).toBe("correctAbstain");
      expect(r.ok).toBe(true);
    }
    expect(run.total.correctAbstain).toBeGreaterThan(0);
  });

  it("penalizes filling a field whose correct answer is blank", () => {
    const base = runEval();
    const vault = JSON.parse(readFileSync(VAULT_PATH, "utf8"));
    vault.push({ id: "bad.color", key: "Bad Color", value: "x", aliases: ["favorite color"] });
    const run = runEval({ vault });
    const hit = run.results.find((r) => r.type === "abstain" && r.actual === "Bad Color");
    expect(hit.outcome).toBe("wrongFill");
    expect(run.total.wrongFill).toBe(base.total.wrongFill + 1);
    expect(run.byType.abstain.accuracy).toBeLessThan(base.byType.abstain.accuracy);
    expect(run.total.accuracy).toBeLessThan(base.total.accuracy);
  });

  it("covers every case type with at least one case", () => {
    const run = runEval();
    for (const type of CASE_TYPES) {
      expect(run.byType[type]?.expected, type).toBeGreaterThan(0);
    }
  });

  it("injects stepped fields only when their step is processed", () => {
    const run = runEval();
    const steps = new Set(run.results.filter((r) => r.type === "dynamic").map((r) => r.step));
    expect([...steps].sort()).toEqual([1, 2, 3]);
  });

  it("fixtures and the eval vault hold no personal data", () => {
    const dir = join(import.meta.dirname, "fixtures");
    const files = readdirSync(dir).filter((f) => /\.(html|json)$/.test(f));
    const texts = files.map((f) => readFileSync(join(dir, f), "utf8"));
    texts.push(readFileSync(VAULT_PATH, "utf8"));
    const emails = /[\w.+-]+@[\w-]+\.[\w.]+/g;
    const phones = /\+?\d[\d\s().-]{6,}\d/g;
    for (const text of texts) {
      for (const m of text.match(emails) || []) expect(m).toMatch(/@example\.com$/);
      for (const m of text.match(phones) || []) expect(m).toMatch(/^555-01\d\d$/);
    }
  });
});

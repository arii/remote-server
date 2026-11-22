// Offline fill-accuracy eval (#1538). Parses the committed form fixtures with
// happy-dom, labels each control with the shared resolveLabel, and runs the
// shared matchField against a sample vault. Compares the matched vault entry
// key with the `fills` expectations in tests/fixtures/expected-matches.json.
// Scoring (technical_specs.md 4.1): correct fill, wrong fill, correct abstain,
// wrong abstain; accuracy = (correct fill + correct abstain) / total. A fill
// where null (blank) was expected is a wrong fill, not a neutral result.
// No network, no model calls, no timestamps: the output is deterministic so
// eval/last-run.json can be committed as the accuracy baseline.
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { Window } from "happy-dom";
import { fieldSignature, matchField, resolveLabel } from "../extension/lib/matcher.js";

const ROOT = join(import.meta.dirname, "..");
const FIXTURES_DIR = join(ROOT, "tests", "fixtures");
export const VAULT_PATH = join(ROOT, "eval", "eval-vault.json");
export const LAST_RUN_PATH = join(ROOT, "eval", "last-run.json");

const round = (n) => Math.round(n * 10000) / 10000;

export const CASE_TYPES = ["plain", "ambiguous", "mismatch", "dynamic", "abstain"];
export const OUTCOMES = ["correctFill", "wrongFill", "correctAbstain", "wrongAbstain"];

export function scoreOutcome(expected, actual) {
  if (expected === null || expected === undefined) return actual === null ? "correctAbstain" : "wrongFill";
  if (actual === null) return "wrongAbstain";
  return actual === expected ? "correctFill" : "wrongFill";
}

function summarize(results) {
  const counts = Object.fromEntries(OUTCOMES.map((o) => [o, 0]));
  for (const r of results) counts[r.outcome] += 1;
  const expected = results.length;
  const correct = counts.correctFill + counts.correctAbstain;
  return { correct, expected, accuracy: expected ? round(correct / expected) : 0, ...counts };
}

// Fields marked data-step=N simulate a multi-step form: every stepped element
// is detached when the document loads and re-attached to its original parent
// only when the first fill for that step is processed, the way the content
// script re-scans after a step renders new fields.
function detachSteps(doc) {
  const pending = new Map();
  for (const node of [...doc.querySelectorAll("[data-step]")]) {
    const step = Number(node.getAttribute("data-step"));
    const entry = { parent: node.parentElement, node };
    if (!pending.has(step)) pending.set(step, []);
    pending.get(step).push(entry);
    node.remove();
  }
  return pending;
}

function injectStep(pending, step) {
  for (const { parent, node } of pending.get(step) || []) parent.appendChild(node);
  pending.delete(step);
}

function findControl(doc, { id, name }) {
  if (id) return doc.getElementById(id);
  return doc.querySelector(`[name="${String(name).replace(/"/g, '\\"')}"]`);
}

// Returns {total, fixtures, results}. `vault` defaults to eval/eval-vault.json;
// tests pass a modified copy to simulate a regression.
export function runEval({ vault } = {}) {
  const activeVault = vault || JSON.parse(readFileSync(VAULT_PATH, "utf8"));
  const golden = JSON.parse(readFileSync(join(FIXTURES_DIR, "expected-matches.json"), "utf8"));
  const window = new Window();
  // resolveLabel uses the CSS.escape global, which Node lacks.
  const hadCss = "CSS" in globalThis;
  if (!hadCss) globalThis.CSS = window.CSS;
  const docs = new Map();
  const pendingSteps = new Map();
  const results = [];
  try {
    for (const fill of golden.fills || []) {
      if (!docs.has(fill.fixture)) {
        const doc = new window.DOMParser().parseFromString(
          readFileSync(join(FIXTURES_DIR, fill.fixture), "utf8"),
          "text/html"
        );
        docs.set(fill.fixture, doc);
        pendingSteps.set(fill.fixture, detachSteps(doc));
      }
      const doc = docs.get(fill.fixture);
      if (fill.step) injectStep(pendingSteps.get(fill.fixture), fill.step);
      const el = findControl(doc, fill.field);
      if (!el) throw new Error(`eval: no control ${JSON.stringify(fill.field)} in ${fill.fixture}`);
      const label = resolveLabel(el, doc);
      const autocomplete = el.getAttribute("autocomplete") || "";
      const sig = fieldSignature({ label, name: el.getAttribute("name") || "", autocomplete, type: el.type || el.tagName.toLowerCase() });
      const match = matchField({ label, autocomplete }, activeVault);
      const actual = match ? match.entry.key : null;
      const outcome = scoreOutcome(fill.expect, actual);
      results.push({
        fixture: fill.fixture,
        field: fill.field,
        sig,
        type: fill.type || "plain",
        ...(fill.step ? { step: fill.step } : {}),
        expected: fill.expect,
        actual,
        pass: match ? match.pass : null,
        outcome,
        ok: outcome === "correctFill" || outcome === "correctAbstain"
      });
    }
  } finally {
    if (!hadCss) delete globalThis.CSS;
    window.close?.();
  }

  const group = (keyOf) => {
    const groups = {};
    for (const r of results) (groups[keyOf(r)] ||= []).push(r);
    return Object.fromEntries(Object.entries(groups).map(([k, rs]) => [k, summarize(rs)]));
  };
  const fixtures = group((r) => r.fixture);
  const byType = Object.fromEntries(Object.entries(group((r) => r.type)).sort(([a], [b]) => CASE_TYPES.indexOf(a) - CASE_TYPES.indexOf(b)));
  const total = summarize(results);
  return { total, byType, fixtures, results };
}

export function readBaseline() {
  return JSON.parse(readFileSync(LAST_RUN_PATH, "utf8"));
}

const pct = (a) => `${(a * 100).toFixed(1)}%`;

function main() {
  const run = runEval();
  console.log("per fixture:");
  for (const [name, f] of Object.entries(run.fixtures)) {
    console.log(`  ${name}: ${f.correct}/${f.expected} (${pct(f.accuracy)})`);
  }
  console.log("per case type:");
  for (const [name, f] of Object.entries(run.byType)) {
    console.log(`  ${name}: ${f.correct}/${f.expected} (${pct(f.accuracy)})`);
  }
  for (const r of run.results.filter((x) => !x.ok)) {
    console.log(`  ${r.outcome} ${r.fixture} ${JSON.stringify(r.field)}: expected ${r.expected}, got ${r.actual}`);
  }
  const t = run.total;
  console.log(`total: ${t.correct}/${t.expected} (${pct(t.accuracy)})`);
  const typeAccuracy = Object.fromEntries(Object.entries(run.byType).map(([k, v]) => [k, v.accuracy]));
  console.log(JSON.stringify({ eval: "fill-accuracy", ...t, byType: typeAccuracy }));
  writeFileSync(LAST_RUN_PATH, JSON.stringify(run, null, 2) + "\n");
}

if (process.argv[1] === import.meta.filename) main();

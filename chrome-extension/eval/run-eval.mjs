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
import { QUARANTINE_CLEAN_USES, confirmMapping, learnFromOutcomes } from "../extension/lib/mappings.js";
import { buildOutcomeRecords } from "../extension/lib/outcomes.js";

const ROOT = join(import.meta.dirname, "..");
const FIXTURES_DIR = join(ROOT, "tests", "fixtures");
export const VAULT_PATH = join(ROOT, "eval", "eval-vault.json");
export const LAST_RUN_PATH = join(ROOT, "eval", "last-run.json");
export const REPLAY_PATH = join(ROOT, "eval", "replay-outcomes.json");

// Each fixture stands in for one site, so learned mappings are scoped to it.
export const fixtureHost = (fixture) => `${fixture.replace(/\.html$/, "")}.example.test`;

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
// tests pass a modified copy to simulate a regression. `learned` is a
// learnedMappings list (#1540) used by the corrections replay; without it the
// run is the plain deterministic baseline.
export function runEval({ vault, learned = null } = {}) {
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
      const match = matchField({ label, autocomplete, name: el.getAttribute("name") || "", type: el.type || el.tagName.toLowerCase() }, activeVault, { learned, host: fixtureHost(fill.fixture) });
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

const strip = ({ total, byType, fixtures }) => ({ total, byType, fixtures });

// Corrections replay (#1540). eval/replay-outcomes.json is a synthetic list of
// recorded corrections: a field was filled with the wrong vault entry and the
// user edited it to the right one. The replay pushes them through the real
// mapping functions and re-runs matching:
//   before      the plain baseline
//   quarantined after the corrections are learned (nothing may change: a
//               quarantined mapping is never used to fill)
//   after       after every mapping is confirmed, by one Confirm click (the
//               fields listed in confirmByClick) or by the agreeing
//               submissions the extension would actually see
// Every visit asks matchField what the extension would fill with the mappings
// learned so far, so a quarantined mapping fills nothing and the user keeps
// correcting, exactly as in production. UPPER BOUND: outcomes exist only for
// fields the extension filled. Where the vault abstains the replay assumes the
// on-device model filled the field with spec.filledEntryId (pass "prompt");
// `modeledLlmFills` counts those fields. Without the model the vault-abstain
// fields produce no outcome and are not learnable.
export function runReplay({ vault } = {}) {
  const activeVault = vault || JSON.parse(readFileSync(VAULT_PATH, "utf8"));
  const spec = JSON.parse(readFileSync(REPLAY_PATH, "utf8"));
  const before = runEval({ vault: activeVault });
  const find = (c) => {
    const r = before.results.find((x) => x.fixture === c.fixture && JSON.stringify(x.field) === JSON.stringify(c.field));
    if (!r) throw new Error(`replay: no field ${JSON.stringify(c.field)} in ${c.fixture}`);
    return r;
  };
  const byId = (id) => activeVault.find((e) => e.id === id);
  let mappings = [];
  let n = 0;
  let modeledLlmFills = 0;
  const modeled = new Set();
  // One visit to the field: fill as matchField would, the user submits the
  // submittedEntryId value, the outcome is learned.
  const visit = (c) => {
    const r = find(c);
    const [label, name, autocomplete, type] = r.sig.split("|");
    const host = fixtureHost(c.fixture);
    const m = matchField({ label, name, autocomplete, type }, activeVault, { learned: mappings, host });
    if (!m) modeled.add(JSON.stringify([c.fixture, c.field]));
    const filled = m ? m.entry : byId(c.filledEntryId);
    const submitted = byId(c.submittedEntryId);
    const records = buildOutcomeRecords(
      {
        fillId: `replay-${++n}`,
        host,
        fields: [
          {
            sig: { label, name, autocomplete, type },
            filled: filled.value,
            submitted: submitted.value,
            entryKey: filled.key,
            entryId: filled.id,
            pass: m ? m.pass : "prompt",
            confidence: m ? m.confidence : 0.6
          }
        ]
      },
      activeVault,
      n
    );
    mappings = learnFromOutcomes(mappings, records, activeVault, n);
    return r.sig;
  };
  const sigs = spec.corrections.map((c) => visit(c));
  const quarantined = runEval({ vault: activeVault, learned: mappings });
  const clicked = new Set(spec.confirmByClick.map((c) => JSON.stringify([c.fixture, c.field])));
  spec.corrections.forEach((c, i) => {
    if (clicked.has(JSON.stringify([c.fixture, c.field]))) {
      mappings = confirmMapping(mappings, sigs[i]);
      return;
    }
    const status = () => mappings.find((m) => m.sig === sigs[i])?.status;
    for (let k = 0; k < 2 * QUARANTINE_CLEAN_USES && status() !== "confirmed"; k++) visit(c);
  });
  modeledLlmFills = modeled.size;
  const after = runEval({ vault: activeVault, learned: mappings });
  return {
    corrections: spec.corrections.length,
    confirmed: mappings.filter((m) => m.status === "confirmed").length,
    modeledLlmFills,
    before: strip(before),
    quarantined: strip(quarantined),
    after: strip(after),
    accuracyRise: round(after.total.accuracy - before.total.accuracy)
  };
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
  const replay = runReplay();
  console.log(`corrections replay (before -> quarantined -> after confirm; upper bound, ${replay.modeledLlmFills} of ${replay.corrections} fields assume an on-device model fill):`);
  for (const [name, f] of Object.entries(replay.after.fixtures)) {
    const b = replay.before.fixtures[name];
    const q = replay.quarantined.fixtures[name];
    console.log(`  ${name}: ${b.correct}/${b.expected} -> ${q.correct}/${q.expected} -> ${f.correct}/${f.expected}`);
  }
  for (const [name, f] of Object.entries(replay.after.byType)) {
    const b = replay.before.byType[name];
    console.log(`  type ${name}: ${pct(b.accuracy)} -> ${pct(f.accuracy)}`);
  }
  const rt = replay.after.total;
  console.log(`replay total: ${pct(replay.before.total.accuracy)} -> ${pct(replay.quarantined.total.accuracy)} -> ${pct(rt.accuracy)} (+${pct(replay.accuracyRise)})`);
  writeFileSync(LAST_RUN_PATH, JSON.stringify({ ...run, replay }, null, 2) + "\n");
}

if (process.argv[1] === import.meta.filename) main();

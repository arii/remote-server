// ============================================================================
// LocalForm AI - Correction Learning (pure logic)
// Approve/discard decisions in the Review and Merge Queue and verified-fill
// verdicts are labeled data. This module turns them into alias proposals for
// existing vault entries. Aliases only: values and keys are never changed.
//
// Pipeline: recordCase -> aggregateProposals -> verifyProposal -> promoteAlias.
// The service worker (the only storage writer) runs every step; the side panel
// may refine proposals with the on-device LanguageModel, but its output is
// untrusted and only accepted through parseRefinement + verifyProposal.
// No chrome.* APIs here.
// ============================================================================

import { aliasMatches, normalizeLabel } from "./matcher.js";
import { isSensitiveLabel } from "./sensitive.js";

export const MAX_LEARNING_CASES = 200;
export const MAX_LEARNED_LOG = 50;
export const MIN_CORRECTIONS = 2; // same label -> same entry approvals before proposing
export const MAX_ALIAS_LENGTH = 80;
export const OUTCOMES = ["approved", "discarded", "fill-pass", "fill-fail"];

// Punctuation-insensitive form used to check that an alias is "the same label".
export function canonicalLabel(s) {
  return normalizeLabel(String(s || "").replace(/[^\p{L}\p{N}\s]/gu, " "));
}

// First entry (vault order) whose key or an alias matches the label: the same
// rule the panel's deterministic matcher and the observer apply.
export function resolveEntry(label, vault) {
  for (const entry of vault || []) {
    const names = [entry.key, ...(entry.aliases || [])];
    if (names.some((n) => aliasMatches(label, n))) return entry;
  }
  return null;
}

// Append a case (newest last), FIFO-evicting beyond MAX_LEARNING_CASES.
// Sensitive labels/keys, unknown outcomes, and cases lacking a label or entry
// are skipped.
export function recordCase(cases, c, max = MAX_LEARNING_CASES) {
  const list = Array.isArray(cases) ? cases : [];
  if (!c || typeof c.label !== "string" || typeof c.entryId !== "string") return list;
  const label = normalizeLabel(c.label);
  if (!label || !c.entryId || !OUTCOMES.includes(c.outcome)) return list;
  if (isSensitiveLabel(c.label) || isSensitiveLabel(c.entryKey)) return list;
  const next = [
    ...list,
    {
      label,
      entryId: c.entryId,
      entryKey: String(c.entryKey || ""),
      outcome: c.outcome,
      ts: typeof c.ts === "number" ? c.ts : Date.now()
    }
  ];
  return next.length > max ? next.slice(next.length - max) : next;
}

// Cases for a batch of pending deltas that were approved or discarded.
// Only deltas that carry a field label and target a vault entry are usable.
export function casesFromDeltas(deltas, outcome, vault, ts = Date.now()) {
  const out = [];
  for (const d of deltas || []) {
    if (!d || typeof d.fieldLabel !== "string" || !d.targetEntryId) continue;
    const entry = (vault || []).find((e) => e.id === d.targetEntryId);
    out.push({ label: d.fieldLabel, entryId: d.targetEntryId, entryKey: entry?.key || d.key || "", outcome, ts });
  }
  return out;
}

export function recordCases(cases, newCases, max = MAX_LEARNING_CASES) {
  return (newCases || []).reduce((acc, c) => recordCase(acc, c, max), Array.isArray(cases) ? cases : []);
}

// Repeated approved corrections for one label -> one entry become
// {label, entryId, alias} proposals, unless the label already resolves to
// that entry (nothing to learn).
export function aggregateProposals(cases, vault, { minCount = MIN_CORRECTIONS } = {}) {
  const groups = new Map();
  for (const c of cases || []) {
    if (c.outcome !== "approved" || isSensitiveLabel(c.label)) continue;
    const label = normalizeLabel(c.label);
    const k = `${label}\u0000${c.entryId}`;
    const g = groups.get(k) || { label, entryId: c.entryId, count: 0 };
    g.count++;
    groups.set(k, g);
  }
  const proposals = [];
  for (const g of groups.values()) {
    if (g.count < minCount) continue;
    const entry = (vault || []).find((e) => e.id === g.entryId);
    if (!entry) continue;
    if (resolveEntry(g.label, vault)?.id === g.entryId) continue;
    proposals.push({ label: g.label, entryId: g.entryId, alias: normalizeLabel(g.label), count: g.count });
  }
  return proposals;
}

// Append the alias to the entry only. Value, key, tags untouched; no duplicate.
export function promoteAlias(vault, proposal) {
  const alias = normalizeLabel(proposal?.alias);
  return (vault || []).map((e) => {
    if (e.id !== proposal?.entryId) return e;
    const aliases = e.aliases || [];
    if (aliases.some((a) => normalizeLabel(a) === alias)) return e;
    return { ...e, aliases: [...aliases, alias], updatedAt: new Date().toISOString() };
  });
}

// Deterministic gate run worker-side before any write. Replays the recorded
// cases with and without the alias:
//  - must fix >= 1 recorded failure (approved case for this entry that the
//    current vault does not resolve to it),
//  - must break zero recorded successes (a case that resolved to its entry
//    stops doing so) and must not resolve a label the user discarded for
//    that entry,
//  - rejected on conflict with a different entry.
export function verifyProposal(proposal, cases, vault) {
  const reject = (reason) => ({ ok: false, reason });
  if (!proposal || typeof proposal !== "object") return reject("invalid-proposal");
  const label = normalizeLabel(proposal.label);
  const alias = normalizeLabel(proposal.alias);
  if (!label || !alias) return reject("empty-label");
  if (alias.length > MAX_ALIAS_LENGTH) return reject("alias-too-long");
  if (isSensitiveLabel(label) || isSensitiveLabel(alias)) return reject("sensitive");
  if (canonicalLabel(alias) !== canonicalLabel(label)) return reject("alias-not-label-form");

  const entry = (vault || []).find((e) => e.id === proposal.entryId);
  if (!entry) return reject("unknown-entry");
  if (isSensitiveLabel(entry.key)) return reject("sensitive-entry");

  const current = resolveEntry(label, vault);
  if (current && current.id !== entry.id) return reject("conflict-label-matches-other-entry");
  for (const other of vault) {
    if (other.id === entry.id) continue;
    const names = [other.key, ...(other.aliases || [])].map(normalizeLabel);
    if (names.includes(alias) || names.includes(label)) return reject("conflict-alias-owned-by-other-entry");
  }

  const after = promoteAlias(vault, { ...proposal, alias });
  let fixed = 0;
  for (const c of cases || []) {
    const before = resolveEntry(c.label, vault)?.id || null;
    const now = resolveEntry(c.label, after)?.id || null;
    if (before === now) continue;
    if (c.outcome === "discarded" && now === c.entryId) return reject("breaks-discarded-case");
    if (before === c.entryId && now !== c.entryId && c.outcome !== "fill-fail") return reject("breaks-recorded-success");
    if (c.outcome === "approved" && c.entryId === entry.id && now === entry.id && before !== entry.id) fixed++;
  }
  if (fixed < 1) return reject("fixes-no-recorded-failure");
  return { ok: true, fixed };
}

// ---- LanguageModel refinement (panel side, untrusted output) --------------

export const REFINEMENT_SCHEMA = {
  type: "object",
  properties: {
    proposals: {
      type: "array",
      items: {
        type: "object",
        properties: {
          entryId: { type: "string" },
          alias: { type: "string", maxLength: MAX_ALIAS_LENGTH }
        },
        required: ["entryId", "alias"],
        additionalProperties: false
      }
    }
  },
  required: ["proposals"],
  additionalProperties: false
};

export function buildRefinementPrompt(proposals) {
  const items = proposals.map((p) => ({ entryId: p.entryId, label: p.label }));
  return (
    "Each item is a form field label that a user repeatedly mapped to a saved profile entry. " +
    "For each item return a short, clean alias: the same label with punctuation, asterisks and " +
    "required-markers removed. Do not add or change words. Never invent values.\n" +
    'Respond with JSON only: {"proposals":[{"entryId":string,"alias":string}]}\n' +
    JSON.stringify(items)
  );
}

// Strict validation of the model's text. Returns the accepted refined
// proposals (subset of `offered`, alias replaced) or null when anything is off.
export function parseRefinement(text, offered, cases, vault) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  if (Object.keys(data).length !== 1 || !Array.isArray(data.proposals)) return null;
  const refined = [];
  for (const item of data.proposals) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const keys = Object.keys(item).sort().join(",");
    if (keys !== "alias,entryId") return null;
    if (typeof item.entryId !== "string" || typeof item.alias !== "string") return null;
    if (item.alias.length > MAX_ALIAS_LENGTH || !normalizeLabel(item.alias)) return null;
    const base = offered.find(
      (p) => p.entryId === item.entryId && canonicalLabel(p.label) === canonicalLabel(item.alias)
    );
    if (!base) return null;
    const candidate = { ...base, alias: normalizeLabel(item.alias) };
    if (!verifyProposal(candidate, cases, vault).ok) return null;
    refined.push(candidate);
  }
  return refined;
}

// Apply an accepted refinement over the deterministic list; anything the model
// did not (validly) return keeps its deterministic proposal.
export function mergeRefinement(offered, refined) {
  if (!refined) return offered;
  return offered.map((p) => refined.find((r) => r.entryId === p.entryId && r.label === p.label) || p);
}

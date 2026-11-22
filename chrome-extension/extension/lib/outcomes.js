// ============================================================================
// LocalForm AI - Fill Outcomes (pure logic, #1539)
// After a Form-Import++ fill, the user submits the form. Comparing what we
// filled with what was submitted tells us whether each field was kept, edited
// or cleared. Only the class of the outcome is stored, never a value.
//
// The content script sends the values once, in a transient message; the
// service worker (the only storage writer) classifies and stores records under
// `fillOutcomes`. No chrome.* APIs here.
// ============================================================================

import { normalizeLabel } from "./matcher.js";
import { isSensitiveLabel } from "./sensitive.js";

export const MAX_FILL_OUTCOMES = 500;
export const STATS_WINDOW = 20; // distinct fills used for the kept-rate
export const PASSES = ["learned", "autocomplete", "alias", "prompt"];
export const OUTCOME_CLASSES = ["kept", "edited", "cleared"];

// Whitespace and case differences are not a user edit.
export function classifyOutcome(filled, submitted) {
  const was = normalizeLabel(filled);
  const now = normalizeLabel(submitted);
  if (!now) return "cleared";
  return now === was ? "kept" : "edited";
}

// Vault entry (other than the one we filled from) whose value equals what the
// user submitted. Sensitive entries are never a target. Returns an id or null.
export function remapTarget(submitted, vault, excludeEntryId) {
  const target = normalizeLabel(submitted);
  if (!target) return null;
  for (const entry of vault || []) {
    if (!entry || entry.id === excludeEntryId) continue;
    if (isSensitiveLabel(entry.key) || isSensitiveLabel(entry.id)) continue;
    if (normalizeLabel(entry.value) === target) return entry.id;
  }
  return null;
}

// Exact hostname match against observerDomainAllowlist; fail closed.
export function shouldRecord(host, allowlist) {
  return typeof host === "string" && host !== "" && Array.isArray(allowlist) && allowlist.includes(host);
}

function sigPart(s) {
  return normalizeLabel(s);
}

// Build stored records from the transient content-script message. Values are
// read here and dropped: the returned records hold classes and ids only.
export function buildOutcomeRecords({ fillId, host, fields }, vault, ts = Date.now()) {
  if (typeof fillId !== "string" || !fillId || typeof host !== "string" || !Array.isArray(fields)) return [];
  const records = [];
  for (const f of fields) {
    if (!f || !PASSES.includes(f.pass)) continue;
    const sig = f.sig || {};
    if ([sig.label, sig.name, sig.autocomplete, sig.type, f.entryKey].some((s) => isSensitiveLabel(s))) continue;
    if (sig.type === "password") continue;
    const outcome = classifyOutcome(f.filled, f.submitted);
    records.push({
      fillId,
      ts,
      host,
      sig: {
        label: sigPart(sig.label),
        name: sigPart(sig.name),
        autocomplete: sigPart(sig.autocomplete),
        type: sigPart(sig.type)
      },
      entryKey: typeof f.entryKey === "string" && f.entryKey ? f.entryKey : null,
      pass: f.pass,
      confidence: typeof f.confidence === "number" ? Math.round(f.confidence * 100) / 100 : null,
      outcome,
      remappedEntryId: outcome === "edited" ? remapTarget(f.submitted, vault, f.entryId) : null
    });
  }
  return records;
}

// Append (newest last), dropping the oldest beyond the cap.
export function recordOutcomes(existing, records, max = MAX_FILL_OUTCOMES) {
  const next = [...(Array.isArray(existing) ? existing : []), ...(records || [])];
  return next.length > max ? next.slice(next.length - max) : next;
}

// {last: {kept, edited, cleared} | null, keptRate: 0..1 | null, fills}
// `last` is the most recent fillId; keptRate covers the last STATS_WINDOW
// distinct fillIds (kept / all recorded fields in them).
export function fillStats(outcomes, window = STATS_WINDOW) {
  const list = Array.isArray(outcomes) ? outcomes : [];
  const ids = [];
  for (let i = list.length - 1; i >= 0 && ids.length < window; i--) {
    if (!ids.includes(list[i].fillId)) ids.push(list[i].fillId);
  }
  if (ids.length === 0) return { last: null, keptRate: null, fills: 0 };
  const count = (rows) => ({
    kept: rows.filter((r) => r.outcome === "kept").length,
    edited: rows.filter((r) => r.outcome === "edited").length,
    cleared: rows.filter((r) => r.outcome === "cleared").length
  });
  const inWindow = list.filter((r) => ids.includes(r.fillId));
  const last = count(list.filter((r) => r.fillId === ids[0]));
  return { last, keptRate: inWindow.length ? count(inWindow).kept / inWindow.length : null, fills: ids.length };
}

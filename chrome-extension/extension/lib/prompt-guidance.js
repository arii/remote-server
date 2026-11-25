// ============================================================================
// LocalForm AI - Prompt guidance edits and improvement proposals (pure logic)
// Guidance lives in chrome.storage.local `promptGuidance`. Model-proposed
// guidance is untrusted: verified here before it is ever shown, and applied
// only when the user accepts it. Every edit is revertible via history.
// ============================================================================

import { PROMPT_IDS, MAX_GUIDANCE_CHARS, sanitizeGuidance } from "./prompts.js";

export const MAX_GUIDANCE_HISTORY = 20;
export const MAX_SUGGESTION_CHARS = 200;
export const MAX_SUGGESTIONS = 5;
export const MAX_FEEDBACK_COUNT = 1000;

export const EMPTY_GUIDANCE = Object.freeze({ global: "", perPrompt: {}, history: [] });

export function normalizeGuidance(raw) {
  const g = raw && typeof raw === "object" ? raw : {};
  const perPrompt = {};
  for (const id of PROMPT_IDS) {
    const t = sanitizeGuidance(g.perPrompt?.[id]);
    if (t) perPrompt[id] = t;
  }
  return {
    global: sanitizeGuidance(g.global),
    perPrompt,
    history: Array.isArray(g.history) ? g.history.slice(-MAX_GUIDANCE_HISTORY) : []
  };
}

// Only the text part, as stored in history before/after snapshots.
function snapshot(g) {
  return { global: g.global, perPrompt: { ...g.perPrompt } };
}

// target: "global" or a prompt id. Empty text clears it.
export function applyGuidanceEdit(current, target, text, { source = "user", at = Date.now() } = {}) {
  if (target !== "global" && !PROMPT_IDS.includes(target)) throw new Error(`Unknown guidance target: ${target}`);
  if (source !== "user" && source !== "suggestion") throw new Error(`Unknown guidance source: ${source}`);
  const base = normalizeGuidance(current);
  const next = snapshot(base);
  const clean = sanitizeGuidance(text);
  if (target === "global") next.global = clean;
  else if (clean) next.perPrompt[target] = clean;
  else delete next.perPrompt[target];
  if (JSON.stringify(next) === JSON.stringify(snapshot(base))) return base;
  const history = [...base.history, { at, before: snapshot(base), after: next, source }].slice(-MAX_GUIDANCE_HISTORY);
  return { ...next, history };
}

// Undo the most recent edit. No-op when there is no history.
export function revertGuidance(current) {
  const base = normalizeGuidance(current);
  if (!base.history.length) return base;
  const last = base.history[base.history.length - 1];
  const before = normalizeGuidance(last.before);
  return { global: before.global, perPrompt: before.perPrompt, history: base.history.slice(0, -1) };
}

// Append a line to existing guidance for a target (used by Accept).
export function appendGuidanceLine(current, target, line, opts) {
  const base = normalizeGuidance(current);
  const existing = target === "global" ? base.global : base.perPrompt[target] || "";
  const joined = existing ? `${existing}\n${line}` : line;
  if (joined.length > MAX_GUIDANCE_CHARS) throw new Error("Guidance would exceed the size limit; trim existing guidance first.");
  return applyGuidanceEdit(base, target, joined, opts);
}

// Capped approve/discard counters per category. Counts only, never values.
export function recordDeltaFeedback(feedback, category, decision) {
  const fb = feedback && typeof feedback === "object" ? feedback : {};
  if (decision !== "approved" && decision !== "discarded") return fb;
  const cat = typeof category === "string" && category ? category.slice(0, 40) : "custom";
  const row = fb[cat] || { approved: 0, discarded: 0 };
  return { ...fb, [cat]: { ...row, [decision]: Math.min(MAX_FEEDBACK_COUNT, (row[decision] || 0) + 1) } };
}

// Per-field edit/clear rates from stored fill outcomes (labels only, no values).
function fieldRates(outcomes) {
  const by = new Map();
  for (const r of Array.isArray(outcomes) ? outcomes : []) {
    const label = r?.sig?.label || r?.label || r?.entryId;
    if (!label || !["kept", "edited", "cleared"].includes(r.outcome)) continue;
    const row = by.get(label) || { label: String(label).slice(0, 60), kept: 0, edited: 0, cleared: 0 };
    row[r.outcome]++;
    by.set(label, row);
  }
  return [...by.values()]
    .filter((r) => r.edited + r.cleared > 0)
    .sort((a, b) => b.edited + b.cleared - (a.edited + a.cleared))
    .slice(0, 8);
}

export const SUGGESTION_SCHEMA = {
  type: "object",
  properties: {
    suggestions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          promptId: { type: "string", enum: ["global", ...PROMPT_IDS] },
          guidance: { type: "string", maxLength: MAX_SUGGESTION_CHARS },
          reason: { type: "string", maxLength: MAX_SUGGESTION_CHARS }
        },
        required: ["promptId", "guidance", "reason"],
        additionalProperties: false
      }
    }
  },
  required: ["suggestions"],
  additionalProperties: false
};

// Build the request from aggregate stats only. Returns null when there is no
// signal worth asking about.
export function proposeGuidanceRequest({ outcomes, deltaFeedback, learnedAliasCount = 0, guidance } = {}) {
  const fields = fieldRates(outcomes);
  const feedback = Object.entries(deltaFeedback || {}).filter(([, v]) => v && v.approved + v.discarded > 0);
  if (!fields.length && !feedback.length) return null;
  const current = normalizeGuidance(guidance);
  const signals = {
    fieldsUsersCorrected: fields.map((f) => ({ field: f.label, edited: f.edited, cleared: f.cleared, kept: f.kept })),
    suggestedFactsByCategory: feedback.map(([category, v]) => ({ category, approved: v.approved, discarded: v.discarded })),
    learnedAliasCount
  };
  return (
    "You tune the instructions of an on-device form-filling assistant. Based on these usage signals, propose at most " +
    `${MAX_SUGGESTIONS} short guidance lines (each under ${MAX_SUGGESTION_CHARS} characters) that would make future profile fact extraction and form answers more accurate. ` +
    `promptId is "global" or one of: ${PROMPT_IDS.join(", ")}. Never include personal values, emails, phone numbers or URLs.\n` +
    `Current guidance: ${JSON.stringify({ global: current.global, perPrompt: current.perPrompt })}\n` +
    `Signals: ${JSON.stringify(signals)}\n` +
    'Respond with JSON only: {"suggestions":[{"promptId":string,"guidance":string,"reason":string}]}'
  );
}

const PII = [
  /https?:\/\/|www\./i,
  /[^\s@]+@[^\s@]+\.[a-z]{2,}/i,
  /\+?\d[\d\s().-]{7,}\d/,
  /\b\d{3}-\d{2}-\d{4}\b/
];

export function looksLikePii(text, vaultValues = []) {
  const t = String(text);
  if (PII.some((re) => re.test(t))) return true;
  const lower = t.toLowerCase();
  return vaultValues.some((v) => {
    const s = String(v ?? "").trim().toLowerCase();
    return s.length >= 4 && lower.includes(s);
  });
}

// Strict validation of untrusted model text. Returns accepted suggestions;
// returns [] when the output is malformed. Invalid items are dropped, never shown.
export function parseGuidanceSuggestions(text, { vaultValues = [] } = {}) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return [];
  }
  if (!data || typeof data !== "object" || !Array.isArray(data.suggestions)) return [];
  const out = [];
  for (const item of data.suggestions) {
    if (!item || typeof item !== "object") continue;
    const { promptId, guidance, reason } = item;
    if (typeof promptId !== "string" || (promptId !== "global" && !PROMPT_IDS.includes(promptId))) continue;
    if (typeof guidance !== "string" || typeof reason !== "string") continue;
    const g = sanitizeGuidance(guidance);
    const r = sanitizeGuidance(reason);
    if (!g || g.length > MAX_SUGGESTION_CHARS || r.length > MAX_SUGGESTION_CHARS) continue;
    if (looksLikePii(g, vaultValues) || looksLikePii(r, vaultValues)) continue;
    if (out.some((o) => o.promptId === promptId && o.guidance === g)) continue;
    out.push({ promptId, guidance: g, reason: r });
    if (out.length >= MAX_SUGGESTIONS) break;
  }
  return out;
}

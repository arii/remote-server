// ============================================================================
// LocalForm AI - Prompt eval samples: deterministic scoring and guidance
// suggestions from failures (pure logic, no network, no model calls).
// Scope: the "ask" prompt. Other prompt ids are future work.
// ============================================================================

import { sanitizeGuidance } from "./prompts.js";
import { MAX_SUGGESTIONS, looksLikePii } from "./prompt-guidance.js";

export const MAX_EVAL_CASES = 10;
export const MAX_EVAL_INPUT_CHARS = 500;
const MAX_TERMS = 10;
const MAX_TERM_CHARS = 40;

function cleanTerms(list) {
  const out = [];
  for (const t of Array.isArray(list) ? list : []) {
    const s = String(t ?? "").trim().slice(0, MAX_TERM_CHARS);
    if (s && !out.some((o) => o.toLowerCase() === s.toLowerCase())) out.push(s);
    if (out.length >= MAX_TERMS) break;
  }
  return out;
}

export function normalizeCases(raw) {
  const out = [];
  for (const c of Array.isArray(raw) ? raw : []) {
    const input = String(c?.input ?? "").trim().slice(0, MAX_EVAL_INPUT_CHARS);
    if (!input) continue;
    const max = Number(c?.expect?.maxWords);
    const expect = {
      mustContain: cleanTerms(c?.expect?.mustContain),
      mustNotContain: cleanTerms(c?.expect?.mustNotContain),
      maxWords: Number.isFinite(max) && max > 0 ? Math.floor(max) : null
    };
    // A case with no expectation can never fail and would inflate the score.
    if (!expect.mustContain.length && !expect.mustNotContain.length && !expect.maxWords) continue;
    out.push({ input, expect });
    if (out.length >= MAX_EVAL_CASES) break;
  }
  return out;
}

export function scoreOutput(output, expect = {}) {
  const text = String(output ?? "");
  const lower = text.toLowerCase();
  const failures = [];
  for (const term of expect.mustContain || []) if (!lower.includes(term.toLowerCase())) failures.push({ type: "missing", term });
  for (const term of expect.mustNotContain || []) if (lower.includes(term.toLowerCase())) failures.push({ type: "forbidden", term });
  const limit = expect.maxWords;
  if (limit && text.split(/\s+/).filter(Boolean).length > limit) failures.push({ type: "too_long", limit });
  const checks = (expect.mustContain?.length || 0) + (expect.mustNotContain?.length || 0) + (limit ? 1 : 0);
  const score = checks ? (checks - failures.length) / checks : 1;
  return { pass: failures.length === 0, score, failures };
}

// Stable key for a case set (djb2 over its JSON), so runs are only compared when the set is unchanged.
export function casesKey(cases) {
  const str = JSON.stringify(cases);
  let h = 5381;
  for (let i = 0; i < str.length; i++) h = ((h * 33) ^ str.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

// results: [{pass, score, failures}]. previous: prior summary score (number) or null.
export function summarizeRun(results, previous = null) {
  const total = results.length;
  const passed = results.filter((r) => r.pass).length;
  const score = total ? results.reduce((a, r) => a + r.score, 0) / total : 0;
  const delta = typeof previous === "number" ? score - previous : null;
  return { passed, total, score, delta };
}

export function suggestFromFailures(results, { vaultValues = [], guidance } = {}) {
  const existing = [guidance?.global, guidance?.perPrompt?.ask].filter(Boolean).join("\n").toLowerCase();
  const out = [];
  for (const r of results) {
    for (const f of r.failures || []) {
      let line;
      let reason;
      if (f.type === "too_long") [line, reason] = [`Keep answers under ${f.limit} words.`, "A sample answer was too long."];
      else if (f.type === "missing") [line, reason] = [`Mention "${f.term}" when relevant.`, `A sample answer did not mention "${f.term}".`];
      else if (f.type === "forbidden") [line, reason] = [`Never use the word "${f.term}".`, `A sample answer used "${f.term}".`];
      else continue;
      const g = sanitizeGuidance(line);
      if (!g || looksLikePii(g, vaultValues) || existing.includes(g.toLowerCase())) continue;
      if (out.some((o) => o.guidance === g)) continue;
      out.push({ promptId: "ask", guidance: g, reason });
      if (out.length >= MAX_SUGGESTIONS) return out;
    }
  }
  return out;
}

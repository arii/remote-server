// ============================================================================
// LocalForm AI - Shared Field Matcher (audit high #4 contract)
// The ONE set of label-matching rules for the extension. Strict alias
// matching: exact match after normalization, then whole-word containment
// for aliases of 8+ characters. Short aliases match exactly only — the old
// bidirectional substring test let a two-letter-overlap alias like "name"
// match "Company name".
//
// Consumers: sidepanel.js (vault search), service-worker.js (delta
// proposals). The content script still carries its classic-script copy —
// it cannot import ES modules; the OD-19 matcher phase bridges it through
// the build-emitted legacy hook.
// ============================================================================

export function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, (ch) => "\\" + ch);
}

// Normalize a label or alias: lowercase, collapse whitespace runs to single
// spaces, trim. The exact pipeline the inline matchers applied inline.
export function normalizeLabel(s) {
  return String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
}

const aliasRegexCache = new Map(); // compiled word-boundary matchers per normalized alias

export function aliasMatches(query, alias) {
  const q = normalizeLabel(query);
  const a = normalizeLabel(alias);
  if (!q || !a) return false;
  if (q === a) return true;
  if (a.length < 8) return false; // short aliases match exactly only
  let re = aliasRegexCache.get(a);
  if (!re) {
    re = new RegExp(`\\b${escapeRegExp(a)}\\b`);
    aliasRegexCache.set(a, re);
  }
  return re.test(q);
}

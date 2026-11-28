// ============================================================================
// LocalForm AI - Answer policy (pure logic)
// Guardrails for autofill answers. Substantive commitments (work authorization,
// sponsorship, onsite, relocation, start date, pay) and generated essays are
// never written to a form silently: they become suggestions the user accepts,
// edits or rejects. Consent boxes are never ticked by default. Generated text
// must be grounded in the supplied facts.
// ============================================================================

const COMMITMENT_RE =
  /authori[sz]ed to work|work authori[sz]ation|legally (?:able|eligible|authori)|right to work|visa|sponsor|on-?\s?site|in[- ]office|hybrid|remote|relocat|start date|available to start|earliest start|notice period|salary|compensation|expected pay|veteran|disabilit|gender|race|ethnic|criminal|background check|security clearance|citizen/i;
const CONSENT_RE = /\bagree\b|\bconsent\b|\bterms\b|privacy|acknowledg|certify|\bi (?:have read|accept)\b/i;

export function isCommitmentQuery(q) {
  return COMMITMENT_RE.test(String(q || ""));
}

export function isConsentQuery(q) {
  return CONSENT_RE.test(String(q || ""));
}

// Consent and legal acknowledgments are never filled automatically.
export function isConsentField(field) {
  return field?.semanticCategory === "legal.consent" || (field?.inputType === "checkbox" && isConsentQuery(field?.query));
}

// Facts usable as essay grounding: vault entries not marked model-inferred.
export function groundedEntries(vault) {
  return (vault || []).filter((e) => e && !(e.tags || []).some((t) => /^(inferred|ai|ai-draft|rejected)$/i.test(t)));
}

// A commitment answer is verified only by an explicit record that the USER
// confirmed this value: entry.userConfirmation = {by: "user", value, at}.
// No flag, tag, source or model output can create it; legacy entries have none.
export function isUserConfirmed(entry, query) {
  const c = entry?.userConfirmation;
  if (!c || c.by !== "user" || typeof c.at !== "string") return false;
  if (String(c.value).trim() !== String(entry.value).trim()) return false;
  // Scope: the confirmation covers only the question the user confirmed it for.
  // A missing scope, or a different question that merely matched by alias, is unverified.
  const norm1 = (x) => String(x || "").toLowerCase().replace(/\s+/g, " ").trim();
  return !!c.scope && norm1(c.scope) === norm1(query);
}

const STOP = new Set(["about", "which", "their", "there", "would", "could", "should", "these", "those", "where", "while", "being", "other", "using", "through"]);
const words = (s) => (norm(s).match(/[a-z]{5,}/g) || []).filter((w) => !STOP.has(w));

// Keep only sentences whose content words are mostly present in one fact line.
// Returns {kept, dropped, support}. Still lexical: the result is a draft for
// review, never proof, and is never written to the page without acceptance.
export function supportedSentences(text, factLines) {
  const lines = (factLines || []).map((l) => ({ l, w: new Set(words(l)) }));
  const kept = [];
  const dropped = [];
  const support = [];
  for (const sent of String(text || "").split(/(?<=[.!?])\s+/).map((x) => x.trim()).filter(Boolean)) {
    const ws = words(sent);
    let best = null;
    let bestScore = 0;
    for (const f of lines) {
      const hit = ws.filter((w) => f.w.has(w)).length;
      const score = ws.length ? hit / ws.length : 0;
      if (score > bestScore) { bestScore = score; best = f.l; }
    }
    if (ws.length === 0 || bestScore >= 0.6) {
      kept.push(sent);
      if (best) support.push({ sentence: sent, fact: best });
    } else dropped.push(sent);
  }
  return { kept, dropped, support };
}

const NUM_RE = /\d[\d,.]*\s?(?:%|k|m|x|\+)?/gi;
const norm = (s) => String(s || "").toLowerCase();

// A draft is grounded when it has facts to draw on and introduces no number or
// proper product name that is absent from the facts. `banned` holds strings
// that must never appear (the extension's own name, placeholder text).
export function checkGrounding(text, facts, { banned = [] } = {}) {
  const draft = String(text || "").trim();
  const base = norm(facts);
  if (!draft || /no supported answer/i.test(draft)) return { ok: false, reason: "empty" };
  if (base.replace(/\W/g, "").length < 40) return { ok: false, reason: "no-facts" };
  for (const b of banned) {
    if (b && norm(draft).includes(norm(b))) return { ok: false, reason: `banned:${b}` };
  }
  const bad = (draft.match(NUM_RE) || []).map((n) => n.trim().replace(/[.,]$/, "")).filter((n) => !base.includes(norm(n)));
  if (bad.length) return { ok: false, reason: `unsupported-numbers:${bad.join("|")}` };
  return { ok: true };
}

// Suggestion record kept for the review list; provenance says where it came from.
export function makeSuggestion(field, { value, source, entry = null, reason = "", support = [], dropped = [], verified = false }) {
  return {
    fieldId: field.fieldId,
    query: field.query,
    value,
    source, // "vault" | "ai-draft"
    provenance: entry ? `Vault entry "${entry.key}"` : "Generated on this device from your vault facts",
    entryId: entry?.id || null,
    reason,
    verified,
    support,
    dropped,
    status: "pending"
  };
}

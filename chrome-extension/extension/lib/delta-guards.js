// ============================================================================
// LocalForm AI - Delta Dedupe Guards (service-worker observer, GUARDRAIL 1)
// Pure membership tests deciding whether an observed submission is already
// captured by the vault or by a queued delta. URL variants dedupe together:
// protocol, www., and all non-alphanumerics are stripped before comparing.
// ============================================================================

export function normalizeForDedup(str) {
  return String(str || "")
    .toLowerCase()
    .replace(/^https?:\/\/(www\.)?/, "") // Strip protocol + www so URL variants dedupe together
    .replace(/[^a-z0-9]/g, "");
}

// True when `heldValue` already covers `proposedValue`. Same value always
// dedupes; a longer proposal (normalized length > 10) is also skipped when
// a held value contains it. Both sides normalize, so callers may pass raw
// values — normalization is idempotent for already-normalized input.
export function valueAlreadyHeld(heldValue, proposedValue) {
  const normHeld = normalizeForDedup(heldValue);
  const normProposed = normalizeForDedup(proposedValue);
  return normHeld === normProposed || (normProposed.length > 10 && normHeld.includes(normProposed));
}

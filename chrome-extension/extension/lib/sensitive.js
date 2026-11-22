// ============================================================================
// LocalForm AI - Sensitive Field Guardrail (shared predicate)
// One definition of "never touch this field" (passwords, card numbers, SSN).
// service-worker.js and lib/learning.js import it. content-script.js is a
// classic script and keeps a literal copy; tests/learning.test.js asserts the
// two pattern sources are identical so they cannot drift.
// ============================================================================

export const SENSITIVE_PATTERN = /password|passcode|cvv|cvc|cc-number|card-number|ssn|social-security/i;

export function isSensitiveLabel(text) {
  return SENSITIVE_PATTERN.test(String(text || ""));
}

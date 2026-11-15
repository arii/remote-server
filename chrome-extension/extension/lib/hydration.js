// ============================================================================
// LocalForm AI - Hydration-Verify Result Logic (verified-fill contract)
// Pure decisions and summaries for the verified-fill contract: a field
// counts as filled ONLY when the landed value is proven to match the
// intended value, and hydration runs are summarized into the filled /
// failed shape the side panel logs and renders.
//
// Consumers: sidepanel.js (trace summaries). The content script's setter
// still runs its checks inline — it cannot import ES modules; the OD-19
// matcher phase bridges the content script and adopts these helpers there.
// ============================================================================

// Verdict for a plain input/textarea fill: did the value stick exactly?
// Mirrors the inline `el.value !== String(value)` check in the content
// script's setReactSafeValue (non-typeahead branch), including the
// 60-char truncation in the failure reason.
export function verifyValueLanded(expected, landed) {
  if (landed !== String(expected)) {
    return {
      ok: false,
      reason: `Input value is "${String(landed).slice(0, 60)}", expected "${String(expected).slice(0, 60)}"`
    };
  }
  return { ok: true };
}

// Summarize a hydrate result ({ totalFields, filledCount, failedFields })
// into the trace-details shape the side panel logs for Stage 1:
// { filledCount, failedCount }. Same expressions as the inline code it
// replaces — undefined filledCount stays absent after JSON.stringify.
export function summarizeHydrationResult(result) {
  return {
    filledCount: result?.filledCount,
    failedCount: result?.failedFields?.length || 0
  };
}

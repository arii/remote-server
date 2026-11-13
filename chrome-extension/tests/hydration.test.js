// Golden tests for the hydration-verify result logic (lib/hydration.js) —
// the verified-fill contract's pure decisions and summaries.
import { describe, expect, it } from "vitest";
import { summarizeHydrationResult, verifyValueLanded } from "../extension/lib/hydration.js";

describe("verifyValueLanded", () => {
  it("passes when the landed value equals the intended value exactly", () => {
    expect(verifyValueLanded("Ariel Anders", "Ariel Anders")).toEqual({ ok: true });
    expect(verifyValueLanded("  spaced  ", "  spaced  ")).toEqual({ ok: true }); // no silent trimming
  });

  it("fails and names both values when the fill did not stick", () => {
    const v = verifyValueLanded("Ariel Anders", "");
    expect(v.ok).toBe(false);
    expect(v.reason).toBe('Input value is "", expected "Ariel Anders"');
  });

  it("fails on type mismatches, since the setter fills strings", () => {
    expect(verifyValueLanded("7", 7).ok).toBe(false);
  });

  it("truncates both sides of the failure reason at 60 chars", () => {
    const long = "A".repeat(80);
    const v = verifyValueLanded(long, "B".repeat(80));
    expect(v.ok).toBe(false);
    expect(v.reason).toBe(`Input value is "${"B".repeat(60)}", expected "${"A".repeat(60)}"`);
  });
});

describe("summarizeHydrationResult", () => {
  it("summarizes filled and failed counts for the Stage-1 trace", () => {
    const result = {
      totalFields: 9,
      filledCount: 6,
      failedFields: [
        { fieldId: "f1", query: "Phone (optional)", reason: "Element not found in DOM" },
        { fieldId: "f2", query: "Name", reason: 'Input value is "", expected "A"' },
        { fieldId: "f3", query: "Email", reason: "Fill could not be verified" }
      ]
    };
    expect(summarizeHydrationResult(result)).toEqual({ filledCount: 6, failedCount: 3 });
  });

  it("treats missing failedFields as zero failures", () => {
    expect(summarizeHydrationResult({ filledCount: 2 })).toEqual({ filledCount: 2, failedCount: 0 });
  });

  it("keeps an absent filledCount absent after JSON serialization (trace shape unchanged)", () => {
    const roundTripped = JSON.parse(JSON.stringify(summarizeHydrationResult(null)));
    expect(roundTripped).toEqual({ failedCount: 0 });
  });
});

// happy-dom sanity: the verdict helper composes with real DOM values, the
// same shape the content script's setter hands back after a fill attempt.
describe("verified-fill verdict over a DOM element", () => {
  it("verifies the landed value of an input element", () => {
    const input = document.createElement("input");
    input.value = "Ariel Anders";
    expect(verifyValueLanded("Ariel Anders", input.value)).toEqual({ ok: true });

    input.value = "Ariel Ander"; // fill fell one character short
    const v = verifyValueLanded("Ariel Anders", input.value);
    expect(v.ok).toBe(false);
    expect(v.reason).toContain('expected "Ariel Anders"');
  });
});

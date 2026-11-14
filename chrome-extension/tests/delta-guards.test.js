// Golden tests for the observer's delta dedupe guards (lib/delta-guards.js),
// extracted verbatim from the service worker's GUARDRAIL 1 tests.
import { describe, expect, it } from "vitest";
import { normalizeForDedup, valueAlreadyHeld } from "../extension/lib/delta-guards.js";

describe("normalizeForDedup", () => {
  it("strips protocol, www, and non-alphanumerics so URL variants dedupe together", () => {
    expect(normalizeForDedup("https://www.linkedin.com/in/ariel-anders")).toBe("linkedincominarielanders");
    expect(normalizeForDedup("http://linkedin.com/in/ariel-anders")).toBe("linkedincominarielanders");
    expect(normalizeForDedup("https://linkedin.com/in/ariel-anders")).toBe("linkedincominarielanders");
  });

  it("lowercases and strips spaces and punctuation", () => {
    expect(normalizeForDedup("Staff Engineer at Acme Corp.")).toBe("staffengineeratacmecorp");
  });

  it("maps empty input to empty string", () => {
    expect(normalizeForDedup("")).toBe("");
    expect(normalizeForDedup(null)).toBe("");
  });
});

describe("valueAlreadyHeld (vault / pending membership guard)", () => {
  it("skips a proposed value the vault already holds exactly, modulo normalization", () => {
    expect(valueAlreadyHeld("Staff Engineer at Acme Corp", "staff engineer at acme corp")).toBe(true);
  });

  it("skips URL variants of a held profile link", () => {
    expect(valueAlreadyHeld("https://github.com/ariel", "http://www.github.com/ariel")).toBe(true);
  });

  it("skips a long proposal contained in a held value", () => {
    // Frozen rule: containment counts only when the PROPOSED value's
    // normalized length is > 10.
    expect(valueAlreadyHeld("Senior software engineer at Acme Corp", "software engineer")).toBe(true);
  });

  it("keeps a proposal when containment length is at or under 10", () => {
    expect(valueAlreadyHeld("Senior engineer at Acme Corp", "engineer")).toBe(false);
  });

  it("keeps genuinely new values", () => {
    expect(valueAlreadyHeld("https://github.com/ariel", "https://github.com/someone-else")).toBe(false);
  });

  it("never lets a long held value be swallowed by a short proposal", () => {
    expect(valueAlreadyHeld("a", "a much longer proposal than the held value")).toBe(false);
  });
});

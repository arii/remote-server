import { describe, it, expect } from "vitest";
import {
  applyGuidanceEdit,
  appendGuidanceLine,
  revertGuidance,
  normalizeGuidance,
  recordDeltaFeedback,
  proposeGuidanceRequest,
  parseGuidanceSuggestions,
  MAX_GUIDANCE_HISTORY
} from "../extension/lib/prompt-guidance.js";

describe("guidance edits", () => {
  it("applies, reverts, and no-ops identical edits", () => {
    let g = applyGuidanceEdit(null, "global", "be brief", { at: 1 });
    expect(g.global).toBe("be brief");
    expect(g.history).toHaveLength(1);
    expect(applyGuidanceEdit(g, "global", "be brief")).toEqual(g);
    g = applyGuidanceEdit(g, "ask", "cite tabs", { source: "suggestion" });
    expect(g.perPrompt.ask).toBe("cite tabs");
    g = revertGuidance(g);
    expect(g.perPrompt.ask).toBeUndefined();
    expect(g.global).toBe("be brief");
    g = revertGuidance(revertGuidance(g));
    expect(g.global).toBe("");
  });
  it("clears per-prompt text and rejects unknown targets", () => {
    let g = applyGuidanceEdit(null, "ask", "x");
    g = applyGuidanceEdit(g, "ask", "");
    expect(g.perPrompt).toEqual({});
    expect(() => applyGuidanceEdit(g, "bogus", "x")).toThrow();
  });
  it("caps history", () => {
    let g = null;
    for (let i = 0; i < 30; i++) g = applyGuidanceEdit(g, "global", `v${i}`);
    expect(g.history).toHaveLength(MAX_GUIDANCE_HISTORY);
  });
  it("appends lines and refuses to exceed the cap", () => {
    let g = appendGuidanceLine(null, "fill_answer", "one", { source: "suggestion" });
    g = appendGuidanceLine(g, "fill_answer", "two", { source: "suggestion" });
    expect(g.perPrompt.fill_answer).toBe("one\ntwo");
    expect(g.history.at(-1).source).toBe("suggestion");
    expect(() => appendGuidanceLine(g, "fill_answer", "y".repeat(800))).toThrow();
  });
  it("normalizes junk", () => {
    expect(normalizeGuidance("x")).toEqual({ global: "", perPrompt: {}, history: [] });
  });
});

describe("feedback and request", () => {
  it("counts per category", () => {
    let fb = recordDeltaFeedback({}, "social", "approved");
    fb = recordDeltaFeedback(fb, "social", "discarded");
    expect(fb.social).toEqual({ approved: 1, discarded: 1 });
    expect(recordDeltaFeedback(fb, "social", "weird")).toBe(fb);
  });
  it("returns null without signals and builds a request with them", () => {
    expect(proposeGuidanceRequest({})).toBeNull();
    const req = proposeGuidanceRequest({
      outcomes: [{ sig: { label: "phone" }, outcome: "edited" }, { sig: { label: "phone" }, outcome: "kept" }],
      deltaFeedback: { social: { approved: 1, discarded: 4 } }
    });
    expect(req).toContain("phone");
    expect(req).toContain("social");
  });
});

describe("parseGuidanceSuggestions", () => {
  const wrap = (s) => JSON.stringify({ suggestions: s });
  it("accepts valid suggestions", () => {
    const out = parseGuidanceSuggestions(wrap([{ promptId: "profile_extract", guidance: "Capture LinkedIn handle.", reason: "often discarded" }]));
    expect(out).toHaveLength(1);
  });
  it("rejects unknown ids, PII, vault values, malformed output", () => {
    const bad = [
      { promptId: "evil", guidance: "ok", reason: "r" },
      { promptId: "ask", guidance: "email me at a@b.com", reason: "r" },
      { promptId: "ask", guidance: "see https://x.io", reason: "r" },
      { promptId: "ask", guidance: "call 555-123-4567", reason: "r" },
      { promptId: "ask", guidance: "use Ari Example", reason: "r" },
      { promptId: "ask", guidance: "x".repeat(300), reason: "r" },
      { promptId: "ask", guidance: "", reason: "r" },
      { promptId: "ask", guidance: 5, reason: "r" }
    ];
    expect(parseGuidanceSuggestions(wrap(bad), { vaultValues: ["Ari Example"] })).toEqual([]);
    expect(parseGuidanceSuggestions("not json")).toEqual([]);
    expect(parseGuidanceSuggestions('{"suggestions":5}')).toEqual([]);
  });
});

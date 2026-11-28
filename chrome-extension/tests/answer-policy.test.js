import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { checkGrounding, isCommitmentQuery, isConsentField, isUserConfirmed, makeSuggestion, supportedSentences } from "../extension/lib/answer-policy.js";

const FACTS = ["Employer: Acme Robotics", "Role: Software engineer working on robot perception code", "Bio: Builds computer vision tools in Python"];

describe("answer policy", () => {
  it("classifies commitments and consent", () => {
    for (const q of ["Are you legally authorized to work in the US?", "Will you require sponsorship?", "Are you able to work entirely on-site?", "Are you willing to relocate?", "Earliest start date"]) expect(isCommitmentQuery(q)).toBe(true);
    expect(isCommitmentQuery("Why do you want this job?")).toBe(false);
    expect(isConsentField({ inputType: "checkbox", query: "I agree to the Terms and Privacy Policy" })).toBe(true);
    expect(isConsentField({ inputType: "checkbox", semanticCategory: "legal.consent", query: "x" })).toBe(true);
    expect(isConsentField({ inputType: "checkbox", query: "Subscribe to updates" })).toBe(false);
  });
  it("rejects a number-free false claim", () => {
    const r = supportedSentences("I led the entire platform organization and shipped the payments system. I build computer vision tools in Python.", FACTS);
    expect(r.dropped.join(" ")).toMatch(/led the entire platform/);
    expect(r.kept.join(" ")).toMatch(/computer vision/);
  });
  it("rejects an invention that reuses a real name", () => {
    const r = supportedSentences("At Acme Robotics I launched customer onboarding dashboards and negotiated enterprise contracts.", FACTS);
    expect(r.kept).toEqual([]);
  });
  it("rejects invented numbers, the product name, empty facts and the refusal token", () => {
    expect(checkGrounding("Cut latency by 40%.", FACTS.join("\n"), {}).ok).toBe(false);
    expect(checkGrounding("I built LocalForm AI.", FACTS.join("\n"), { banned: ["LocalForm AI"] }).ok).toBe(false);
    expect(checkGrounding("Anything", "", {}).reason).toBe("no-facts");
    expect(checkGrounding("NO SUPPORTED ANSWER", FACTS.join("\n"), {}).ok).toBe(false);
  });
  it("legacy or flagged entries are not user-confirmed", () => {
    expect(isUserConfirmed({ value: "Yes", tags: ["confirmed"], source: "user", confirmed: true })).toBe(false);
    expect(isUserConfirmed({ value: "Yes" })).toBe(false);
    expect(isUserConfirmed({ value: "Yes", userConfirmation: { by: "user", at: "2026-11-28", value: "Yes", scope: "Q1" } }, "q1")).toBe(true);
    expect(isUserConfirmed({ value: "Yes", userConfirmation: { by: "user", at: "2026-11-28", value: "Yes" } }, "Q1")).toBe(false);
    expect(isUserConfirmed({ value: "No", userConfirmation: { by: "user", at: "2026-11-28", value: "Yes", scope: "Q" } }, "Q")).toBe(false);
  });
  it("suggestions are pending and carry provenance", () => {
    const s = makeSuggestion({ fieldId: "f1", query: "q" }, { value: "v", source: "ai-draft" });
    expect(s.status).toBe("pending");
    expect(s.verified).toBe(false);
    expect(s.entryId).toBeNull();
  });
});

describe("sidepanel wiring (static)", () => {
  const src = readFileSync(join(import.meta.dirname, "../extension/sidepanel.js"), "utf8");
  const html = readFileSync(join(import.meta.dirname, "../extension/sidepanel.html"), "utf8");
  it("has no auto-consent control or default", () => {
    expect(html).not.toMatch(/chk-agree-consent/);
    expect(src).not.toMatch(/field\.value = true;\s*\n\s*field\.confidence = 0\.95/);
  });
  it("model output only reaches suggestions, never field.value or a live DOM write", () => {
    expect(src.slice(src.indexOf("Tailor \"${field.query}\""), src.indexOf("session.destroy"))).not.toMatch(/onChunk|HYDRATE_PAGE_FORMS/);
    expect(src).not.toMatch(/field\.value = finalText/);
    expect(src).not.toMatch(/field\.value = activeSessionContext\.preDraftedCoverLetter/);
  });
  it("review accept strips vault ids so nothing is learned", () => {
    expect(src).toMatch(/pass: undefined, entryId: undefined, entryKey: undefined/);
  });
});

import { deduplicateVault } from "../extension/lib/vault.js";
describe("vault dedupe and commitments", () => {
  it("does not merge identical values of different commitment entries", () => {
    const v = deduplicateVault([
      { id: "a", key: "Relocation", value: "No", category: "professional" },
      { id: "b", key: "Sponsorship", value: "No", category: "professional", userConfirmation: { by: "user", at: "x", value: "No" } }
    ]);
    expect(v.map((e) => e.id).sort()).toEqual(["a", "b"]);
    expect(v.find((e) => e.id === "a").aliases).toBeUndefined();
  });
});

describe("confirmation scope", () => {
  it("onsite Yes confirmed does not authorize relocation Yes via alias", () => {
    const e = { value: "Yes", aliases: ["willing to relocate"], userConfirmation: { by: "user", at: "t", value: "Yes", scope: "Are you able to work entirely on-site?" } };
    expect(isUserConfirmed(e, "Are you able to work entirely on-site?")).toBe(true);
    expect(isUserConfirmed(e, "Are you willing to relocate?")).toBe(false);
  });
});

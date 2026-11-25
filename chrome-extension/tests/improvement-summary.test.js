import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { LINK_TARGETS, LOOP, improvementSummary } from "../extension/lib/improvement-summary.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "../extension/sidepanel.html"), "utf8");
const panelSrc = readFileSync(path.join(here, "../extension/sidepanel.js"), "utf8");

const item = (s, id) => s.items.find((i) => i.id === id);
const fill = (fillId, outcome) => ({ fillId, ts: 1, outcome });
const mapping = (sig, status) => ({ sig, status });
const guidance = { global: "Keep it short.", perPrompt: { ask: "Be brief." }, history: [{ at: 1, source: "suggestion", before: {}, after: {} }] };

describe("improvementSummary", () => {
  it("empty state explains the loop and points at Observer", () => {
    const s = improvementSummary();
    expect(s.loop.some((l) => l.kind === "auto")).toBe(true);
    expect(s.loop.some((l) => l.kind === "you")).toBe(true);
    expect(item(s, "vault").text).toBe("0 facts in your vault.");
    expect(item(s, "fills").text).toBe("No fills recorded yet.");
    expect(item(s, "guidance").text).toBe("No prompt guidance set.");
    expect(item(s, "samples").text).toBe("No samples saved.");
    expect(s.next.link).toBe(LINK_TARGETS.observe);
    expect(s.next.text).toMatch(/Turn on Observer/);
  });

  it("counts pending facts and makes them the first next step", () => {
    const s = improvementSummary({ vault: [{}, {}], pendingDeltas: [{}, {}, {}], learnedMappings: [mapping("a", "quarantined")] });
    expect(item(s, "vault").text).toBe("2 facts in your vault.");
    expect(item(s, "pending").text).toBe("3 suggested facts waiting for your review.");
    expect(s.next.link).toBe(LINK_TARGETS.pending);
  });

  it("separates confirmed from unconfirmed mappings", () => {
    const s = improvementSummary({
      learnedMappings: [mapping("a", "confirmed"), mapping("b", "quarantined"), mapping("c", "quarantined")],
      learnedAliases: [{}]
    });
    expect(item(s, "mappings").text).toBe("1 confirmed field mapping, 2 waiting for you to confirm. 1 learned alias.");
    expect(s.next.link).toBe(LINK_TARGETS.mappings);
  });

  it("reports the last fill and kept rate", () => {
    const s = improvementSummary({ fillOutcomes: [fill("f1", "edited"), fill("f2", "kept"), fill("f2", "kept"), fill("f2", "cleared")] });
    expect(item(s, "fills").text).toBe("2 fills recorded. Last fill: 2 kept, 0 edited, 1 cleared. 50% of fields kept over recent fills.");
  });

  it("counts guidance lines and names the source of the last edit", () => {
    const s = improvementSummary({ promptGuidance: guidance, fillOutcomes: [fill("f1", "kept")] });
    expect(item(s, "guidance").text).toBe("2 guidance lines active (1 global, 1 per prompt). Last edit by an accepted suggestion.");
  });

  it("shows the last sample score, or saved samples not yet run", () => {
    const cases = [{ input: "q", expect: { maxWords: 20 } }];
    expect(item(improvementSummary({ promptEvalCases: cases }), "samples").text).toBe("1 sample saved, not run yet.");
    const s = improvementSummary({ promptEvalCases: cases, promptEvalRuns: [{ passed: 1, total: 2, score: 0.75 }] });
    expect(item(s, "samples").text).toBe("Last sample run: 1/2 passed, score 75%.");
  });

  it("next step priority: pending, mappings, no fills, suggest, run samples, nothing", () => {
    const fills = [fill("f1", "kept")];
    const cases = [{ input: "q", expect: { maxWords: 20 } }];
    const text = (o) => improvementSummary(o).next.text;
    expect(text({ pendingDeltas: [{}], learnedMappings: [mapping("a", "quarantined")] })).toMatch(/Review the 1 suggested fact/);
    expect(text({ learnedMappings: [mapping("a", "quarantined")] })).toMatch(/Confirm or delete/);
    expect(text({})).toMatch(/Turn on Observer/);
    expect(text({ fillOutcomes: fills })).toMatch(/Suggest improvements/);
    expect(text({ fillOutcomes: fills, promptGuidance: guidance, promptEvalCases: cases })).toMatch(/Run samples/);
    expect(text({ fillOutcomes: fills, promptGuidance: guidance })).toBe("Nothing waiting.");
  });
});

describe("improvement card wiring", () => {
  it("every link targets an element and tab that exist in sidepanel.html", () => {
    for (const { tab, el } of Object.values(LINK_TARGETS)) {
      expect(html).toContain(`id="${el}"`);
      expect(html).toContain(`data-tab="${tab}"`);
    }
  });

  it("the card ids the panel looks up exist, and the card comes first in the Observer tab", () => {
    for (const id of ["improve-card", "improve-loop", "improve-items", "improve-next"]) {
      expect(html).toContain(`id="${id}"`);
      if (id !== "improve-card") expect(panelSrc).toContain(`"${id}"`);
    }
    const observer = html.slice(html.indexOf('id="tab-observer"'));
    expect(observer.indexOf('id="improve-card"')).toBeGreaterThan(-1);
    expect(observer.indexOf('id="improve-card"')).toBeLessThan(observer.indexOf('id="btn-observe-toggle"'));
  });

  it("loop copy has both kinds", () => {
    expect(LOOP.map((l) => l.kind)).toEqual(["auto", "auto", "auto", "you"]);
  });
});

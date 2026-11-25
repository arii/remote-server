import { describe, it, expect } from "vitest";
import {
  allocateBudget,
  askTabEntries,
  buildAskPrompt,
  formatTargetContext,
  isReadableTabUrl,
  selectVaultFacts
} from "../extension/lib/ask-context.js";

describe("allocateBudget", () => {
  it("redistributes unused share to longer sources and never exceeds total", () => {
    const a = allocateBudget([10, 1000, 1000], 300);
    expect(a[0]).toBe(10);
    expect(a.reduce((x, y) => x + y, 0)).toBeLessThanOrEqual(300);
    expect(a[1]).toBeGreaterThan(100);
    expect(Math.abs(a[1] - a[2])).toBeLessThanOrEqual(1);
  });
  it("gives everything when it fits", () => {
    expect(allocateBudget([5, 6], 100)).toEqual([5, 6]);
  });
});

describe("buildAskPrompt", () => {
  const big = "word ".repeat(5000);
  it("never exceeds budgetChars", () => {
    for (const budget of [800, 2000, 4500, 6000]) {
      const { prompt } = buildAskPrompt({
        question: "What is this page about?",
        sources: [
          { kind: "page", title: "P", url: "https://a.test", text: big },
          { kind: "tab", title: "T", url: "https://b.test", text: big },
          { kind: "vault", text: "short fact" }
        ],
        guidance: { global: "Be brief." },
        budgetChars: budget
      });
      expect(prompt.length).toBeLessThanOrEqual(budget);
    }
  });
  it("labels sources, reports truncation, and keeps the missing-context instruction", () => {
    const { prompt, used } = buildAskPrompt({
      question: "Q?",
      sources: [
        { kind: "page", title: "Home", url: "https://a.test", text: big },
        { kind: "vault", text: "Name: Ada" }
      ],
      budgetChars: 1200
    });
    expect(prompt).toContain("[Source 1: page - Home (https://a.test)]");
    expect(prompt).toContain("[Source 2: vault]");
    expect(prompt).toContain("say what is missing");
    expect(used[0].truncated).toBe(true);
    expect(used[1].truncated).toBe(false);
  });
  it("skips empty sources and includes guidance", () => {
    const { prompt, used } = buildAskPrompt({
      question: "Q?",
      sources: [{ kind: "tab", text: "   " }],
      guidance: { perPrompt: { ask: "Answer in one line." } }
    });
    expect(used).toEqual([]);
    expect(prompt).toContain("no context sources");
    expect(prompt).toContain("Answer in one line.");
  });
  it("requires a question and rejects an impossible budget", () => {
    expect(() => buildAskPrompt({ question: " " })).toThrow();
    expect(() => buildAskPrompt({ question: "Q", budgetChars: 50 })).toThrow(/budget/);
  });
});

describe("selectVaultFacts", () => {
  const vault = [
    { id: "a", key: "GitHub Profile URL", value: "https://github.com/ada", category: "social" },
    { id: "b", key: "Employer", value: "Acme Robotics engineer", category: "experience" },
    { id: "c", key: "Account password", value: "hunter2 github" },
    { id: "d", key: "Notes", value: "my ssn is 123 github" },
    { id: "ssn", key: "Other", value: "github" },
    { id: "e", key: "Favorite color", value: "green" }
  ];
  it("ranks by keyword relevance and excludes sensitive entries", () => {
    const out = selectVaultFacts("What is my github link?", vault, 8);
    expect(out.map((e) => e.id)).toEqual(["a"]);
  });
  it("orders key matches above value matches and honors max", () => {
    const out = selectVaultFacts("employer acme", vault, 1);
    expect(out.map((e) => e.id)).toEqual(["b"]);
  });
  it("returns nothing when no entry is relevant", () => {
    expect(selectVaultFacts("quantum", vault)).toEqual([]);
  });
});

describe("helpers", () => {
  it("formats target context and filters tab urls", () => {
    expect(formatTargetContext({ role: "SWE", organization: "Acme" })).toBe("Role: SWE\nOrganization: Acme");
    expect(formatTargetContext(null)).toBe("");
    expect(isReadableTabUrl("https://x.test")).toBe(true);
    expect(isReadableTabUrl("chrome://settings")).toBe(false);
    expect(isReadableTabUrl("https://chromewebstore.google.com/x")).toBe(false);
  });
});

describe("askTabEntries", () => {
  const tab = (id, title, url) => ({ id, title, url });
  it("excludes active tab, counts unreadable tabs", () => {
    const r = askTabEntries([tab(1, "A", "https://a.test/"), tab(2, "B", "https://b.test/x"), tab(3, "Settings", "chrome://settings")], 1);
    expect(r.entries.map((e) => e.id)).toEqual([2]);
    expect(r.unreadableCount).toBe(1);
  });
  it("falls back to hostname for JSON, untitled, empty and url titles", () => {
    const r = askTabEntries([
      tab(2, '{"languageModel":"available"}', "https://j.test/status"),
      tab(3, "Untitled", "https://u.test/"),
      tab(4, "", "https://e.test/p"),
      tab(5, "https://s.test/p", "https://s.test/p"),
      tab(6, "[1,2]", "https://k.test/")
    ], 1);
    expect(r.entries.map((e) => e.label)).toEqual(["j.test", "u.test", "e.test", "s.test", "k.test"]);
  });
  it("collapses whitespace and truncates to 60 chars", () => {
    const r = askTabEntries([tab(2, "  Hello \n  world  ", "https://a.test/"), tab(3, "x".repeat(100), "https://b.test/")], 1);
    expect(r.entries[0].label).toBe("Hello world");
    expect(r.entries[1].label).toHaveLength(60);
  });
  it("handles missing input", () => {
    expect(askTabEntries(undefined, 1)).toEqual({ entries: [], unreadableCount: 0 });
  });
});

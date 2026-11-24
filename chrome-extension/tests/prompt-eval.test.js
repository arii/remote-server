import { describe, expect, it } from "vitest";
import { MAX_EVAL_CASES, casesKey, normalizeCases, removeCase, scoreOutput, suggestFromFailures, summarizeRun } from "../extension/lib/prompt-eval.js";
import { appendGuidanceLine, normalizeGuidance, revertGuidance, MAX_SUGGESTIONS } from "../extension/lib/prompt-guidance.js";
import { getPrompt } from "../extension/lib/prompts.js";

describe("normalizeCases", () => {
  it("caps count and input length, drops empty inputs, cleans terms", () => {
    const raw = Array.from({ length: 15 }, (_, i) => ({ input: `q${i}`, expect: { mustContain: [" a ", "A", ""], maxWords: "20" } }));
    raw.unshift({ input: "   " });
    const out = normalizeCases(raw);
    expect(out).toHaveLength(MAX_EVAL_CASES);
    expect(out[0].expect).toEqual({ mustContain: ["a"], mustNotContain: [], maxWords: 20 });
    expect(normalizeCases([{ input: "x".repeat(900), expect: { maxWords: 5 } }])[0].input).toHaveLength(500);
    expect(normalizeCases("nope")).toEqual([]);
  });
  it("drops cases with no usable expectation", () => {
    expect(normalizeCases([{ input: "x" }, { input: "y", expect: { maxWords: -3, mustContain: [" "] } }])).toEqual([]);
  });
});

describe("casesKey", () => {
  it("is stable for equal sets and differs when the set changes", () => {
    const a = normalizeCases([{ input: "q", expect: { maxWords: 5 } }]);
    const b = normalizeCases([{ input: "q", expect: { maxWords: 6 } }]);
    expect(casesKey(a)).toBe(casesKey(normalizeCases(a)));
    expect(casesKey(a)).not.toBe(casesKey(b));
  });
});

describe("removeCase", () => {
  const set = normalizeCases([
    { input: "a", expect: { maxWords: 5 } },
    { input: "b", expect: { maxWords: 6 } }
  ]);
  it("removes the case at the index and frees a slot at the limit", () => {
    expect(removeCase(set, 0).map((c) => c.input)).toEqual(["b"]);
    const full = Array.from({ length: MAX_EVAL_CASES }, (_, i) => ({ input: `q${i}`, expect: { maxWords: 5 } }));
    expect(removeCase(full, 3)).toHaveLength(MAX_EVAL_CASES - 1);
  });
  it("leaves the set unchanged for an out-of-range index", () => {
    expect(removeCase(set, 5)).toEqual(set);
  });
  it("changes casesKey so earlier runs are not comparable", () => {
    expect(casesKey(removeCase(set, 1))).not.toBe(casesKey(set));
  });
});

describe("scoreOutput", () => {
  it("reports each failure type", () => {
    const r = scoreOutput("one two three four", { mustContain: ["Nano", "one"], mustNotContain: ["three"], maxWords: 3 });
    expect(r.pass).toBe(false);
    expect(r.failures).toEqual([
      { type: "missing", term: "Nano" },
      { type: "forbidden", term: "three" },
      { type: "too_long", limit: 3 }
    ]);
    expect(r.score).toBeCloseTo(1 / 4);
  });
  it("passes when expectations hold", () => {
    expect(scoreOutput("local nano", { mustContain: ["nano"], mustNotContain: [], maxWords: 5 })).toEqual({ pass: true, score: 1, failures: [] });
  });
});

describe("summarizeRun", () => {
  it("computes counts, score and delta", () => {
    const s = summarizeRun([{ pass: true, score: 1 }, { pass: false, score: 0.5 }], 0.5);
    expect(s).toEqual({ passed: 1, total: 2, score: 0.75, delta: 0.25 });
    expect(summarizeRun([], null).delta).toBeNull();
  });
});

describe("suggestFromFailures", () => {
  const fail = (failures) => ({ pass: false, score: 0, failures });
  it("maps failures to rules and dedupes", () => {
    const out = suggestFromFailures([
      fail([{ type: "too_long", limit: 40 }, { type: "missing", term: "privacy" }]),
      fail([{ type: "too_long", limit: 40 }, { type: "forbidden", term: "cloud" }])
    ]);
    expect(out.map((s) => s.guidance)).toEqual(['Keep answers under 40 words.', 'Mention "privacy" when relevant.', 'Never use the word "cloud".']);
    expect(out.every((s) => s.promptId === "ask")).toBe(true);
  });
  it("drops PII, vault values, and guidance that already exists", () => {
    const out = suggestFromFailures(
      [fail([{ type: "missing", term: "me@x.com" }, { type: "missing", term: "Zephyrine" }, { type: "too_long", limit: 40 }])],
      { vaultValues: ["Zephyrine"], guidance: { global: "Keep answers under 40 words.", perPrompt: {} } }
    );
    expect(out).toEqual([]);
  });
  it("caps suggestions", () => {
    const failures = Array.from({ length: 12 }, (_, i) => ({ type: "missing", term: `term${i}` }));
    expect(suggestFromFailures([fail(failures)])).toHaveLength(MAX_SUGGESTIONS);
  });
});

describe("improvement loop with mocked LanguageModel", () => {
  // Mock obeys the "under N words" guidance only when it is in the prompt.
  const mockModel = {
    async create() {
      return {
        async prompt(p) {
          return /Keep answers under 5 words\./.test(p) ? "Short local answer." : "This is a very long rambling answer about everything.";
        },
        destroy() {}
      };
    }
  };
  async function run(cases, guidance) {
    const results = [];
    for (const c of cases) {
      const s = await mockModel.create();
      results.push(scoreOutput(await s.prompt(getPrompt("ask", { guidance, vars: { question: c.input } })), c.expect));
      s.destroy();
    }
    return results;
  }

  it("fails, suggests, accepts, passes with positive delta, and reverts", async () => {
    const cases = normalizeCases([{ input: "Why local?", expect: { maxWords: 5 } }]);
    let guidance = normalizeGuidance({});
    const first = await run(cases, guidance);
    const sum1 = summarizeRun(first, null);
    expect(sum1.passed).toBe(0);

    const [sug] = suggestFromFailures(first, { guidance });
    expect(sug.guidance).toBe("Keep answers under 5 words.");
    expect(getPrompt("ask", { guidance, vars: { question: "q" } })).not.toContain(sug.guidance);

    guidance = appendGuidanceLine(guidance, sug.promptId, sug.guidance, { source: "suggestion" });
    expect(getPrompt("ask", { guidance, vars: { question: "q" } })).toContain(sug.guidance);

    const sum2 = summarizeRun(await run(cases, guidance), sum1.score);
    expect(sum2.passed).toBe(1);
    expect(sum2.delta).toBeGreaterThan(0);

    guidance = revertGuidance(guidance);
    expect(getPrompt("ask", { guidance, vars: { question: "q" } })).not.toContain(sug.guidance);
  });
});

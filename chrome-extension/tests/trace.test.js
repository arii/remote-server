import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { buildTraceEntry } from "../extension/lib/trace.js";

const FAKE_SECRET_TITLE = "sk-FAKE0000000000000000000000";
const FAKE_SECRET_BEARER = "Bearer FAKEtoken123456";
const FAKE_SECRET_KEY = "FAKEKEY123456";

function sourcePath() {
  return resolve(dirname(fileURLToPath(import.meta.url)), "../extension/sidepanel.js");
}

describe("buildTraceEntry", () => {
  it("redacts secrets in title for LLM_PROMPT and ERROR", () => {
    for (const type of ["LLM_PROMPT", "ERROR"]) {
      const entry = buildTraceEntry({
        type,
        title: `Call ${FAKE_SECRET_TITLE}`,
        durationMs: 10,
        details: "ok",
      });
      expect(entry.title).not.toContain(FAKE_SECRET_TITLE);
      expect(entry.title).toContain("Call");
    }
  });

  it("redacts secrets in string details", () => {
    const entry = buildTraceEntry({
      type: "ERROR",
      title: "failure",
      durationMs: 20,
      details: `Authorization: ${FAKE_SECRET_BEARER}`,
    });
    expect(entry.details).not.toContain(FAKE_SECRET_BEARER);
  });

  it("redacts secrets in object details after stringification", () => {
    const input = {
      type: "LLM_PROMPT",
      title: "prompt",
      durationMs: 5,
      details: {
        request: { url: "https://example.com", query: `?key=${FAKE_SECRET_KEY}` },
        body: { apiKey: FAKE_SECRET_TITLE },
      },
    };
    const entry = buildTraceEntry(input);
    expect(entry.details).not.toContain(FAKE_SECRET_KEY);
    expect(entry.details).not.toContain(FAKE_SECRET_TITLE);
  });

  it("handles undefined details", () => {
    const entry = buildTraceEntry({ type: "SCAN", title: "scan", durationMs: 0, details: undefined });
    expect(entry.details).toBe("");
  });

  it("handles undefined title", () => {
    const entry = buildTraceEntry({ type: "ERROR", title: undefined, durationMs: 1 });
    expect(entry.title).toBe("");
  });

  it("rounds durationMs", () => {
    expect(buildTraceEntry({ type: "HYDRATE", title: "t", durationMs: 123.6, details: "d" }).durationMs).toBe(124);
    expect(buildTraceEntry({ type: "HYDRATE", title: "t", durationMs: undefined, details: "d" }).durationMs).toBe(0);
  });

  it("does not mutate the input object", () => {
    const frozenDetails = Object.freeze({ apiKey: FAKE_SECRET_TITLE, nested: { token: FAKE_SECRET_KEY } });
    const input = Object.freeze({
      type: "ERROR",
      title: FAKE_SECRET_TITLE,
      durationMs: 3,
      details: frozenDetails,
    });
    const entry = buildTraceEntry(input);
    expect(input.title).toBe(FAKE_SECRET_TITLE);
    expect(input.details.apiKey).toBe(FAKE_SECRET_TITLE);
    expect(entry.details).not.toContain(FAKE_SECRET_TITLE);
    expect(entry.details).not.toContain(FAKE_SECRET_KEY);
  });

  it("leaves ordinary text unchanged", () => {
    const entry = buildTraceEntry({
      type: "OBSERVER",
      title: "Simple title",
      durationMs: 42,
      details: "No secrets here",
    });
    expect(entry.title).toBe("Simple title");
    expect(entry.details).toBe("No secrets here");
  });

  it("stamps id and ts from now", () => {
    const now = new Date(2024, 0, 1, 12, 30, 45, 123);
    const entry = buildTraceEntry({ type: "SCAN", title: "t", durationMs: 1, details: "d" }, now);
    expect(Math.floor(entry.id)).toBe(now.getTime());
    expect(entry.id).toBeGreaterThanOrEqual(now.getTime());
    expect(entry.id).toBeLessThan(now.getTime() + 1);
    expect(entry.ts).toBe(now.toLocaleTimeString());
  });
});

describe("sidepanel.js integration", () => {
  it("imports buildTraceEntry and uses it in logTrace", () => {
    const source = readFileSync(sourcePath(), "utf8");
    expect(source).toContain('import { buildTraceEntry } from "./lib/trace.js";');
    expect(source).toContain("const entry = buildTraceEntry({ type, title, durationMs, details });");
    expect(source).not.toContain("Date.now() + Math.random()");
  });
});

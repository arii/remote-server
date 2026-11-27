import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { describeTaskFailure } from "../extension/lib/model-download.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const sidepanelSource = readFileSync(path.join(here, "../extension/sidepanel.js"), "utf8");

const FAKE_SECRET = "sk-FAKE0000000000000000000000";
const FAKE_BEARER = "Bearer FAKEtoken123456";

describe("sidepanel error surfaces use describeTaskFailure and redactSecrets", () => {
  it("uses describeTaskFailure for Prompt, Scan, UX Audit, and Auto-Fill", () => {
    expect(sidepanelSource).toContain('describeTaskFailure(err, "Prompt")');
    expect(sidepanelSource).toContain('describeTaskFailure(err, "Scan")');
    expect(sidepanelSource).toContain('describeTaskFailure(err, "UX Audit")');
    expect(sidepanelSource).toContain('describeTaskFailure(err, "Auto-Fill")');
  });

  it("uses redactSecrets in sidepanel.js", () => {
    expect(sidepanelSource).toContain("redactSecrets(");
  });
});

describe("describeTaskFailure redacts secrets", () => {
  const labels = ["Prompt", "Scan", "UX Audit", "Auto-Fill"];

  for (const label of labels) {
    it(`redacts raw messages for ${label}`, () => {
      const result = describeTaskFailure(new Error(`${FAKE_SECRET} and ${FAKE_BEARER}`), label);
      expect(result.startsWith(`${label} failed:`)).toBe(true);
      expect(result).not.toContain(FAKE_SECRET);
      expect(result).not.toContain("FAKEtoken123456");
    });
  }
});

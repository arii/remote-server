// Guards the AI Model tab copy: the panel must not claim all processing is
// on-device once the optional Firebase AI Logic hybrid mode can be enabled,
// and must surface a visible hybrid-mode indicator when it is active.
import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, "..", "extension", "sidepanel.html"), "utf8");
const js = fs.readFileSync(path.join(here, "..", "extension", "sidepanel.js"), "utf8");

describe("hybrid UI copy", () => {
  it("does not claim exclusively on-device processing", () => {
    expect(html.includes("Runs 100% on-device")).toBe(false);
    expect(html).toContain("hybrid: on-device first, cloud fallback");
  });

  it("has a hidden hybrid-mode line shown only when Firebase is configured", () => {
    expect(html).toContain('id="hybrid-mode-line"');
    expect(js).toMatch(/function showHybridModeLine\(\)/);
    expect(js).toMatch(/if \(hybrid\) \{\s*showHybridModeLine\(\);/);
  });
});

// Setup instructions must not turn model readiness into a flags requirement.
describe("stable extension setup copy", () => {
  const readme = fs.readFileSync(path.join(here, "..", "README.md"), "utf8");
  const modelErrors = fs.readFileSync(path.join(here, "..", "extension", "lib", "model-download.js"), "utf8");
  it("removes obsolete flag setup from docs and error paths", () => {
    for (const source of [readme, html, js, modelErrors]) {
      expect(source).not.toContain("chrome://flags");
      expect(source).toContain("chrome://on-device-internals");
    }
    expect(readme).toContain("Chrome 138");
    expect(readme).toContain("Writer is still a developer-trial API");
  });
  it("keeps availability, download and cloud evidence separate", () => {
    for (const state of ["available", "downloadable", "downloading", "unavailable"]) {
      expect(readme).toContain("`" + state + "`");
    }
    expect(readme).toContain("does not prove Gemini Nano is available locally");
    expect(readme).toContain("more than 4 GB GPU VRAM");
    expect(readme).toContain("22 GB free");
  });
});

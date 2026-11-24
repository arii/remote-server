import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { isClientIdConfigured } from "../extension/lib/drive-import.js";

const text = readFileSync(resolve(process.cwd(), "extension/manifest.json"), "utf8");

// JSON.parse silently keeps the last duplicate, so scan the raw text for
// keys at object depth 1.
function topLevelKeys(src) {
  const keys = [];
  let depth = 0;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (ch === '"') {
      let j = i + 1;
      while (src[j] !== '"') j += src[j] === "\\" ? 2 : 1;
      if (depth === 1 && /^\s*:/.test(src.slice(j + 1, j + 20))) keys.push(JSON.parse(src.slice(i, j + 1)));
      i = j;
    } else if (ch === "{" || ch === "[") depth++;
    else if (ch === "}" || ch === "]") depth--;
  }
  return keys;
}

describe("manifest.json", () => {
  it("has no duplicate top-level keys", () => {
    const keys = topLevelKeys(text);
    const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
    expect(dupes).toEqual([]);
  });

  it("has a configured (non-placeholder) oauth2 client_id", () => {
    const manifest = JSON.parse(text);
    expect(isClientIdConfigured(manifest.oauth2?.client_id)).toBe(true);
  });

  it("requests only the Drive scopes the code uses", () => {
    const manifest = JSON.parse(text);
    expect(manifest.oauth2.scopes).toEqual([
      "https://www.googleapis.com/auth/drive.readonly",
      "https://www.googleapis.com/auth/drive.appdata"
    ]);
  });
});

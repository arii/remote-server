import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { aliasMatches, matchField, resolveLabel } from "../extension/lib/matcher.js";

const vault = [
  { id: 1, key: "email" },
  { id: 2, key: "phone" },
  { id: 3, key: "name" },
  { id: 4, key: "news" }
];

describe("label decoration matching", () => {
  it("is strict without stripping", () => {
    expect(aliasMatches("Email (required)", "email")).toBe(false);
    expect(aliasMatches("Email", "email")).toBe(true);
  });

  it("matchField strips markers for non-checkbox/radio", () => {
    expect(matchField({ label: "Email (required)", type: "email" }, vault)?.entry.id).toBe(1);
    expect(matchField({ label: "Email (optional)", type: "text" }, vault)?.entry.id).toBe(1);
    expect(matchField({ label: "Phone [required]", type: "tel" }, vault)?.entry.id).toBe(2);
    expect(matchField({ label: "Email *", type: "text" }, vault)?.entry.id).toBe(1);
  });

  it("does not strip markers for checkbox/radio", () => {
    expect(matchField({ label: "News (optional)", type: "checkbox" }, vault)).toBeNull();
    expect(matchField({ label: "News (optional)", type: "radio" }, vault)).toBeNull();
  });

  it("still matches same label for text", () => {
    expect(matchField({ label: "News (optional)", type: "text" }, vault)?.entry.id).toBe(4);
  });

  it("rejects other partial labels", () => {
    expect(matchField({ label: "Company name (required)", type: "text" }, vault)).toBeNull();
    expect(matchField({ label: "Email (work)", type: "text" }, vault)).toBeNull();
    expect(matchField({ label: "Work email (required)", type: "text" }, vault)).toBeNull();
  });
});

describe("real form integration", () => {
  const html = readFileSync(join(import.meta.dirname, "fixtures", "lever-form.html"), "utf8");
  const vault = JSON.parse(readFileSync(join(import.meta.dirname, "..", "eval", "eval-vault.json"), "utf8"));
  const doc = new DOMParser().parseFromString(html, "text/html");

  it("resolves label from fixture and matches phone with alias", () => {
    const el = doc.getElementById("phone");
    const label = resolveLabel(el, doc);
    expect(label).toContain("optional");
    const result = matchField({ label, name: "phone", type: "tel" }, vault);
    expect(result.entry.id).toBe("basics.phone");
    expect(result.pass).toBe("alias");
  });

  it("does not match full_name for phone optional", () => {
    const result = matchField({ label: "Phone (optional)" }, vault);
    expect(result?.entry?.id).not.toBe("basics.full_name");
  });

  it("does not match full_name for company name required", () => {
    const result = matchField({ label: "Company name (required)" }, vault);
    expect(result?.entry?.id).not.toBe("basics.full_name");
  });

  it("does not match phone for work phone optional", () => {
    const result = matchField({ label: "Work phone (optional)" }, vault);
    expect(result?.entry?.id).not.toBe("basics.phone");
  });
});

// Golden matcher tests over the committed form fixtures.
//
// The expected decisions live in fixtures/expected-matches.json and encode
// the strict aliasMatches rules as shipped (OD-19 behavior freeze): short
// aliases match exactly only, longer aliases match as whole words. Changing
// an expectation to make a test pass is a behavior change, not a fix.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { aliasMatches, escapeRegExp, normalizeLabel } from "../extension/lib/matcher.js";

const FIXTURES_DIR = join(import.meta.dirname, "fixtures");
const FIXTURE_FILES = ["ashby-form.html", "greenhouse-form.html", "lever-form.html"];
const golden = JSON.parse(readFileSync(join(FIXTURES_DIR, "expected-matches.json"), "utf8"));

// Every candidate label source the content script's waterfall reads for the
// priority levels these fixtures exercise: <label> elements (explicit and
// wrapping), [class*="label"] containers, aria-label, and placeholder.
function collectFixtureLabels(fixtureFile) {
  const html = readFileSync(join(FIXTURES_DIR, fixtureFile), "utf8");
  const doc = new DOMParser().parseFromString(html, "text/html");
  const labels = new Set();
  doc.querySelectorAll('label, [class*="label"]').forEach((el) => {
    // Mirror the waterfall's wrapping-label handling: form controls inside a
    // <label> contribute no text (a wrapped <select> otherwise leaks option
    // text into the label).
    const clone = el.cloneNode(true);
    clone.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
    const t = normalizeLabel(clone.textContent);
    if (t) labels.add(t);
  });
  doc.querySelectorAll("[aria-label], [placeholder]").forEach((el) => {
    for (const attr of ["aria-label", "placeholder"]) {
      const t = normalizeLabel(el.getAttribute(attr));
      if (t) labels.add(t);
    }
  });
  return labels;
}

describe("golden matcher decisions over form fixtures", () => {
  it("covers all three committed fixtures", () => {
    const used = new Set(golden.cases.map((c) => c.fixture));
    expect([...used].sort()).toEqual([...FIXTURE_FILES].sort());
  });

  for (const file of FIXTURE_FILES) {
    it(`grounds every ${file} case in a label that exists in the fixture`, () => {
      const labels = collectFixtureLabels(file);
      const cases = golden.cases.filter((c) => c.fixture === file);
      expect(cases.length).toBeGreaterThan(0);
      for (const c of cases) {
        expect(labels, `label "${c.query}" not found in ${file}`).toContain(normalizeLabel(c.query));
      }
    });

    it(`matches today's decisions for ${file}`, () => {
      for (const c of golden.cases.filter((c) => c.fixture === file)) {
        expect(aliasMatches(c.query, c.alias), `${file}: "${c.alias}" vs "${c.query}" (${c.why})`).toBe(c.expect);
      }
    });
  }
});

describe("aliasMatches strict rules", () => {
  it("matches exact queries regardless of alias length", () => {
    expect(aliasMatches("name", "name")).toBe(true);
    expect(aliasMatches("First Name", "first name")).toBe(true);
  });

  it("never lets a short alias match inside a longer query", () => {
    expect(aliasMatches("Company name", "name")).toBe(false);
    expect(aliasMatches("Mother's maiden name", "name")).toBe(false);
    expect(aliasMatches("Email address", "email")).toBe(false);
  });

  it("matches long aliases as whole words only", () => {
    expect(aliasMatches("Legal Full Name", "full name")).toBe(true);
    expect(aliasMatches("Company full name", "full name")).toBe(true);
    expect(aliasMatches("fullname", "full name")).toBe(false); // no word boundary inside a token
  });

  it("treats empty queries or aliases as no match", () => {
    expect(aliasMatches("", "name")).toBe(false);
    expect(aliasMatches("name", "")).toBe(false);
    expect(aliasMatches(null, "name")).toBe(false);
  });

  it("escapes regex metacharacters in aliases", () => {
    expect(() => aliasMatches("phone (optional)", "phone (optional)")).not.toThrow();
    expect(aliasMatches("phone (optional)", "phone (optional)")).toBe(true);
  });
});

describe("normalizeLabel", () => {
  it("lowercases, collapses whitespace, and trims", () => {
    expect(normalizeLabel("  First   NAME ")).toBe("first name");
    expect(normalizeLabel("Where are you currently based?")).toBe("where are you currently based?");
  });

  it("maps null and undefined to empty string", () => {
    expect(normalizeLabel(null)).toBe("");
    expect(normalizeLabel(undefined)).toBe("");
  });
});

describe("escapeRegExp", () => {
  it("escapes special characters", () => {
    expect(escapeRegExp("a.b*c")).toBe("a\\.b\\*c");
  });
});

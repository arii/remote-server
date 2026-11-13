// Golden matcher tests over the committed form fixtures.
//
// The expected decisions live in fixtures/expected-matches.json and encode
// the strict aliasMatches rules as shipped (OD-19 behavior freeze): short
// aliases match exactly only, longer aliases match as whole words. Changing
// an expectation to make a test pass is a behavior change, not a fix.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  aliasMatches,
  cleanLabelText,
  escapeRegExp,
  normalizeLabel,
  resolveLabel
} from "../extension/lib/matcher.js";
import { LEGACY_EXPORTS, toClassicMatcher } from "../scripts/legacy-matcher.mjs";

const FIXTURES_DIR = join(import.meta.dirname, "fixtures");
const FIXTURE_FILES = ["ashby-form.html", "greenhouse-form.html", "lever-form.html"];
const golden = JSON.parse(readFileSync(join(FIXTURES_DIR, "expected-matches.json"), "utf8"));

// Every golden case's `query` must be a label string the fixture actually
// renders and the shared matcher pipeline can read: either the shared
// resolver's output for a form control (lib/matcher.js resolveLabel — the
// waterfall moved out of the content script) or the raw text of a label
// source the waterfall scans (labels, [class*="label"] containers, legends,
// aria-label, placeholder — e.g. the Ashby question span that keys a radio
// group). This grounds the golden decisions in the fixture DOM itself; it
// replaces the D1 test that mirrored the waterfall by hand (a second copy
// of the logic is exactly what the shared module removes).
function fixtureControlLabels(fixtureFile) {
  const html = readFileSync(join(FIXTURES_DIR, fixtureFile), "utf8");
  const doc = new DOMParser().parseFromString(html, "text/html");
  const labels = new Set();
  doc.querySelectorAll('input, textarea, select').forEach((el) => {
    labels.add(normalizeLabel(resolveLabel(el, doc)));
  });
  doc
    .querySelectorAll('label, [class*="label"], [class*="Label"], legend, [aria-label], [placeholder]')
    .forEach((el) => {
      for (const raw of [el.textContent, el.getAttribute("aria-label"), el.getAttribute("placeholder")]) {
        const t = normalizeLabel(raw);
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
    it(`grounds every ${file} case in a label source the fixture renders`, () => {
      const labels = fixtureControlLabels(file);
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

describe("resolveLabel — the waterfall moved out of content-script.js", () => {
  const htmlFor = (f) =>
    new DOMParser().parseFromString(readFileSync(join(FIXTURES_DIR, f), "utf8"), "text/html");

  it("resolves explicit <label for> bindings (Greenhouse)", () => {
    const doc = htmlFor("greenhouse-form.html");
    expect(resolveLabel(doc.getElementById("first_name"), doc)).toBe("First Name");
    expect(resolveLabel(doc.getElementById("email"), doc)).toBe("Email Address");
  });

  it("keeps instruction spans in the explicit label (Greenhouse cover letter)", () => {
    const doc = htmlFor("greenhouse-form.html");
    expect(resolveLabel(doc.getElementById("cover_letter"), doc)).toBe(
      "Cover Letter (paste your text cover letter here)"
    );
  });

  it("resolves the screening select's question, not an option (Greenhouse)", () => {
    const doc = htmlFor("greenhouse-form.html");
    expect(resolveLabel(doc.getElementById("years_experience"), doc)).toBe(
      "How many years of software engineering experience do you have?"
    );
  });

  it("prefers the ancestor container over the placeholder (Ashby typeahead)", () => {
    const doc = htmlFor("ashby-form.html");
    expect(resolveLabel(doc.querySelector('input[name="location"]'), doc)).toBe(
      "Where are you currently based?"
    );
  });

  it("prefers aria-label when present (Ashby first name)", () => {
    const doc = htmlFor("ashby-form.html");
    expect(resolveLabel(doc.querySelector('input[name="firstname"]'), doc)).toBe("First name");
  });

  it("resolves a radio to its wrapping label's option text (Ashby work authorization)", () => {
    // Behavior frozen: the radio sits inside a wrapping <label>… Yes</label>,
    // and priority 4 (wrapping label) beats the container question span. The
    // pre-refactor content-script waterfall returned the same "Yes"; the
    // radio-group scan keys groups by name separately.
    const doc = htmlFor("ashby-form.html");
    const radio = doc.querySelector('input[type="radio"][name="workAuth"]');
    expect(resolveLabel(radio, doc)).toBe("Yes");
  });

  it("resolves wrapping labels without leaking control or option text (Lever)", () => {
    const doc = htmlFor("lever-form.html");
    expect(resolveLabel(doc.getElementById("name"), doc)).toBe("Name");
    expect(resolveLabel(doc.getElementById("source"), doc)).toBe("How did you hear about this job?");
  });

  it("finds the sibling label before the placeholder (Lever links)", () => {
    const doc = htmlFor("lever-form.html");
    expect(resolveLabel(doc.getElementById("layoutsContainer"), doc)).toBe("LinkedIn profile");
  });

  it("falls back to name, then Unnamed Field (waterfall priority 7)", () => {
    // A label-free document: the priority-5 prev-sibling heuristic would
    // otherwise consume a real page's wrapper DIVs (its shipped behavior,
    // exercised by the fixture tests above), masking the fallback.
    const doc = new DOMParser().parseFromString("<!DOCTYPE html><html><body></body></html>", "text/html");
    const naked = doc.createElement("input");
    naked.setAttribute("name", "newsletter_opt_in");
    doc.body.appendChild(naked);
    expect(resolveLabel(naked, doc)).toBe("newsletter_opt_in");
    const anon = doc.createElement("input");
    doc.body.appendChild(anon);
    expect(resolveLabel(anon, doc)).toBe("Unnamed Field");
  });

  it("caps cleaned labels at 160 characters (cleanLabelText contract)", () => {
    expect(cleanLabelText("x".repeat(200)).length).toBe(160);
    expect(cleanLabelText("*: Email :*")).toBe("Email");
  });
});

describe("matcher.legacy.js — the classic-script twin", () => {
  const matcherSrc = readFileSync(join(import.meta.dirname, "../extension/lib/matcher.js"), "utf8");
  const legacyPath = join(import.meta.dirname, "../extension/lib/matcher.legacy.js");
  const legacySrc = readFileSync(legacyPath, "utf8");

  it("is exactly what the build emits from lib/matcher.js (no drift)", () => {
    expect(legacySrc).toBe(toClassicMatcher(matcherSrc));
  });

  it("is a classic script: no import/export statements survive", () => {
    expect(/^\s*(import|export)\b/m.test(legacySrc)).toBe(false);
  });

  it("exposes every matcher export on the window hook", () => {
    const exported = [...matcherSrc.matchAll(/^export\s+(?:function|const)\s+(\w+)/gm)].map((m) => m[1]);
    for (const name of exported) {
      expect(LEGACY_EXPORTS, `matcher export "${name}" missing from the legacy attach`).toContain(name);
    }
  });

  it("installs a working matcher at window.__LOCALFORM_MATCHER__", () => {
    delete window.__LOCALFORM_MATCHER__;
    expect(window.__LOCALFORM_MATCHER__).toBeUndefined();
    new Function(legacySrc)();
    const m = window.__LOCALFORM_MATCHER__;
    expect(typeof m.resolveLabel).toBe("function");
    // The strict decisions must survive the transform verbatim.
    expect(m.aliasMatches("Company name", "name")).toBe(false);
    expect(m.aliasMatches("Legal Full Name", "full name")).toBe(true);
    expect(m.normalizeLabel("  First   NAME ")).toBe("first name");
  });
});

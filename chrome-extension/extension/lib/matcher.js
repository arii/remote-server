// ============================================================================
// LocalForm AI - Shared Field Matcher (OD-19 D2)
// The ONE set of label-matching rules, consumed by all three contexts:
//   - sidepanel.js and service-worker.js import this ES module directly.
//   - content-script.js is a classic script and cannot import ES modules,
//     so scripts/build.mjs emits lib/matcher.legacy.js from this same
//     source; it attaches the identical functions to
//     window.__LOCALFORM_MATCHER__ (the existing hook pattern).
//
// Strict alias matching (audit high #4 contract): exact match after
// normalization, then whole-word containment for aliases of 8+ characters.
// Short aliases match exactly only — the old bidirectional substring test
// let a two-letter-overlap alias like "name" match "Company name".
// ============================================================================

export function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, (ch) => "\\" + ch);
}

// Normalize a label or alias: lowercase, collapse whitespace runs to single
// spaces, trim. The exact pipeline the inline matchers applied inline.
export function normalizeLabel(s) {
  return String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
}

const aliasRegexCache = new Map(); // compiled word-boundary matchers per normalized alias

export function aliasMatches(query, alias) {
  const q = normalizeLabel(query);
  const a = normalizeLabel(alias);
  if (!q || !a) return false;
  if (q === a) return true;
  if (a.length < 8) return false; // short aliases match exactly only
  let re = aliasRegexCache.get(a);
  if (!re) {
    re = new RegExp(`\\b${escapeRegExp(a)}\\b`);
    aliasRegexCache.set(a, re);
  }
  return re.test(q);
}

// The content script's label waterfall applied this cleaner to every
// candidate string: collapse whitespace, trim, strip leading/trailing
// punctuation markers, cap at 160 chars. Moved here so the waterfall below
// is one self-contained function.
export function cleanLabelText(text) {
  if (!text) return "";
  return text
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[\*\:\s]+|[\*\:\s]+$/g, "")
    .slice(0, 160);
}

// Resolve the human-readable label for a form control via the strict
// waterfall, moved verbatim from content-script.js. `doc` defaults to the
// global document so the classic-script consumer can call resolveLabel(el);
// tests pass a parsed fixture document explicitly.
export function resolveLabel(el, doc = globalThis.document) {
  // Priority 1: aria-labelledby target elements
  const labelledBy = el.getAttribute("aria-labelledby");
  if (labelledBy) {
    const texts = labelledBy
      .split(/\s+/)
      .map((id) => doc.getElementById(id)?.textContent?.trim())
      .filter(Boolean);
    const cleaned = cleanLabelText(texts.join(" "));
    if (cleaned) return cleaned;
  }

  // Priority 2: aria-label attribute
  const ariaLabel = el.getAttribute("aria-label");
  if (ariaLabel) {
    const cleaned = cleanLabelText(ariaLabel);
    if (cleaned) return cleaned;
  }

  // Priority 3: Explicit <label for="id">
  if (el.id) {
    try {
      const explicitLabel = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (explicitLabel?.textContent?.trim()) {
        const cleaned = cleanLabelText(explicitLabel.textContent);
        if (cleaned) return cleaned;
      }
    } catch {}
  }

  // Priority 4: Wrapping <label>
  const parentLabel = el.closest("label");
  if (parentLabel) {
    const clone = parentLabel.cloneNode(true);
    clone.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
    const cleaned = cleanLabelText(clone.textContent);
    if (cleaned) return cleaned;
  }

  // Priority 5: Walk up to 5 ancestor levels for Ashby/Lever/Workday field containers
  // (Important: Runs BEFORE placeholder so "Start typing..." doesn't mask "Where are you currently based?")
  let curr = el;
  for (let depth = 0; depth < 5 && curr && curr !== doc.body; depth++) {
    const fieldLabel = curr.querySelector('label, [class*="label"], [class*="Label"], legend');
    if (fieldLabel && !fieldLabel.contains(el) && fieldLabel.textContent?.trim()) {
      const cleaned = cleanLabelText(fieldLabel.textContent);
      if (cleaned) return cleaned;
    }
    const prev = curr.previousElementSibling;
    if (prev && /^(LABEL|SPAN|DIV|P|H[1-6])$/i.test(prev.tagName) && prev.textContent?.trim()) {
      const cleaned = cleanLabelText(prev.textContent);
      if (cleaned) return cleaned;
    }
    curr = curr.parentElement;
  }

  // Priority 6: placeholder attribute
  const placeholder = el.getAttribute("placeholder");
  if (placeholder) {
    const cleaned = cleanLabelText(placeholder);
    if (cleaned) return cleaned;
  }

  // Priority 7: name or id fallback
  return cleanLabelText(el.name || el.id) || "Unnamed Field";
}

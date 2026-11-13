// ============================================================================
// LocalForm AI - Content Script (Manifest V3)
// Phases 2, 3, 4, 6, 7: DOM Form Scanning, Hydration, and Page Observation
// ============================================================================

(() => {
  if (window.__localFormAIInjected) return;
  window.__localFormAIInjected = true;

  // Block utility, captcha, payment, and ad iframes from hijacking messages
  const BLOCKED_IFRAME_HOSTS = [
    "recaptcha.net",
    "google.com/recaptcha",
    "gstatic.com/recaptcha",
    "hcaptcha.com",
    "challenges.cloudflare.com",
    "stripe.com",
    "doubleclick.net",
    "googletagmanager.com"
  ];

  const isBlockedIframe =
    window !== window.top &&
    BLOCKED_IFRAME_HOSTS.some((domain) => window.location.href.includes(domain));

  if (isBlockedIframe) {
    // Do not register message listener or scan inside captcha/payment/analytics iframes
    return;
  }

  const SENSITIVE_PATTERNS = /password|passcode|cvv|cvc|cc-number|card-number|ssn|social-security/i;
  const CAPTCHA_FIELD_PATTERNS = /g-recaptcha-response|h-captcha-response|cf-turnstile-response|recaptcha/i;

  function cleanLabelText(raw) {
    return (raw || '').replace(/\s+/g, ' ').replace(/\*+$/, '').trim();
  }

  function cleanText(text) {
    if (!text) return "";
    return text
      .replace(/\s+/g, " ")
      .trim()
      .replace(/^[\*\:\s]+|[\*\:\s]+$/g, "")
      .slice(0, 160);
  }

  // Strict label matching (audit high #4): exact match, or word-boundary
  // containment when the resolved label is long. Replaces the old bidirectional
  // substring test that let loose labels swallow unrelated fields.
  function escapeRegExp(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, (ch) => "\\" + ch);
  }

  const aliasRegexCache = new Map(); // compiled word-boundary matchers per normalized alias

  function aliasMatches(query, alias) {
    const q = String(query || "").toLowerCase().replace(/\s+/g, " ").trim();
    const a = String(alias || "").toLowerCase().replace(/\s+/g, " ").trim();
    if (!q || !a) return false;
    if (q === a) return true;
    if (a.length < 8) return false; // short labels match exactly only
    let re = aliasRegexCache.get(a);
    if (!re) {
      re = new RegExp(`\\b${escapeRegExp(a)}\\b`);
      aliasRegexCache.set(a, re);
    }
    return re.test(q);
  }

  // 1. Resolve human-readable label for any form input via strict waterfall
  function resolveFieldLabel(el) {
    // Priority 1: aria-labelledby target elements
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const texts = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.innerText?.trim())
        .filter(Boolean);
      const cleaned = cleanText(texts.join(" "));
      if (cleaned) return cleaned;
    }

    // Priority 2: aria-label attribute
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) {
      const cleaned = cleanText(ariaLabel);
      if (cleaned) return cleaned;
    }

    // Priority 3: Explicit <label for="id">
    if (el.id) {
      try {
        const explicitLabel = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (explicitLabel?.innerText?.trim()) {
          const cleaned = cleanText(explicitLabel.innerText);
          if (cleaned) return cleaned;
        }
      } catch {}
    }

    // Priority 4: Wrapping <label>
    const parentLabel = el.closest("label");
    if (parentLabel) {
      const clone = parentLabel.cloneNode(true);
      clone.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
      const cleaned = cleanText(clone.innerText);
      if (cleaned) return cleaned;
    }

    // Priority 5: Walk up to 5 ancestor levels for Ashby/Lever/Workday field containers
    // (Important: Runs BEFORE placeholder so "Start typing..." doesn't mask "Where are you currently based?")
    let curr = el;
    for (let depth = 0; depth < 5 && curr && curr !== document.body; depth++) {
      const fieldLabel = curr.querySelector('label, [class*="label"], [class*="Label"], legend');
      if (fieldLabel && !fieldLabel.contains(el) && fieldLabel.innerText?.trim()) {
        const cleaned = cleanText(fieldLabel.innerText);
        if (cleaned) return cleaned;
      }
      const prev = curr.previousElementSibling;
      if (prev && /^(LABEL|SPAN|DIV|P|H[1-6])$/i.test(prev.tagName) && prev.innerText?.trim()) {
        const cleaned = cleanText(prev.innerText);
        if (cleaned) return cleaned;
      }
      curr = curr.parentElement;
    }

    // Priority 6: placeholder attribute
    const placeholder = el.getAttribute("placeholder");
    if (placeholder) {
      const cleaned = cleanText(placeholder);
      if (cleaned) return cleaned;
    }

    // Priority 7: name or id fallback
    return cleanText(el.name || el.id) || "Unnamed Field";
  }

  // 2. Build unique, resilient CSS selector
  function buildCssSelector(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) {
      const tag = el.tagName.toLowerCase();
      try {
        const byName = document.querySelectorAll(`${tag}[name="${CSS.escape(el.name)}"]`);
        if (byName.length === 1) return `${tag}[name="${CSS.escape(el.name)}"]`;
      } catch {}
    }

    const path = [];
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {
      let selector = current.tagName.toLowerCase();
      if (current.id) {
        selector += `#${CSS.escape(current.id)}`;
        path.unshift(selector);
        break;
      } else {
        let sib = current;
        let nth = 1;
        while ((sib = sib.previousElementSibling)) {
          if (sib.tagName === current.tagName) nth++;
        }
        if (nth > 1) selector += `:nth-of-type(${nth})`;
      }
      path.unshift(selector);
      current = current.parentElement;
    }
    return path.join(" > ");
  }

  // 3. Find nearest section heading or fieldset legend (Phase 4 Form-Export++)
  function findNearestSectionHeading(el) {
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend?.innerText?.trim()) return legend.innerText.trim();
    }
    let node = el;
    while (node && node !== document.body) {
      let prev = node.previousElementSibling;
      while (prev) {
        if (/^H[1-4]$/.test(prev.tagName) && prev.innerText?.trim()) {
          return prev.innerText.trim();
        }
        prev = prev.previousElementSibling;
      }
      node = node.parentElement;
    }
    return undefined;
  }

  // 4. Scan forms on the current page (Phase 2 & Phase 4)
  function scanCurrentPageForms(options = { includePageContext: false, includeCurrentValues: true }) {
    const rawElements = Array.from(
      document.querySelectorAll('input, textarea, select, [contenteditable="true"]')
    );

    const radioGroups = new Map();
    const fields = [];

    rawElements.forEach((el, idx) => {
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || (tag === "textarea" ? "textarea" : tag)).toLowerCase();

      // Skip non-interactive or sensitive types
      if (["hidden", "submit", "button", "reset", "image", "file", "password"].includes(type)) return;
      if (el.disabled || el.getAttribute("aria-hidden") === "true") return;

      // Skip hidden captcha textareas (e.g., g-recaptcha-response-100000)
      if (
        CAPTCHA_FIELD_PATTERNS.test(el.id || "") ||
        CAPTCHA_FIELD_PATTERNS.test(el.name || "")
      ) {
        return;
      }

      const style = window.getComputedStyle(el);
      if (type !== "checkbox" && type !== "radio" && (style.display === "none" || style.visibility === "hidden")) {
        return;
      }

      const labelQuery = resolveFieldLabel(el);
      if (
        SENSITIVE_PATTERNS.test(labelQuery) ||
        SENSITIVE_PATTERNS.test(el.name || "") ||
        SENSITIVE_PATTERNS.test(el.autocomplete || "") ||
        CAPTCHA_FIELD_PATTERNS.test(labelQuery)
      ) {
        return;
      }

      // Coalesce radio buttons into a single radio-group field
      if (type === "radio" && el.name) {
        if (!radioGroups.has(el.name)) {
          const groupLegend = findNearestSectionHeading(el) || el.name;
          const groupField = {
            fieldId: `field_radio_${el.name}`,
            query: groupLegend,
            inputType: "radio-group",
            value: null,
            locator: {
              cssSelector: `input[type="radio"][name="${CSS.escape(el.name)}"]`,
              name: el.name
            },
            constraints: {
              required: el.required,
              options: []
            },
            sectionHeading: findNearestSectionHeading(el)
          };
          radioGroups.set(el.name, groupField);
          fields.push(groupField);
        }
        const group = radioGroups.get(el.name);
        const optLabel = resolveFieldLabel(el);
        group.constraints.options.push({
          label: optLabel,
          value: el.value,
          selected: el.checked
        });
        if (el.checked && options.includeCurrentValues) {
          group.value = el.value;
        }
        return;
      }

      const cssSelector = buildCssSelector(el);
      let currentValue = null;
      if (options.includeCurrentValues) {
        if (type === "checkbox") {
          // Check if an Ashby Yes/No button is already selected in the container
          const container = el.closest('[class*="fieldEntry"], [class*="form-field"], fieldset') || el.parentElement?.parentElement;
          const activeBtn = container?.querySelector('button[aria-pressed="true"], button[class*="selected"], button[class*="active"]');
          if (activeBtn) {
            currentValue = /^yes$/i.test(activeBtn.textContent.trim());
          } else {
            currentValue = el.checked ? true : null; // null = unanswered yet, so explicit false ("No") won't be skipped!
          }
        } else if (tag === "select" && el.multiple) {
          currentValue = Array.from(el.selectedOptions).map((o) => o.value);
        } else if (el.isContentEditable) {
          currentValue = el.innerText.trim();
        } else {
          currentValue = el.value || "";
        }
      }

      const selectOptions =
        tag === "select"
          ? Array.from(el.options).map((o) => ({
              label: o.text.trim(),
              value: o.value,
              selected: o.selected
            }))
          : undefined;

      const isConsent =
        type === "checkbox" &&
        /agree|terms|privacy policy|acknowledge|consent/i.test(labelQuery);

      fields.push({
        fieldId: `field_${idx}_${(el.id || el.name || "input").replace(/[^a-zA-Z0-9_-]/g, "")}`,
        query: labelQuery,
        inputType: tag === "select" ? (el.multiple ? "select-multiple" : "select-one") : type,
        semanticCategory: isConsent ? "legal.consent" : undefined,
        value: currentValue,
        locator: {
          cssSelector,
          id: el.id || undefined,
          name: el.name || undefined,
          ariaLabel: el.getAttribute("aria-label") || undefined
        },
        constraints: {
          required: Boolean(el.required),
          placeholder: el.getAttribute("placeholder") || undefined,
          autocomplete: el.getAttribute("autocomplete") || undefined,
          maxLength: el.maxLength > 0 ? el.maxLength : undefined,
          options: selectOptions
        },
        sectionHeading: options.includePageContext ? findNearestSectionHeading(el) : undefined
      });
    });

    const exportDoc = {
      schemaVersion: options.includePageContext ? "2.0" : "1.0",
      exportedAt: new Date().toISOString(),
      fields
    };

    if (options.includePageContext) {
      const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
        .map((h) => h.innerText?.trim())
        .filter(Boolean)
        .slice(0, 12);
      const metaDesc = document.querySelector('meta[name="description"]')?.getAttribute("content") || "";
      const rawBodyExcerpt = (document.body?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 3500);

      exportDoc.pageContext = {
        url: window.location.href,
        domain: window.location.hostname,
        title: document.title,
        description: metaDesc,
        headings,
        rawTextExcerpt: rawBodyExcerpt
      };
    }

    return exportDoc;
  }

  // 5. React / Vue / Ashby / Typeahead Compatible Native Value Setter (Phase 3 & 6)
  // Returns a verification result — { ok: true } or { ok: false, reason } — so the
  // hydrator only counts fills it can prove happened (no more silent wrong fills).
  async function setReactSafeValue(el, value) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();

    if (type === "checkbox") {
      const strVal = String(value).trim().toLowerCase();
      const shouldCheck = value === true || strVal === "true" || strVal === "yes" || strVal === "1";
      const targetButtonText = shouldCheck ? "yes" : "no";

      // Ashby HQ Pattern: Check if parent container uses <button>Yes</button> / <button>No</button> pills
      let container = el.parentElement;
      let clickedPill = false;
      for (let d = 0; d < 4 && container && container !== document.body; d++) {
        const pillButtons = Array.from(container.querySelectorAll('button, [role="button"], label'));
        const matchingPill = pillButtons.find(
          (btn) => (btn.innerText || btn.textContent || "").trim().toLowerCase() === targetButtonText
        );
        if (matchingPill) {
          matchingPill.click();
          clickedPill = true;
          break;
        }
        container = container.parentElement;
      }

      if (!clickedPill && el.checked !== shouldCheck) {
        el.click();
        el.checked = shouldCheck;
      }

      // Verify: a native checkbox must report the target state. A clicked Yes/No
      // pill is accepted as-is (selected styling is site-specific).
      if (!clickedPill && el.checked !== shouldCheck) {
        return { ok: false, reason: `Checkbox did not reach "${shouldCheck ? "checked" : "unchecked"}"` };
      }
    } else if (tag === "select") {
      const strVal = String(value).trim().toLowerCase();
      const matchedOption = Array.from(el.options).find(
        (opt) =>
          opt.value.toLowerCase() === strVal ||
          opt.text.trim().toLowerCase() === strVal ||
          opt.text.trim().toLowerCase().includes(strVal)
      );
      if (!matchedOption) {
        return { ok: false, reason: `No <option> matches "${String(value).slice(0, 60)}"` };
      }
      el.value = matchedOption.value;
      if (el.value !== matchedOption.value || !Array.from(el.selectedOptions || []).includes(matchedOption)) {
        return { ok: false, reason: `<select> did not accept option "${matchedOption.text.trim()}"` };
      }
    } else if (el.isContentEditable) {
      el.innerText = String(value);
      if (el.innerText.trim() !== String(value).trim()) {
        return { ok: false, reason: "Contenteditable value did not stick" };
      }
    } else {
      el.focus();
      const prototype = Object.getPrototypeOf(el);
      const prototypeSetter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      const ownSetter = Object.getOwnPropertyDescriptor(el, "value")?.set;

      if (prototypeSetter && ownSetter !== prototypeSetter) {
        prototypeSetter.call(el, String(value));
      } else if (ownSetter) {
        ownSetter.call(el, String(value));
      } else {
        el.value = String(value);
      }

      // If this is a combobox/typeahead (like Ashby's location picker), select
      // the dropdown suggestion whose text matches the intended value.
      const isTypeahead =
        el.getAttribute("role") === "combobox" ||
        el.getAttribute("placeholder")?.toLowerCase().includes("start typing") ||
        el.getAttribute("aria-autocomplete") === "list";

      if (isTypeahead) {
        el.focus();
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
        const ta = await clickMatchingTypeaheadOption(el, String(value));
        if (!ta.ok) {
          // Nothing matching appeared — commit the typed text with Enter instead
          el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
          await new Promise((r) => setTimeout(r, 80));
          const committed = (el.value || "").trim().toLowerCase();
          if (committed !== String(value).trim().toLowerCase()) {
            return { ok: false, reason: ta.reason || "Typeahead suggestion did not match the intended value" };
          }
        }
      } else if (el.value !== String(value)) {
        return {
          ok: false,
          reason: `Input value is "${String(el.value).slice(0, 60)}", expected "${String(value).slice(0, 60)}"`
        };
      }
    }

    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return { ok: true };
  }

  const TYPEAHEAD_WAIT_MS = 1500;
  const TYPEAHEAD_POLL_MS = 60;

  // Bounded wait for the typeahead dropdown, then click the option whose text
  // matches the intended value (replaces the old fixed 300ms blind first-option click).
  async function clickMatchingTypeaheadOption(el, intendedValue) {
    const wanted = String(intendedValue).trim().toLowerCase();
    const deadline = Date.now() + TYPEAHEAD_WAIT_MS;
    while (Date.now() < deadline) {
      const option = findMatchingTypeaheadOption(wanted);
      if (option) {
        option.click();
        return { ok: true, chosenText: (option.innerText || option.textContent || "").trim() };
      }
      await new Promise((r) => setTimeout(r, TYPEAHEAD_POLL_MS));
    }
    return {
      ok: false,
      reason: `No typeahead suggestion matching "${String(intendedValue).slice(0, 60)}" appeared within ${TYPEAHEAD_WAIT_MS}ms`
    };
  }

  function findMatchingTypeaheadOption(wanted) {
    const listbox = document.querySelector('[role="listbox"], [class*="menu"], [class*="options"]');
    const options = listbox
      ? listbox.querySelectorAll('[role="option"], [class*="option"], li, div[id*="option"]')
      : document.querySelectorAll('[role="option"], [class*="option"], [class*="Option"], [class*="menu"] li');
    for (const opt of options) {
      const text = (opt.innerText || opt.textContent || "").trim().toLowerCase();
      if (!text) continue;
      if (text === wanted || text.includes(wanted) || (wanted.includes(text) && text.length >= 4)) {
        return opt;
      }
    }
    return null;
  }

  function isVisibleInput(el) {
    if (!el || el.disabled || el.getAttribute("aria-hidden") === "true") return false;
    const style = window.getComputedStyle(el);
    if (el.type !== "checkbox" && el.type !== "radio" && (style.display === "none" || style.visibility === "hidden")) {
      return false;
    }
    return true;
  }

  function locateElement(field) {
    const loc = field.locator || {};

    // 1. Direct document.getElementById (immune to CSS selector escaping bugs on numeric UUIDs like "87368112-...")
    if (loc.id) {
      const byId = document.getElementById(loc.id);
      if (byId) return byId;
    }

    // 2. Direct name attribute lookup via CSS.escape
    if (loc.name) {
      try {
        const byName = document.querySelector(`[name="${CSS.escape(loc.name)}"]`);
        if (byName) return byName;
      } catch (_) {}
    }

    // 3. CSS Selector lookup
    if (loc.cssSelector) {
      try {
        const byCss = document.querySelector(loc.cssSelector);
        if (byCss) return byCss;
      } catch (_) {}
    }

    // 4. Fallback: Match by resolved human-readable label text (`field.query`)!
    // Crucial when React re-renders DOM wrappers after "Autofill from resume"
    if (field.query) {
      const targetQ = field.query.toLowerCase().replace(/\s+/g, " ").trim();
      const candidates = Array.from(
        document.querySelectorAll('input:not([type="hidden"]):not([type="file"]), textarea, select, [contenteditable="true"]')
      ).filter(isVisibleInput);

      for (const el of candidates) {
        const elLabel = resolveFieldLabel(el).toLowerCase().replace(/\s+/g, " ").trim();
        if (aliasMatches(targetQ, elLabel)) {
          return el;
        }
      }
    }

    return null;
  }

  // 6. Hydrate Page Forms from Export Document (Phase 3 & Phase 6)
  // Counts a field as filled ONLY when the setter verified the value landed.
  async function hydratePageForms(doc, options = { highlightConfidence: true }) {
    let filledCount = 0;
    const failedFields = [];

    for (const field of doc.fields || []) {
      if (field.value === null || field.value === undefined || field.value === "") continue;

      try {
        if (field.inputType === "radio-group" && field.locator?.name) {
          const radios = Array.from(
            document.querySelectorAll(`input[type="radio"][name="${CSS.escape(field.locator.name)}"]`)
          );
          const targetVal = String(field.value).toLowerCase();
          const matchedRadio = radios.find(
            (r) =>
              r.value.toLowerCase() === targetVal ||
              resolveFieldLabel(r).toLowerCase().includes(targetVal)
          );
          if (matchedRadio) {
            matchedRadio.click();
            if (!matchedRadio.checked) {
              failedFields.push({ fieldId: field.fieldId, query: field.query, reason: "Radio click did not select the option" });
              continue;
            }
            matchedRadio.checked = true;
            matchedRadio.dispatchEvent(new Event("change", { bubbles: true }));
            filledCount++;
          } else {
            failedFields.push({ fieldId: field.fieldId, query: field.query, reason: "Radio option not found" });
          }
          continue;
        }

        // Resilient element lookup: ID -> name -> CSS selector -> resolved label text
        const el = locateElement(field);

        if (!el) {
          failedFields.push({ fieldId: field.fieldId, query: field.query, reason: "Element not found in DOM" });
          continue;
        }

        const setResult = await setReactSafeValue(el, field.value);
        if (!setResult.ok) {
          failedFields.push({ fieldId: field.fieldId, query: field.query, reason: setResult.reason || "Fill could not be verified" });
          continue;
        }
        filledCount++;

        if (options.highlightConfidence) {
          const conf = typeof field.confidence === "number" ? field.confidence : 1.0;
          const borderColor = conf >= 0.8 ? "#10b981" : "#f59e0b";
          el.style.transition = "box-shadow 0.25s ease, outline 0.25s ease";
          el.style.outline = `2px solid ${borderColor}`;
          el.style.boxShadow = `0 0 0 4px ${conf >= 0.8 ? "rgba(16,185,129,0.18)" : "rgba(245,158,11,0.18)"}`;
        }
      } catch (err) {
        failedFields.push({ fieldId: field.fieldId, query: field.query, reason: err.message });
      }
    }

    return {
      totalFields: (doc.fields || []).length,
      filledCount,
      failedFields
    };
  }

  // 7. Capture Page Snapshot for Phase 7 Observer
  function capturePageSnapshot() {
    const formScan = scanCurrentPageForms({ includePageContext: true, includeCurrentValues: true });
    const filledFormFields = formScan.fields.filter(
      (f) => f.value !== null && f.value !== "" && f.value !== false
    );
    const cleanText = (document.body?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 5000);

    return {
      url: window.location.href,
      title: document.title,
      extractedText: cleanText,
      filledFormFields
    };
  }

  // Passive form-submit observation is OPT-IN per domain (audit high #6 /
  // spec FR-7.2). Hostnames live in chrome.storage.local under
  // observerDomainAllowlist, toggled from the side panel's Observer tab.
  // Fail closed: nothing is captured without explicit opt-in.
  async function isObserverEnabledForCurrentDomain() {
    try {
      const { observerDomainAllowlist = [] } = await chrome.storage.local.get(["observerDomainAllowlist"]);
      return Array.isArray(observerDomainAllowlist) && observerDomainAllowlist.includes(window.location.hostname);
    } catch (e) {
      console.warn("Observer allowlist check failed; treating as disabled:", e);
      return false;
    }
  }

  // Listen for passive form submissions to propose context updates
  document.addEventListener(
    "submit",
    async () => {
      try {
        if (!(await isObserverEnabledForCurrentDomain())) return;
        const snapshot = capturePageSnapshot();
        if (snapshot.filledFormFields.length > 0) {
          chrome.runtime.sendMessage({
            action: "FORM_SUBMIT_DETECTED",
            payload: snapshot
          });
        }
      } catch (e) {
        console.warn("Submit observation failed:", e);
      }
    },
    true
  );

  // 8. Phase 8: On-Demand UX & Touch Target Auditor
  function runUxAudit() {
    const violations = [];
    let totalChecked = 0;

    // 1. Interactive touch target sizes (< 24x24 px per WCAG 2.2 AAA / 2.5.8)
    const interactives = document.querySelectorAll('button, a, input, select, textarea, [role="button"]');
    interactives.forEach((el) => {
      totalChecked++;
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return;
      if (rect.width > 0 && rect.height > 0 && (rect.width < 24 || rect.height < 24)) {
        violations.push({
          type: "touch-target-size",
          severity: "warning",
          element: el.tagName.toLowerCase() + (el.id ? `#${el.id}` : ""),
          text: (el.innerText || el.getAttribute("aria-label") || el.name || "").slice(0, 40),
          actualSize: `${Math.round(rect.width)}x${Math.round(rect.height)}px`,
          guideline: "Interactive target should be at least 24x24px"
        });
      }
    });

    // 2. Silent text clipping (scrollWidth > clientWidth with overflow hidden)
    const textNodes = document.querySelectorAll('button, a, span, p, h1, h2, h3, h4, label, [class*="title"], [class*="label"]');
    textNodes.forEach((el) => {
      totalChecked++;
      const style = window.getComputedStyle(el);
      if (
        (style.overflow === "hidden" || style.overflowX === "hidden") &&
        style.textOverflow !== "ellipsis" &&
        el.scrollWidth > el.clientWidth + 2 &&
        el.clientWidth > 0
      ) {
        violations.push({
          type: "text-clipping",
          severity: "error",
          element: el.tagName.toLowerCase() + (el.id ? `#${el.id}` : ""),
          text: (el.innerText || "").slice(0, 50),
          guideline: "Text content clips silently without text-overflow: ellipsis"
        });
      }
    });

    // 3. Missing input labels / accessibility
    const inputs = document.querySelectorAll('input:not([type="hidden"]), select, textarea');
    inputs.forEach((el) => {
      totalChecked++;
      const hasLabel =
        el.getAttribute("aria-label") ||
        el.getAttribute("aria-labelledby") ||
        (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) ||
        el.closest("label");
      if (!hasLabel) {
        violations.push({
          type: "missing-label",
          severity: "error",
          element: el.tagName.toLowerCase() + (el.name ? `[name="${el.name}"]` : ""),
          guideline: "Form control missing accessible label or matching label[for]"
        });
      }
    });

    // 4. Missing mobile inputmode on tel/email/number/url
    inputs.forEach((el) => {
      totalChecked++;
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["tel", "email", "url", "number"].includes(type) && !el.getAttribute("inputmode")) {
        violations.push({
          type: "missing-inputmode",
          severity: "info",
          element: `${el.tagName.toLowerCase()}[type="${type}"]`,
          guideline: `Add inputmode="${type === "number" ? "numeric" : type}" for optimal mobile virtual keyboards`
        });
      }
    });

    const score = Math.max(0, Math.round(100 - violations.length * 4));
    return {
      timestamp: new Date().toISOString(),
      url: window.location.href,
      title: document.title,
      totalChecked,
      score,
      violations
    };
  }

  // Proactive Form Detection Badge ("Pre-Scan on Page Load")
  function proactiveFormPreScan() {
    try {
      const scan = scanCurrentPageForms({ includePageContext: true, includeCurrentValues: true });
      window.__PRE_SCANNED_FORM__ = {
        timestamp: Date.now(),
        doc: scan
      };
      if (scan.fields && scan.fields.length >= 3) {
        chrome.runtime.sendMessage({
          action: "FORM_DETECTED_ON_PAGE",
          count: scan.fields.length
        });
      }
    } catch (e) {}
  }

  let preScanTimer = null;
  const schedulePreScan = () => {
    clearTimeout(preScanTimer);
    preScanTimer = setTimeout(proactiveFormPreScan, 600);
  };

  if (document.readyState === "complete") {
    schedulePreScan();
  } else {
    window.addEventListener("load", schedulePreScan);
  }

  // Expose hooks for multi-frame fallback execution via chrome.scripting
  window.__LOCALFORM_SCAN__ = (opts) =>
    scanCurrentPageForms(opts || {});

  window.__LOCALFORM_HYDRATE__ = (doc, opts) =>
    hydratePageForms(doc, opts || {});

  window.__LOCALFORM_SNAPSHOT__ = () =>
    capturePageSnapshot();

  window.__LOCALFORM_AUDIT__ = () =>
    runUxAudit();

  // Message router from extension popup/sidepanel
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === "SCAN_PAGE_FORMS") {
      if (
        window.__PRE_SCANNED_FORM__ &&
        Date.now() - window.__PRE_SCANNED_FORM__.timestamp < 5000 &&
        !msg.options?.forceFresh
      ) {
        sendResponse(window.__PRE_SCANNED_FORM__.doc);
      } else {
        const fresh = scanCurrentPageForms(msg.options || {});
        window.__PRE_SCANNED_FORM__ = { timestamp: Date.now(), doc: fresh };
        sendResponse(fresh);
      }
    } else if (msg.action === "HYDRATE_PAGE_FORMS") {
      // hydratePageForms is async (fill verification) — resolve before responding
      hydratePageForms(msg.document, msg.options || {})
        .then(sendResponse)
        .catch((err) => sendResponse({ error: err.message, totalFields: 0, filledCount: 0, failedFields: [] }));
      return true;
    } else if (msg.action === "CAPTURE_PAGE_SNAPSHOT") {
      sendResponse(capturePageSnapshot());
    } else if (msg.action === "RUN_UX_AUDIT") {
      sendResponse(runUxAudit());
    }
    return true;
  });
})();

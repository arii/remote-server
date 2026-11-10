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
          currentValue = Boolean(el.checked);
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

      fields.push({
        fieldId: `field_${idx}_${(el.id || el.name || "input").replace(/[^a-zA-Z0-9_-]/g, "")}`,
        query: labelQuery,
        inputType: tag === "select" ? (el.multiple ? "select-multiple" : "select-one") : type,
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
  function setReactSafeValue(el, value) {
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
    } else if (tag === "select") {
      const strVal = String(value).toLowerCase();
      const matchedOption = Array.from(el.options).find(
        (opt) =>
          opt.value.toLowerCase() === strVal ||
          opt.text.trim().toLowerCase() === strVal ||
          opt.text.trim().toLowerCase().includes(strVal)
      );
      if (matchedOption) {
        el.value = matchedOption.value;
      }
    } else if (el.isContentEditable) {
      el.innerText = String(value);
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

      // If this is a combobox/typeahead (like Ashby's location picker), select the first dropdown suggestion
      const isTypeahead =
        el.getAttribute("role") === "combobox" ||
        el.getAttribute("placeholder")?.toLowerCase().includes("start typing") ||
        el.getAttribute("aria-autocomplete") === "list";

      if (isTypeahead) {
        setTimeout(() => {
          const listboxOptions = document.querySelectorAll(
            '[role="option"], [class*="option"], [class*="Option"], [class*="menu"] li'
          );
          if (listboxOptions.length > 0) {
            listboxOptions[0].click();
          } else {
            el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
          }
        }, 350);
      }
    }

    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  // 6. Hydrate Page Forms from Export Document (Phase 3 & Phase 6)
  function hydratePageForms(doc, options = { highlightConfidence: true }) {
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
            matchedRadio.checked = true;
            matchedRadio.dispatchEvent(new Event("change", { bubbles: true }));
            filledCount++;
          } else {
            failedFields.push({ fieldId: field.fieldId, query: field.query, reason: "Radio option not found" });
          }
          continue;
        }

        // Locator fallback waterfall: cssSelector -> id -> name
        let el = null;
        if (field.locator?.cssSelector) {
          try { el = document.querySelector(field.locator.cssSelector); } catch {}
        }
        if (!el && field.locator?.id) {
          el = document.getElementById(field.locator.id);
        }
        if (!el && field.locator?.name) {
          try {
            el = document.querySelector(`[name="${CSS.escape(field.locator.name)}"]`);
          } catch {}
        }

        if (!el) {
          failedFields.push({ fieldId: field.fieldId, query: field.query, reason: "Element not found in DOM" });
          continue;
        }

        setReactSafeValue(el, field.value);
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

  // Listen for passive form submissions to propose context updates
  document.addEventListener(
    "submit",
    () => {
      try {
        const snapshot = capturePageSnapshot();
        if (snapshot.filledFormFields.length > 0) {
          chrome.runtime.sendMessage({
            action: "FORM_SUBMIT_DETECTED",
            payload: snapshot
          });
        }
      } catch (e) {}
    },
    true
  );

  // Expose hooks for multi-frame fallback execution via chrome.scripting
  window.__LOCALFORM_SCAN__ = (opts) =>
    scanCurrentPageForms(opts || {});

  window.__LOCALFORM_HYDRATE__ = (doc, opts) =>
    hydratePageForms(doc, opts || {});

  window.__LOCALFORM_SNAPSHOT__ = () =>
    capturePageSnapshot();

  // Message router from extension popup/sidepanel
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === "SCAN_PAGE_FORMS") {
      sendResponse(scanCurrentPageForms(msg.options || {}));
    } else if (msg.action === "HYDRATE_PAGE_FORMS") {
      sendResponse(hydratePageForms(msg.document, msg.options || {}));
    } else if (msg.action === "CAPTURE_PAGE_SNAPSHOT") {
      sendResponse(capturePageSnapshot());
    }
    return true;
  });
})();

// ============================================================================
// LocalForm AI - Background Service Worker (Manifest V3)
// ============================================================================

import { aliasMatches } from "./lib/matcher.js";
import { normalizeForDedup, valueAlreadyHeld } from "./lib/delta-guards.js";

// NOTE: No seed data. The vault starts empty and is populated by the user via
// "Import Vault JSON" in the side panel (same format as "Export Vault JSON")
// or by adding facts manually. The extension ships with zero personal data.

// Open side panel on toolbar action click
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((err) => console.error("SidePanel setPanelBehavior error:", err));

chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(["userContextVault", "pendingDeltas"]);
  if (!existing.userContextVault) {
    // Vault starts empty — the user imports their profile via "Import Vault JSON"
    // in the side panel (same JSON format as "Export Vault JSON").
    await chrome.storage.local.set({
      userContextVault: [],
      pendingDeltas: []
    });
  }
  updateBadge((existing.pendingDeltas || []).length);
});

// Update toolbar badge to reflect pending deltas count
function updateBadge(count) {
  if (count > 0) {
    chrome.action.setBadgeText({ text: String(count) });
    chrome.action.setBadgeBackgroundColor({ color: "#10b981" });
  } else {
    chrome.action.setBadgeText({ text: "" });
  }
}

// Handle messages from content script or sidepanel
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "FORM_SUBMIT_DETECTED") {
    handleFormSubmitObservation(message.payload, sender.tab)
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({ error: err.message }));
    return true; // Keep message channel open for async response
  }

  if (message.action === "UPDATE_BADGE_COUNT") {
    updateBadge(message.count || 0);
    sendResponse({ ok: true });
    return false;
  }

  if (message.action === "FORM_DETECTED_ON_PAGE") {
    const tabId = sender.tab?.id;
    if (tabId && message.count >= 3) {
      chrome.action.setBadgeText({ text: String(message.count), tabId });
      chrome.action.setBadgeBackgroundColor({ color: "#3b82f6", tabId });
    }
    sendResponse({ ok: true });
    return false;
  }
});

// Strict alias matching lives in lib/matcher.js; the dedupe guards below
// live in lib/delta-guards.js — shared rule sets (audit high #4).

async function handleFormSubmitObservation(payload, tab) {
  const { userContextVault = [], pendingDeltas = [], activeSessionContext = null } = await chrome.storage.local.get([
    "userContextVault",
    "pendingDeltas",
    "activeSessionContext"
  ]);

  const newDeltas = [];
  const sensitiveRegex = /password|passcode|cvv|cvc|cc-number|card-number|ssn|social-security/i;

  for (const field of payload.filledFormFields || []) {
    if (!field.value || typeof field.value !== "string") continue;
    if (sensitiveRegex.test(field.query) || sensitiveRegex.test(field.locator?.name || "")) continue;

    const propText = String(field.value).trim();
    const normProp = normalizeForDedup(propText);
    if (!normProp || normProp.length < 2) continue;

    // GUARDRAIL 1: Strict URL / Value Deduplication (ignores http/https/www/hyphens)
    const alreadyInVault = userContextVault.some((v) => valueAlreadyHeld(v.value, propText));
    if (alreadyInVault) continue;

    const alreadyInPending = pendingDeltas.some((d) => valueAlreadyHeld(d.proposedValue, propText));
    if (alreadyInPending) continue;

    let key = field.query;
    let category = "custom";

    // GUARDRAIL 2: Never label a linkedin.com URL as "GitHub" or vice versa
    if (/linkedin\.com/i.test(propText) && /github/i.test(key)) {
      key = "LinkedIn Profile URL";
      category = "social";
    } else if (/github\.com/i.test(propText) && /linkedin/i.test(key)) {
      key = "GitHub Profile URL";
      category = "social";
    }

    const pageTitle = payload.title || "active page";
    const pageUrl = payload.url || (tab && tab.url) || "";

    // GUARDRAIL 3: Never allow a company-specific Cover Letter ("I am applying for ... at [Company]")
    // to overwrite canonical "Professional Bio & Summary" (custom.bio)!
    const isCompanySpecificCoverLetter =
      /i am applying for|writing to apply|role at |position at /i.test(propText) ||
      /cover letter/i.test(key);

    if (isCompanySpecificCoverLetter) {
      const companyOrg = activeSessionContext?.organization || pageTitle.split("|")[0].trim() || "Recent Application";
      newDeltas.push({
        deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        action: "create",
        category: "custom",
        key: `Cover Letter Sample (${companyOrg})`,
        proposedValue: propText,
        evidenceSnippet: `Submitted on form "${pageTitle}"`,
        sourceUrl: pageUrl,
        confidence: 0.9
      });
      continue;
    }

    const normalizedQuery = key.toLowerCase().trim();
    const existing = userContextVault.find((e) => {
      if (e.key.toLowerCase() === normalizedQuery) return true;
      if (e.aliases && e.aliases.some((a) => aliasMatches(normalizedQuery, a))) return true;
      if (field.constraints?.autocomplete && e.autocomplete === field.constraints.autocomplete) return true;
      return false;
    });

    if (!existing) {
      newDeltas.push({
        deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        action: "create",
        category,
        key,
        proposedValue: propText,
        evidenceSnippet: `Submitted on form "${pageTitle}"`,
        sourceUrl: pageUrl,
        confidence: 0.88
      });
    } else if (existing.value !== propText) {
      // If existing is canonical custom.bio, do NOT overwrite it with long-form prose
      if (existing.id === "custom.bio") {
        newDeltas.push({
          deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
          action: "create",
          category: "custom",
          key: `Cover Letter / Bio (${pageTitle.split("|")[0].trim()})`,
          proposedValue: propText,
          evidenceSnippet: `Submitted on form "${pageTitle}"`,
          sourceUrl: pageUrl,
          confidence: 0.88
        });
      } else {
        newDeltas.push({
          deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
          action: "update",
          targetEntryId: existing.id,
          category: existing.category,
          key: existing.key,
          previousValue: existing.value,
          proposedValue: propText,
          evidenceSnippet: `Observed updated value on form "${pageTitle}"`,
          sourceUrl: pageUrl,
          confidence: 0.94
        });
      }
    }
  }

  if (newDeltas.length > 0) {
    const merged = [...newDeltas, ...pendingDeltas].slice(0, 100);
    await chrome.storage.local.set({ pendingDeltas: merged });
    updateBadge(merged.length);
  }

  return { queuedDeltas: newDeltas.length };
}

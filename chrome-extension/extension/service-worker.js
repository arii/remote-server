// ============================================================================
// LocalForm AI - Background Service Worker (Manifest V3)
// ============================================================================

import { aliasMatches } from "./lib/matcher.js";
import { normalizeForDedup, valueAlreadyHeld } from "./lib/delta-guards.js";
import { deduplicateVault } from "./lib/vault.js";
import {
  applyDeltaApprove,
  applyDeltaApproveAll,
  applyDeltaDiscard,
  applyDeltaDiscardAll,
  applyDeltaQueue,
  applyVaultDelete,
  applyVaultReplace,
  applyVaultUpsert,
  createMutationQueue
} from "./lib/storage-mutations.js";

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
  sanitizeVaultStorage();
});

chrome.runtime.onStartup.addListener(() => {
  sanitizeVaultStorage();
});

// Deduplicate the vault at worker startup. The side panel used to do this on
// render (a panel write site); with single-writer storage the worker owns the
// sanitizing pass instead.
async function sanitizeVaultStorage() {
  try {
    const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
    if (userContextVault.length === 0) return;
    const cleaned = deduplicateVault(userContextVault);
    if (cleaned.length !== userContextVault.length) {
      await chrome.storage.local.set({ userContextVault: cleaned });
    }
  } catch (err) {
    console.warn("Vault startup sanitization warning:", err);
  }
}

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
    enqueueObserverMerge(message.payload, sender.tab)
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

  // Single-writer boundary (OD-19 D3): the panel sends typed mutations for
  // vault/delta state instead of writing chrome.storage.local itself.
  if (message.type && MUTATION_HANDLERS[message.type]) {
    handleMutation(message)
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({ error: err.message }));
    return true; // Keep message channel open for async response
  }
});

// --- Single-writer mutation pipeline -----------------------------------------
// Every vault/delta write — the panel's typed mutations AND the observer's
// delta merge — flows through this one serialized queue, so two mutations can
// never interleave their read-modify-write windows and lose a delta.

const enqueueMutation = createMutationQueue();

const MUTATION_HANDLERS = {
  "vault:upsert": (msg) => (state) => applyVaultUpsert(state, msg.entry),
  "vault:delete": (msg) => (state) => applyVaultDelete(state, msg.id),
  "vault:replace": (msg) => (state) => applyVaultReplace(state, msg.entries),
  "delta:approve": (msg) => (state) => applyDeltaApprove(state, msg.deltaId),
  "delta:discard": (msg) => (state) => applyDeltaDiscard(state, msg.deltaId),
  "delta:approveAll": () => (state) => applyDeltaApproveAll(state),
  "delta:discardAll": () => (state) => applyDeltaDiscardAll(state),
  "delta:queue": (msg) => (state) => applyDeltaQueue(state, msg.deltas)
};

async function readVaultState() {
  const { userContextVault = [], pendingDeltas = [] } = await chrome.storage.local.get([
    "userContextVault",
    "pendingDeltas"
  ]);
  return { userContextVault, pendingDeltas };
}

async function writeVaultState(state) {
  // Sanitize on write: keeps storage deduplicated the way the panel's old
  // render-time cleanup pass did, without the panel holding a write path.
  await chrome.storage.local.set({
    userContextVault: deduplicateVault(state.userContextVault),
    pendingDeltas: state.pendingDeltas
  });
  updateBadge(state.pendingDeltas.length);
  return state;
}

async function handleMutation(message) {
  const build = MUTATION_HANDLERS[message.type];
  if (!build) throw new Error(`Unknown vault/delta mutation: ${message.type}`);

  return enqueueMutation(async () => {
    const state = await readVaultState();
    const next = await writeVaultState(build(message)(state));
    return {
      ok: true,
      vaultCount: next.userContextVault.length,
      pendingDeltas: next.pendingDeltas.length
    };
  });
}

// The observer's delta merge reads and writes inside the same queue, so a
// panel mutation in flight cannot clobber it (and vice versa).
function enqueueObserverMerge(payload, tab) {
  return enqueueMutation(() => handleFormSubmitObservation(payload, tab));
}

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

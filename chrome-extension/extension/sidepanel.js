// ============================================================================
// LocalForm AI - Side Panel Controller (Manifest V3)
// Implements Phases 1 through 7 for Chrome's Built-in On-Device AI
// ============================================================================

import { matchField } from "./lib/matcher.js";
import { createHybridPromptSession, getHybridStatus } from "./lib/hybrid-session.js";
import { deduplicateVault, sanitizeVaultEntries, describeVaultImport } from "./lib/vault.js";
import { MAX_CHUNKS_PER_PAGE, MAX_CHUNKS_PER_RUN, PROFILE_FACTS_SCHEMA, collectSourcePages, extractFactsFromText, htmlToText } from "./lib/profile-ingest.js";
import {
  askTabEntries,
  buildAskPrompt,
  formatTargetContext,
  formatVaultFacts,
  selectVaultFacts
} from "./lib/ask-context.js";
import { MAX_DRIVE_FILES_PER_RUN } from "./lib/drive-import.js";
import { improvementSummary } from "./lib/improvement-summary.js";
import { errorInfo as syncErrorInfo, formatSyncFailure } from "./lib/drive-sync.js";
import { INTERNALS_URL, MODEL_APIS, collapseRepeatedErrors, describeModelError, describeTaskFailure, formatModelError, makeDownloadMonitor, modelProgressState, redactSecrets } from "./lib/model-download.js";
import { buildTraceEntry } from "./lib/trace.js";
import { modelPanelView, observeView, webHostname } from "./lib/panel-state.js";
import { getPrompt, guidanceChanged, PROMPT_IDS } from "./lib/prompts.js";
import {
  SUGGESTION_SCHEMA,
  appendGuidanceLine,
  applyGuidanceEdit,
  normalizeGuidance,
  parseGuidanceSuggestions,
  proposeGuidanceRequest,
  recordDeltaFeedback,
  revertGuidance
} from "./lib/prompt-guidance.js";
import { MAX_EVAL_CASES, casesKey, normalizeCases, removeCase, scoreOutput, suggestFromFailures, summarizeRun } from "./lib/prompt-eval.js";
import { summarizeHydrationResult } from "./lib/hydration.js";
import { MAX_PENDING_DELTAS } from "./lib/storage-mutations.js";
import { buildVaultPrompt, existingKeysForPrompt, parseVaultImport } from "./lib/vault-onboarding.js";
import {
  REFINEMENT_SCHEMA,
  buildRefinementPrompt,
  mergeRefinement,
  parseRefinement
} from "./lib/learning.js";

// Global state cache & pre-warmed AI pool
let currentExportDoc = null;
let baseWarmSession = null;
let promptGuidance = normalizeGuidance(null);

// Guidance is baked into the warm session's system prompt, so a change must
// drop the warm session; the next getClonedSession() rebuilds it.
function setPromptGuidance(next) {
  const normalized = normalizeGuidance(next);
  const changed = guidanceChanged(promptGuidance, normalized);
  promptGuidance = normalized;
  if (changed && baseWarmSession) {
    baseWarmSession.destroy?.();
    baseWarmSession = null;
  }
}

chrome.storage.local.get(["promptGuidance"]).then((r) => {
  setPromptGuidance(r.promptGuidance);
  renderGuidanceCard();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.promptGuidance) {
    setPromptGuidance(changes.promptGuidance.newValue);
    renderGuidanceCard();
  }
  if (area === "local" && IMPROVE_KEYS.some((k) => changes[k])) renderImprovement();
});

async function getClonedSession() {
  const lmGlobal = self.LanguageModel || self.ai?.languageModel;
  if (!lmGlobal) return null;
  try {
    if (!baseWarmSession) {
      baseWarmSession = await lmGlobal.create({
        outputLanguage: "en",
        systemPrompt: getPrompt("system", { guidance: promptGuidance })
      });
    }
    if (typeof baseWarmSession.clone === "function") {
      try {
        return await baseWarmSession.clone({ outputLanguage: "en" });
      } catch {
        return await baseWarmSession.clone();
      }
    }
    return await lmGlobal.create({ outputLanguage: "en" });
  } catch (e) {
    baseWarmSession = null;
    return await lmGlobal.create({ outputLanguage: "en" });
  }
}

// Pre-warm Gemini Nano on startup
getClonedSession().catch(() => {});

// Helpers
async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// Send message to active tab targeting top-level frame (frameId: 0) first,
// with graceful fallback for embedded job-board iframes and pre-existing tabs
async function sendToActiveTab(payload) {
  const tab = await getActiveTab();
  if (!tab?.id) throw new Error("No active tab found");

  try {
    // Always target the top-level document (frameId: 0) first to avoid captcha iframe race conditions!
    const mainFrameResponse = await chrome.tabs.sendMessage(tab.id, payload, { frameId: 0 });

    // If scanning forms and the main frame has 0 fields (e.g., an embedded Greenhouse/Lever iframe),
    // fallback to checking child frames via scripting.executeScript across all frames
    if (
      payload.action === "SCAN_PAGE_FORMS" &&
      (!mainFrameResponse?.fields || mainFrameResponse.fields.length === 0)
    ) {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        func: (opts) => (window.__LOCALFORM_SCAN__ ? window.__LOCALFORM_SCAN__(opts) : null),
        args: [payload.options || {}]
      });
      const validFrames = results
        .map((r) => r.result)
        .filter((res) => res && Array.isArray(res.fields) && res.fields.length > 0);
      if (validFrames.length > 0) {
        return {
          ...mainFrameResponse,
          fields: validFrames.flatMap((f) => f.fields)
        };
      }
    }

    // If hydrating and main frame didn't fill anything, also attempt across all frames
    if (
      payload.action === "HYDRATE_PAGE_FORMS" &&
      mainFrameResponse?.filledCount === 0
    ) {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        func: (doc, opts) => (window.__LOCALFORM_HYDRATE__ ? window.__LOCALFORM_HYDRATE__(doc, opts) : null),
        args: [payload.document, payload.options || {}]
      });
      const validResults = results
        .map((r) => r.result)
        .filter((res) => res && res.filledCount > 0);
      if (validResults.length > 0) {
        return validResults[0];
      }
    }

    // If capturing page snapshot and main frame has 0 filled fields, check child frames
    if (
      payload.action === "CAPTURE_PAGE_SNAPSHOT" &&
      (!mainFrameResponse?.filledFormFields || mainFrameResponse.filledFormFields.length === 0)
    ) {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        func: () => (window.__LOCALFORM_SNAPSHOT__ ? window.__LOCALFORM_SNAPSHOT__() : null)
      });
      const validFrames = results
        .map((r) => r.result)
        .filter((res) => res && Array.isArray(res.filledFormFields) && res.filledFormFields.length > 0);
      if (validFrames.length > 0) {
        return {
          ...mainFrameResponse,
          filledFormFields: validFrames.flatMap((f) => f.filledFormFields),
          extractedText: (mainFrameResponse?.extractedText || "") + "\n" + validFrames.map((f) => f.extractedText || "").join("\n")
        };
      }
    }

    return mainFrameResponse;
  } catch (err) {
    // Auto-inject content-script.js into the main frame if the tab was open before extension reload
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: false },
      files: ["content-script.js"]
    });
    return await chrome.tabs.sendMessage(tab.id, payload, { frameId: 0 });
  }
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Strict alias matching for vault entries lives in lib/matcher.js — the one
// shared rule set (audit high #4).

// ============================================================================
// OBSERVABILITY & UX PROGRESS CONTROLLER
// ============================================================================
const Telemetry = {
  traces: [],
  timerInterval: null,
  startTime: 0,
  activeAbortController: null,

  startTask(stageLabel, progressPct = 15) {
    this.startTime = performance.now();
    this.activeAbortController = new AbortController();

    const bar = document.getElementById("ux-activity-bar");
    const lbl = document.getElementById("ux-stage-label");
    const fill = document.getElementById("ux-progress-fill");
    const timerEl = document.getElementById("ux-elapsed-timer");
    const previewEl = document.getElementById("ux-stream-preview");

    if (bar) bar.style.display = "block";
    if (lbl) lbl.textContent = stageLabel;
    if (fill) fill.style.width = `${progressPct}%`;
    if (previewEl) previewEl.textContent = "";

    clearInterval(this.timerInterval);
    this.timerInterval = setInterval(() => {
      const elapsedSec = ((performance.now() - this.startTime) / 1000).toFixed(1);
      if (timerEl) timerEl.textContent = `${elapsedSec}s`;
    }, 100);

    return this.activeAbortController.signal;
  },

  updateStage(stageLabel, progressPct, streamSnippet = "") {
    const lbl = document.getElementById("ux-stage-label");
    const fill = document.getElementById("ux-progress-fill");
    const previewEl = document.getElementById("ux-stream-preview");
    if (lbl) lbl.textContent = stageLabel;
    if (fill && progressPct !== undefined) fill.style.width = `${progressPct}%`;
    if (previewEl && streamSnippet) {
      previewEl.textContent = `⚡ ${streamSnippet.slice(-90)}`;
    }
  },

  endTask() {
    clearInterval(this.timerInterval);
    const bar = document.getElementById("ux-activity-bar");
    const fill = document.getElementById("ux-progress-fill");
    if (fill) fill.style.width = "100%";
    setTimeout(() => {
      if (bar) bar.style.display = "none";
    }, 450);
  },

  updateTokenUsage(session) {
    if (!session) return;
    const used = session.contextUsage ?? session.tokensSoFar ?? 0;
    const max = session.contextWindow ?? session.maxTokens ?? 9216;
    const pill = document.getElementById("obs-token-pill");
    if (pill) {
      const pct = Math.round((used / max) * 100);
      pill.textContent = `Tokens: ${used} / ${max} (${pct}%)`;
      pill.style.color = pct > 80 ? "#f87171" : "#38bdf8";
    }
  },

  logTrace({ type, title, durationMs, details }) {
    const entry = buildTraceEntry({ type, title, durationMs, details });
    this.traces.unshift(entry);
    if (this.traces.length > 40) this.traces.pop();
    this.renderTraces();
  },

  renderTraces() {
    const countEl = document.getElementById("obs-log-count");
    const listEl = document.getElementById("obs-trace-list");
    if (countEl) countEl.textContent = String(this.traces.length);
    if (!listEl) return;

    const colorMap = {
      SCAN: "#38bdf8",
      HYDRATE: "#10b981",
      LLM_PROMPT: "#a855f7",
      OBSERVER: "#f59e0b",
      ERROR: "#ef4444"
    };

    listEl.innerHTML = this.traces
      .map(
        (t) => `
      <details style="margin-bottom:6px; border-left:2px solid ${colorMap[t.type] || "#64748b"}; padding-left:6px;">
        <summary style="cursor:pointer; color:#e2e8f0; display:flex; justify-content:space-between;">
          <span><strong style="color:${colorMap[t.type] || "#94a3b8"}">[${t.type}]</strong> ${escapeHtml(t.title)}</span>
          <span style="color:#64748b;">${t.durationMs}ms · ${t.ts}</span>
        </summary>
        <pre style="margin:4px 0 0; padding:6px; background:#020617; color:#94a3b8; border-radius:4px; overflow-x:auto; white-space:pre-wrap;">${escapeHtml(t.details)}</pre>
      </details>
    `
      )
      .join("");
  }
};

// Wire Drawer Toggle & Abort Button
document.getElementById("obs-drawer-toggle")?.addEventListener("click", () => {
  const body = document.getElementById("obs-drawer-body");
  if (body) body.style.display = body.style.display === "none" ? "block" : "none";
});
document.getElementById("btn-clear-traces")?.addEventListener("click", (e) => {
  e.stopPropagation();
  Telemetry.traces = [];
  Telemetry.renderTraces();
});
document.getElementById("btn-abort-ai")?.addEventListener("click", () => {
  Telemetry.activeAbortController?.abort();
  Telemetry.logTrace({ type: "ERROR", title: "User Aborted AI Task", durationMs: 0, details: "AbortController triggered" });
  Telemetry.endTask();
});

async function runObservablePrompt(session, prompt, options = {}) {
  const t0 = performance.now();
  const { label = "Gemini Nano Inference", responseConstraint, signal, onChunk } = options;

  try {
    let finalOutput = "";
    const promptOpts = {};
    if (responseConstraint) promptOpts.responseConstraint = responseConstraint;
    if (signal) promptOpts.signal = signal;

    if (typeof session.promptStreaming === "function" && !responseConstraint) {
      const stream = session.promptStreaming(prompt, promptOpts);
      for await (const chunk of stream) {
        if (chunk.startsWith(finalOutput)) {
          finalOutput = chunk;
        } else {
          finalOutput += chunk;
        }
        Telemetry.updateStage(
          `${label} (${finalOutput.length} chars)...`,
          undefined,
          finalOutput
        );
        if (onChunk) onChunk(finalOutput);
      }
    } else {
      finalOutput = await session.prompt(prompt, promptOpts);
    }

    const durationMs = performance.now() - t0;
    Telemetry.updateTokenUsage(session);
    Telemetry.logTrace({
      type: "LLM_PROMPT",
      title: `${label} (${finalOutput.length} chars out)`,
      durationMs,
      details: `PROMPT (${prompt.length} chars):\n${prompt.slice(0, 600)}...\n\nOUTPUT:\n${finalOutput}`
    });

    return finalOutput;
  } catch (err) {
    Telemetry.logTrace({
      type: "ERROR",
      title: `${label} Failed: ${redactSecrets(String(err.message))}`,
      durationMs: performance.now() - t0,
      details: redactSecrets(String(err.stack || err.message))
    });
    throw err;
  }
}

// ----------------------------------------------------------------------------
// TAB NAVIGATION
// ----------------------------------------------------------------------------
document.querySelectorAll(".nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-pane").forEach((p) => (p.style.display = "none"));

    btn.classList.add("active");
    const targetId = btn.dataset.tab;
    const targetPane = document.getElementById(targetId);
    if (targetPane) targetPane.style.display = "block";

    if (targetId === "tab-observer" || targetId === "tab-fill") {
      renderSessionContext();
    }
    if (targetId === "tab-observer") {
      renderObserverDomainToggle();
      renderImprovement();
    }
  });
});

// ----------------------------------------------------------------------------
// HOW LOCALFORM IMPROVES: read-only summary at the top of the Observer tab.
// ----------------------------------------------------------------------------
const IMPROVE_KEYS = ["userContextVault", "pendingDeltas", "learnedMappings", "learnedAliases", "fillOutcomes", "promptGuidance", "promptEvalCases", "promptEvalRuns"];

function goToImprovementLink(link) {
  document.querySelector(`.nav button[data-tab="${link.tab}"]`)?.click();
  if (link.open) document.getElementById("prompt-tuning").open = true;
  document.getElementById(link.el)?.scrollIntoView({ block: "center" });
}

function improvementLinkButton(link) {
  const btn = document.createElement("button");
  btn.className = "btn btn-subtle btn-sm";
  btn.textContent = "Go";
  btn.addEventListener("click", () => goToImprovementLink(link));
  return btn;
}

async function renderImprovement() {
  const s = await chrome.storage.local.get(IMPROVE_KEYS);
  const view = improvementSummary({
    vault: s.userContextVault,
    pendingDeltas: s.pendingDeltas,
    learnedMappings: s.learnedMappings,
    learnedAliases: s.learnedAliases,
    fillOutcomes: s.fillOutcomes,
    promptGuidance: s.promptGuidance,
    promptEvalCases: s.promptEvalCases,
    promptEvalRuns: s.promptEvalRuns
  });
  const loop = document.getElementById("improve-loop");
  loop.textContent = "";
  for (const l of view.loop) {
    const p = document.createElement("div");
    p.style.marginBottom = "4px";
    p.textContent = `${l.kind === "auto" ? "Automatic: " : "Needs you: "}${l.text}`;
    loop.appendChild(p);
  }
  const items = document.getElementById("improve-items");
  items.textContent = "";
  for (const it of view.items) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex; justify-content:space-between; align-items:center; gap:8px; font-size:11px; margin:4px 0;";
    const text = document.createElement("span");
    text.textContent = it.text;
    row.append(text, improvementLinkButton(it.link));
    items.appendChild(row);
  }
  const next = document.getElementById("improve-next");
  next.textContent = "";
  const label = document.createElement("strong");
  label.textContent = "Next: ";
  next.append(label, document.createTextNode(`${view.next.text} `), improvementLinkButton(view.next.link));
}

// ----------------------------------------------------------------------------
// PHASE 1: BUILT-IN AI DETECTION & BOOTSTRAP
// ----------------------------------------------------------------------------
const failedModelDetails = {};

async function checkBuiltInAIStatus() {
  const report = {
    languageModel: "unavailable",
    summarizer: "unavailable",
    writer: "unavailable",
    userAgent: navigator.userAgent
  };

  try {
    if ("LanguageModel" in self) {
      report.languageModel = await self.LanguageModel.availability();
    } else if (self.ai?.languageModel) {
      const cap = await self.ai.languageModel.capabilities();
      report.languageModel = cap?.available || "unavailable";
    }
  } catch (e) {
    report.languageModelError = e.message;
  }

  try {
    if ("Summarizer" in self) {
      report.summarizer = await self.Summarizer.availability();
    }
  } catch (e) {
    report.summarizerError = e.message;
  }

  try {
    if ("Writer" in self) {
      report.writer = await self.Writer.availability();
    }
  } catch (e) {
    report.writerError = e.message;
  }

  // Update Status Pills
  document.getElementById("pill-prompt-status").textContent = report.languageModel;
  document.getElementById("pill-summarizer-status").textContent = report.summarizer;
  document.getElementById("pill-writer-status").textContent = report.writer;

  // Update Header Badge
  const badge = document.getElementById("ai-status-badge");
  const badgeText = document.getElementById("ai-status-text");
  badge.className = "badge";
  if (report.languageModel === "available") {
    badge.classList.add("badge-available");
    badgeText.textContent = "Nano Ready";
  } else if (report.languageModel === "downloadable" || report.languageModel === "downloading") {
    badge.classList.add("badge-downloadable");
    badgeText.textContent = `Nano ${report.languageModel}`;
  } else {
    badge.classList.add("badge-unavailable");
    badgeText.textContent = "AI Unavailable";
  }

  const items = [];
  for (const api of MODEL_APIS) {
    renderModelProgress(api.key, modelProgressState(report[api.key]));
    const text = report[api.key] === "unavailable"
      ? formatModelError(describeModelError(null, { availability: "unavailable" }))
      : "";
    items.push({ key: api.key, label: MODEL_API_LABEL[api.key] || api.key, text });
  }
  const collapsed = collapseRepeatedErrors(items);
  for (const item of collapsed) {
    const el = document.getElementById(`model-error-${item.key}`);
    if (item.text && el) {
      el.hidden = false;
      el.textContent = item.text;
    } else if (!failedModelDetails[item.key]) {
      renderModelError(item.key, null);
    }
  }

  document.getElementById("model-details-box").textContent = JSON.stringify(report, null, 2);
  applyModelView(report);
  return report;
}

// Show only what the current model state needs: progress and download button
// until everything is ready, prompt tools only once the Prompt model is ready.
function applyModelView(report) {
  const view = modelPanelView(report);
  const show = (id, on) => {
    const el = document.getElementById(id);
    if (el) el.style.display = on ? "" : "none";
  };
  show("download-section", view.showProgress);
  show("btn-bootstrap-model", view.showDownloadButton);
  show("model-ready-line", view.allReady);
  show("prompt-tools", view.promptReady);
  show("prompt-cta", !view.promptReady);
  document.getElementById("model-ready-line").textContent = view.summary;
  document.getElementById("prompt-cta-text").textContent = view.ctaText;
}

const MODEL_API_LABEL = {
  languageModel: "Prompt API",
  summarizer: "Summarizer",
  writer: "Writer",
};

function renderModelError(key, detail) {
  const el = document.getElementById(`model-error-${key}`);
  if (!el) return;
  el.hidden = !detail;
  el.textContent = detail ? formatModelError(detail) : "";
}

function renderModelProgress(key, { state, pct, label }, errorText = "") {
  const fill = document.getElementById(`download-fill-${key}`);
  const text = document.getElementById(`download-text-${key}`);
  if (!fill || !text) return;
  const indeterminate = state === "downloading" && pct === null;
  fill.classList.toggle("indeterminate", indeterminate);
  if (!indeterminate) fill.style.width = `${pct ?? 0}%`;
  text.textContent = errorText || label;
}

// Reflect configured Firebase AI Logic hybrid mode in the panel so it is
// never implied that all processing is on-device once cloud fallback exists.
function showHybridModeLine(hybrid) {
  const el = document.getElementById("hybrid-mode-line");
  if (!el) return;
  let text = "Hybrid mode active: Firebase AI Logic configured. Prompts prefer the on-device model and may fall back to cloud inference via Firebase.";
  if (!getHybridStatus().appCheckActive) {
    text += " App Check is not active in this build, so a cloud fallback may be rejected if your Firebase project enforces it.";
  }
  el.textContent = text;
  el.style.display = "";
  hybrid.onError = (msg) => {
    el.textContent = `${text} ${msg}`;
    el.style.display = "";
  };
}

async function createPromptSession(onProgress, options = {}) {
  const monitor = makeDownloadMonitor(onProgress);

  const sessionOptions = {
    monitor,
    ...options,
    outputLanguage: options.outputLanguage || "en"
  };

  // Hybrid inference (#BuildHybridAI): when a local Firebase config exists,
  // every prompt goes through Firebase AI Logic in PREFER_ON_DEVICE mode
  // (on-device Gemini Nano via the Prompt API, cloud fallback otherwise).
  // Without a config the built-in LanguageModel path below is unchanged.
  const hybrid = await createHybridPromptSession(sessionOptions).catch((err) => {
    console.warn("Firebase AI Logic hybrid session unavailable, using built-in APIs:", err);
    return null;
  });
  if (hybrid) {
    showHybridModeLine(hybrid);
    return hybrid;
  }

  if (!onProgress && !options.systemPrompt) {
    const warm = await getClonedSession();
    if (warm) return warm;
  }

  if ("LanguageModel" in self) {
    return await self.LanguageModel.create(sessionOptions);
  } else if (self.ai?.languageModel) {
    return await self.ai.languageModel.create(sessionOptions);
  }
  throw new Error("Prompt API (LanguageModel) not available in this browser context.");
}

// Model Bootstrap Button: downloads only models that are downloadable/downloading.
document.getElementById("btn-bootstrap-model").addEventListener("click", async () => {
  const box = document.getElementById("model-details-box");
  box.textContent = "Checking which models need downloading...";
  const before = await checkBuiltInAIStatus();
  const errors = [];
  for (const api of MODEL_APIS) {
    delete failedModelDetails[api.key];
    renderModelError(api.key, null);
  }

  for (const api of MODEL_APIS) {
    if (before[api.key] !== "downloadable" && before[api.key] !== "downloading") continue;
    const onPct = (pct) => {
      renderModelProgress(api.key, modelProgressState("downloading", pct));
      box.textContent = `Downloading ${api.label} model: ${pct}%...`;
    };
    renderModelProgress(api.key, modelProgressState("downloading", null));
    try {
      let session;
      if (api.key === "languageModel") {
        session = await createPromptSession(onPct);
      } else {
        session = await self[api.global].create({ monitor: makeDownloadMonitor(onPct), outputLanguage: "en" });
      }
      session.destroy?.();
    } catch (err) {
      errors.push(`${api.label}: ${err.message}`);
      let availability;
      try {
        availability = await self[api.global]?.availability?.();
      } catch {
        availability = undefined;
      }
      const detail = describeModelError(err, { availability });
      detail.availability = availability;
      failedModelDetails[api.key] = detail;
      renderModelProgress(api.key, modelProgressState(availability || before[api.key]), "Failed");
      renderModelError(api.key, detail);
      Telemetry.logTrace({
        type: "ERROR",
        title: `${api.label} model download failed`,
        durationMs: 0,
        details: `${formatModelError(detail)}\ncause=${detail.cause} availability=${availability ?? "unknown"}`
      });
    }
  }

  const after = await checkBuiltInAIStatus();
  for (const api of MODEL_APIS) {
    if (failedModelDetails[api.key]) renderModelProgress(api.key, modelProgressState(after[api.key]), "Failed");
  }
  if (errors.length) {
    box.textContent = `Model initialization errors:\n${errors.join("\n")}\n\nCheck model status and logs at chrome://on-device-internals. Stable extension Prompt API needs no experimental flags; model availability, hardware and download requirements still apply. Writer availability is separate.\n\n${box.textContent}`;
  }
});

document.getElementById("btn-open-on-device-internals").addEventListener("click", () => {
  chrome.tabs.create({ url: INTERNALS_URL });
});

// ----------------------------------------------------------------------------
// ASK CARD: general questions answered from sources the user ticks.
// Never writes to the vault; "Save answer as fact" goes through the review queue.
// ----------------------------------------------------------------------------
let lastAskAnswer = "";
let lastAskSourceNote = "";

async function renderAskTabList() {
  const list = document.getElementById("ask-tab-list");
  if (!list) return;
  list.textContent = "";
  const active = await getActiveTab();
  const { entries, unreadableCount } = askTabEntries(await chrome.tabs.query({ currentWindow: true }), active?.id);
  if (!entries.length) {
    list.textContent = "No other readable tabs open.";
  }
  for (const tab of entries) {
    const label = document.createElement("label");
    label.className = "card-desc";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.tabId = String(tab.id);
    box.dataset.title = tab.title;
    box.dataset.url = tab.url;
    label.append(box, ` ${tab.label}`);
    list.appendChild(label);
  }
  if (unreadableCount > 0) {
    const note = document.createElement("div");
    note.className = "card-desc";
    note.textContent = `${unreadableCount} other ${unreadableCount === 1 ? "tab cannot" : "tabs cannot"} be read (browser pages).`;
    list.appendChild(note);
  }
}
document.getElementById("ask-tabs-details")?.addEventListener("toggle", (e) => {
  if (e.target.open) renderAskTabList();
});

async function gatherAskSources(notes) {
  const sources = [];
  const question = document.getElementById("ask-question").value;

  if (document.getElementById("ask-src-page").checked) {
    try {
      const snap = await sendToActiveTab({ action: "CAPTURE_PAGE_SNAPSHOT" });
      if (snap?.extractedText) sources.push({ kind: "page", title: snap.title, url: snap.url, text: snap.extractedText });
      else notes.push("Current page: no readable text.");
    } catch (err) {
      notes.push(`Current page: could not be read (${err.message}).`);
    }
  }

  for (const box of document.querySelectorAll("#ask-tab-list input[type=checkbox]:checked")) {
    const name = box.dataset.title || box.dataset.url || `tab ${box.dataset.tabId}`;
    try {
      const snap = await chrome.tabs.sendMessage(Number(box.dataset.tabId), { action: "CAPTURE_PAGE_SNAPSHOT" }, { frameId: 0 });
      if (snap?.extractedText) sources.push({ kind: "tab", title: snap.title, url: snap.url, text: snap.extractedText });
      else notes.push(`Tab "${name}": no readable text.`);
    } catch {
      notes.push(`Tab "${name}": skipped, no content script (reload the tab and retry).`);
    }
  }

  const needsStorage = document.getElementById("ask-src-vault").checked || document.getElementById("ask-src-target").checked;
  const { userContextVault = [], activeSessionContext = null } = needsStorage
    ? await chrome.storage.local.get(["userContextVault", "activeSessionContext"])
    : {};
  if (document.getElementById("ask-src-vault").checked) {
    const facts = selectVaultFacts(question, userContextVault);
    if (facts.length) sources.push({ kind: "vault", title: "Your saved facts", text: formatVaultFacts(facts) });
    else notes.push("Vault: no relevant facts for this question.");
  }
  if (document.getElementById("ask-src-target").checked) {
    const text = formatTargetContext(activeSessionContext);
    if (text) sources.push({ kind: "target", title: activeSessionContext.sourceTitle || "Target context", text });
    else notes.push("Target context: none captured yet.");
  }
  return { sources, question };
}

document.getElementById("btn-ask")?.addEventListener("click", async () => {
  const askBtn = document.getElementById("btn-ask");
  const stopBtn = document.getElementById("btn-ask-stop");
  const saveBtn = document.getElementById("btn-ask-save");
  const box = document.getElementById("ask-answer");
  const usedEl = document.getElementById("ask-sources-used");
  if (!document.getElementById("ask-question").value.trim()) return;

  askBtn.disabled = true;
  stopBtn.disabled = false;
  saveBtn.style.display = "none";
  box.style.display = "block";
  box.textContent = "Reading sources...";
  usedEl.textContent = "";
  lastAskAnswer = "";
  let session;
  const signal = Telemetry.startTask("Ask: gathering sources...", 20);
  try {
    const notes = [];
    const { sources, question } = await gatherAskSources(notes);
    const { prompt, used } = buildAskPrompt({ question, sources, guidance: promptGuidance });
    lastAskSourceNote = used.length
      ? used.map((u) => `${u.kind}${u.title ? `: ${u.title}` : ""}${u.truncated ? " (truncated)" : ""}`).join("; ")
      : "none";
    usedEl.textContent = [`Sources used: ${lastAskSourceNote}`, ...notes].join("\n");
    box.textContent = "Thinking on-device...";
    session = await createPromptSession();
    lastAskAnswer = await runObservablePrompt(session, prompt, {
      label: "Ask",
      signal,
      onChunk: (text) => {
        box.textContent = text;
      }
    });
    box.textContent = lastAskAnswer;
    if (lastAskAnswer.trim()) saveBtn.style.display = "";
  } catch (err) {
    box.textContent = signal.aborted ? "Stopped." : describeTaskFailure(err, "Ask");
  } finally {
    session?.destroy();
    Telemetry.endTask();
    askBtn.disabled = false;
    stopBtn.disabled = true;
  }
});

document.getElementById("btn-ask-stop")?.addEventListener("click", () => {
  Telemetry.activeAbortController?.abort();
});

document.getElementById("btn-ask-save")?.addEventListener("click", async () => {
  const saveBtn = document.getElementById("btn-ask-save");
  const question = document.getElementById("ask-question").value.trim();
  const queued = await queueVerifiedDeltas(
    [
      {
        key: question.slice(0, 60) || "Answer",
        proposedValue: lastAskAnswer.trim(),
        category: "custom",
        evidenceSnippet: `Ask answer (${lastAskSourceNote})`,
        confidence: 0.6
      }
    ],
    "Ask answer",
    ""
  );
  saveBtn.style.display = "none";
  document.getElementById("ask-sources-used").textContent += queued
    ? "\nQueued in the Review & Merge Queue on the Observer tab."
    : "\nNot queued: an equal fact is already in the vault or queue.";
});

// Prompt API Sandbox Run
document.getElementById("btn-run-sandbox").addEventListener("click", async () => {
  const input = document.getElementById("sandbox-prompt-input").value.trim();
  const box = document.getElementById("sandbox-response-box");
  if (!input) return;

  box.style.display = "block";
  box.textContent = "Executing prompt on local Gemini Nano...";

  const startTime = performance.now();
  try {
    const session = await createPromptSession();
    const result = await session.prompt(getPrompt("ask", { guidance: promptGuidance, vars: { question: input } }));
    const elapsed = Math.round(performance.now() - startTime);
    box.textContent = `[${elapsed}ms local inference]\n\n${result}`;
    session.destroy();
  } catch (err) {
    box.textContent = describeTaskFailure(err, "Prompt");
  }
});

// Prompt eval samples: counts and scores only are stored, never model outputs.
function evalTerms(id) {
  return document.getElementById(id).value.split(",");
}

async function renderEvalSamples() {
  const box = document.getElementById("sandbox-eval-samples");
  const { promptEvalCases = [] } = await chrome.storage.local.get("promptEvalCases");
  const cases = normalizeCases(promptEvalCases);
  box.textContent = "";
  for (const [i, c] of cases.entries()) {
    const row = document.createElement("div");
    row.className = "btn-row";
    const label = document.createElement("div");
    label.style.cssText = "font-size:11px; flex:1;";
    label.textContent = `#${i + 1}: ${c.input.slice(0, 50)}`;
    const del = document.createElement("button");
    del.className = "btn btn-subtle btn-sm";
    del.textContent = "Delete";
    del.setAttribute("aria-label", `Delete sample ${i + 1}`);
    del.addEventListener("click", async () => {
      const out = document.getElementById("sandbox-eval-results");
      try {
        const { promptEvalCases: stored = [] } = await chrome.storage.local.get("promptEvalCases");
        const next = removeCase(stored, i);
        await chrome.storage.local.set({ promptEvalCases: next });
        out.textContent = `Sample deleted. ${next.length} sample(s) left. The next run starts a new comparison.`;
      } catch (e) {
        out.textContent = `Not deleted: ${e.message}`;
      }
      await renderEvalSamples();
    });
    row.append(label, del);
    box.append(row);
  }
}
renderEvalSamples();

document.getElementById("btn-eval-save").addEventListener("click", async () => {
  const out = document.getElementById("sandbox-eval-results");
  const input = document.getElementById("sandbox-prompt-input").value;
  const { promptEvalCases = [] } = await chrome.storage.local.get("promptEvalCases");
  const before = normalizeCases(promptEvalCases);
  const cases = normalizeCases([
    ...before,
    {
      input,
      expect: {
        mustContain: evalTerms("sandbox-expect-contain"),
        mustNotContain: evalTerms("sandbox-expect-forbid"),
        maxWords: document.getElementById("sandbox-expect-words").value
      }
    }
  ]);
  if (cases.length === before.length) {
    out.textContent = before.length >= MAX_EVAL_CASES
      ? `Not saved: limit of ${MAX_EVAL_CASES} samples reached.`
      : "Not saved: enter a question and at least one expectation (must contain, must not contain, or max words).";
    return;
  }
  await chrome.storage.local.set({ promptEvalCases: cases });
  out.textContent = `${cases.length} sample(s) saved.`;
  await renderEvalSamples();
});

document.getElementById("btn-eval-run").addEventListener("click", async () => {
  const out = document.getElementById("sandbox-eval-results");
  const list = document.getElementById("sandbox-eval-suggestions");
  list.textContent = "";
  const { promptEvalCases = [], promptEvalRuns = [], userContextVault = [] } =
    await chrome.storage.local.get(["promptEvalCases", "promptEvalRuns", "userContextVault"]);
  const cases = normalizeCases(promptEvalCases);
  if (!cases.length) {
    out.textContent = "No saved samples. Save one first.";
    return;
  }
  const results = [];
  try {
    for (const [i, c] of cases.entries()) {
      out.textContent = `Running sample ${i + 1} of ${cases.length}...`;
      const session = await createPromptSession();
      try {
        const text = await session.prompt(getPrompt("ask", { guidance: promptGuidance, vars: { question: c.input } }));
        results.push(scoreOutput(text, c.expect));
      } finally {
        session.destroy?.();
      }
    }
  } catch (err) {
    out.textContent = `Samples not run: ${err.message}`;
    return;
  }
  const key = casesKey(cases);
  const last = promptEvalRuns[promptEvalRuns.length - 1];
  const comparable = last && last.key === key;
  const sum = summarizeRun(results, comparable ? last.score : null);
  const runs = [...promptEvalRuns, { at: Date.now(), key, passed: sum.passed, total: sum.total, score: sum.score }].slice(-10);
  await chrome.storage.local.set({ promptEvalRuns: runs });
  const delta = sum.delta === null ? (last ? "samples changed, not comparable" : "first run") : `${sum.delta >= 0 ? "+" : ""}${Math.round(sum.delta * 100)} pts vs previous`;
  out.textContent = `${sum.passed}/${sum.total} passed, score ${Math.round(sum.score * 100)}% (${delta}).\n` +
    results.map((r, i) => `${r.pass ? "PASS" : "FAIL"} #${i + 1}: ${cases[i].input.slice(0, 50)}`).join("\n");
  const suggestions = suggestFromFailures(results, { vaultValues: userContextVault.map((v) => v.value), guidance: promptGuidance });
  for (const sug of suggestions) {
    const row = document.createElement("div");
    row.className = "item-card";
    const label = document.createElement("div");
    label.style.cssText = "font-size:11px; margin-bottom:4px;";
    label.textContent = `[${sug.promptId}] ${sug.guidance} (${sug.reason})`;
    const row2 = document.createElement("div");
    row2.className = "btn-row";
    const accept = document.createElement("button");
    accept.className = "btn btn-primary btn-sm";
    accept.textContent = "Accept";
    accept.addEventListener("click", async () => {
      try {
        await saveGuidance(appendGuidanceLine(promptGuidance, sug.promptId, sug.guidance, { source: "suggestion" }));
        row.remove();
        out.textContent += "\nGuidance saved. Run samples again to check the effect on all samples.";
      } catch (e) {
        out.textContent = e.message;
      }
    });
    const dismiss = document.createElement("button");
    dismiss.className = "btn btn-subtle btn-sm";
    dismiss.textContent = "Dismiss";
    dismiss.addEventListener("click", () => row.remove());
    row2.append(accept, dismiss);
    row.append(label, row2);
    list.append(row);
  }
});

// ----------------------------------------------------------------------------
// PROMPT GUIDANCE: user-editable guidance injected into every on-device prompt,
// plus model-proposed improvements that need an explicit Accept.
// ----------------------------------------------------------------------------
function guidanceTargetText(target) {
  return target === "global" ? promptGuidance.global : promptGuidance.perPrompt[target] || "";
}

function renderGuidanceCard() {
  const select = document.getElementById("guidance-target");
  const input = document.getElementById("guidance-input");
  const preview = document.getElementById("guidance-preview");
  if (!select || !input || !preview) return;
  const target = select.value;
  if (document.activeElement !== input) input.value = guidanceTargetText(target);
  const previewId = target === "global" ? "ask" : target;
  const sample = { categories: "...", known: "", chunk: "<page text>", field: "<field>", context: "<target context>", facts: "<vault facts>", org: "<org>", title: "<title>", url: "<url>", text: "<page text>", question: "<your question>" };
  preview.textContent = getPrompt(previewId, { guidance: promptGuidance, vars: sample });
  document.getElementById("btn-guidance-revert").disabled = promptGuidance.history.length === 0;
}

async function saveGuidance(next) {
  setPromptGuidance(next);
  await chrome.storage.local.set({ promptGuidance: next });
  renderGuidanceCard();
}

(function initGuidanceCard() {
  const select = document.getElementById("guidance-target");
  if (!select) return;
  for (const id of PROMPT_IDS) select.add(new Option(id, id));
  select.addEventListener("change", renderGuidanceCard);
  document.getElementById("btn-guidance-save").addEventListener("click", () =>
    saveGuidance(applyGuidanceEdit(promptGuidance, select.value, document.getElementById("guidance-input").value))
  );
  document.getElementById("btn-guidance-revert").addEventListener("click", () => saveGuidance(revertGuidance(promptGuidance)));
  document.getElementById("btn-guidance-suggest").addEventListener("click", suggestGuidanceImprovements);
})();

async function suggestGuidanceImprovements() {
  const status = document.getElementById("guidance-suggest-status");
  const list = document.getElementById("guidance-suggestions");
  list.textContent = "";
  const { fillOutcomes = [], deltaFeedback = {}, learnedAliases = [], userContextVault = [] } =
    await chrome.storage.local.get(["fillOutcomes", "deltaFeedback", "learnedAliases", "userContextVault"]);
  const request = proposeGuidanceRequest({
    outcomes: fillOutcomes,
    deltaFeedback,
    learnedAliasCount: learnedAliases.length,
    guidance: promptGuidance
  });
  if (!request) {
    status.textContent = "Not enough usage yet. Fill some forms and review some suggested facts first.";
    return;
  }
  status.textContent = "Asking the on-device model...";
  let session;
  try {
    session = await createPromptSession();
    const text = await session.prompt(request, { responseConstraint: SUGGESTION_SCHEMA });
    const suggestions = parseGuidanceSuggestions(text, { vaultValues: userContextVault.map((v) => v.value) });
    status.textContent = suggestions.length ? "Review each suggestion. Nothing changes until you accept." : "The model returned no usable suggestions.";
    for (const sug of suggestions) {
      const row = document.createElement("div");
      row.className = "item-card";
      const label = document.createElement("div");
      label.style.cssText = "font-size:11px; margin-bottom:4px;";
      label.textContent = `[${sug.promptId}] ${sug.guidance} (${sug.reason})`;
      const row2 = document.createElement("div");
      row2.className = "btn-row";
      const accept = document.createElement("button");
      accept.className = "btn btn-primary btn-sm";
      accept.textContent = "Accept";
      accept.addEventListener("click", async () => {
        try {
          await saveGuidance(appendGuidanceLine(promptGuidance, sug.promptId, sug.guidance, { source: "suggestion" }));
          row.remove();
        } catch (e) {
          status.textContent = e.message;
        }
      });
      const dismiss = document.createElement("button");
      dismiss.className = "btn btn-subtle btn-sm";
      dismiss.textContent = "Dismiss";
      dismiss.addEventListener("click", () => row.remove());
      row2.append(accept, dismiss);
      row.append(label, row2);
      list.append(row);
    }
  } catch (err) {
    status.textContent = describeTaskFailure(err, "Suggestion");
  } finally {
    session?.destroy?.();
  }
}

// ----------------------------------------------------------------------------
// PHASES 2 & 4: FORM-EXPORT & FORM-EXPORT++
// ----------------------------------------------------------------------------
document.getElementById("btn-scan-page").addEventListener("click", async () => {
  const isPlus = document.getElementById("chk-export-plus").checked;
  const includeValues = document.getElementById("chk-include-values").checked;
  const preview = document.getElementById("export-preview-box");
  const actionsRow = document.getElementById("export-actions-row");

  preview.textContent = "Traversing active tab DOM for interactive form elements...";

  try {
    const tab = await getActiveTab();
    if (!tab?.id) throw new Error("No active web page found.");

    const doc = await sendToActiveTab({
      action: "SCAN_PAGE_FORMS",
      options: { includePageContext: isPlus, includeCurrentValues: includeValues }
    });

    // Phase 4: Enriched context via built-in Summarizer
    if (isPlus && doc.pageContext?.rawTextExcerpt && "Summarizer" in self) {
      try {
        preview.textContent = "Summarizing page context with Built-in Summarizer API...";
        const summarizer = await self.Summarizer.create({ type: "key-points", length: "short" });
        doc.pageContext.pageSummary = await summarizer.summarize(doc.pageContext.rawTextExcerpt);
        summarizer.destroy();
      } catch (sumErr) {
        doc.pageContext.pageSummary = `Summarizer note: ${sumErr.message}`;
      }
    }

    currentExportDoc = doc;
    preview.textContent = JSON.stringify(doc, null, 2);
    actionsRow.style.display = "flex";
  } catch (err) {
    preview.textContent = `${describeTaskFailure(err, "Scan")}\nMake sure you are on an http/https web page and refresh the page if the extension was just installed.`;
  }
});

document.getElementById("btn-download-export-json").addEventListener("click", () => {
  if (!currentExportDoc) return;
  const jsonStr = JSON.stringify(currentExportDoc, null, 2);
  const blob = new Blob([jsonStr], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const domain = currentExportDoc.pageContext?.domain || "page";
  a.href = url;
  a.download = `${domain}-form-schema-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

document.getElementById("btn-copy-export-json").addEventListener("click", () => {
  if (!currentExportDoc) return;
  navigator.clipboard.writeText(JSON.stringify(currentExportDoc, null, 2));
  const btn = document.getElementById("btn-copy-export-json");
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy to Clipboard"), 1500);
});

// Phase 8: Run UX & Touch Target Audit Button
document.getElementById("btn-run-ux-audit")?.addEventListener("click", async () => {
  const preview = document.getElementById("export-preview-box");
  preview.textContent = "Running UX & Touch Target Audit on active tab...";

  try {
    const report = await sendToActiveTab({ action: "RUN_UX_AUDIT" });
    preview.textContent = JSON.stringify(report, null, 2);
  } catch (err) {
    preview.textContent = describeTaskFailure(err, "UX Audit");
  }
});

// ----------------------------------------------------------------------------
// PHASES 3 & 6: FORM-IMPORT & FORM-IMPORT++ (TWO-STAGE INSTANT FILL)
// ----------------------------------------------------------------------------
document.getElementById("btn-autofill-now").addEventListener("click", async () => {
  const btn = document.getElementById("btn-autofill-now");
  const statusBox = document.getElementById("autofill-status-box");
  btn.disabled = true;
  btn.textContent = "⚡ Stage 1: Instant Vault Fill (<15ms)...";

  const abortSignal = Telemetry.startTask("[1/4] Scanning DOM for interactive inputs...", 20);

  try {
    const tab = await getActiveTab();
    if (!tab?.id) throw new Error("No active web page found.");

    // 1. Scan current form (reads fresh or pre-scanned cache)
    const scanT0 = performance.now();
    const doc = await sendToActiveTab({
      action: "SCAN_PAGE_FORMS",
      options: { includePageContext: true, includeCurrentValues: true }
    });

    Telemetry.logTrace({
      type: "SCAN",
      title: "Scan Page Forms",
      durationMs: performance.now() - scanT0,
      details: { url: doc?.pageContext?.url, fieldCount: doc?.fields?.length || 0 }
    });

    if (!doc?.fields || doc.fields.length === 0) {
      statusBox.textContent = "No fillable interactive form inputs found on this page.";
      Telemetry.endTask();
      return;
    }

    Telemetry.updateStage("[2/4] Instant Vault Match & Consent Guard (<15ms)...", 45);

    const { userContextVault = [], activeSessionContext = null } = await chrome.storage.local.get([
      "userContextVault",
      "activeSessionContext"
    ]);

    // Learned mappings (#1540): only confirmed ones are used by matchField.
    let learned = [];
    try {
      learned = (await chrome.runtime.sendMessage({ type: "learning:getMappings" }))?.mappings || [];
    } catch {
      learned = [];
    }
    const host = webHostname(tab.url);
    const matchOpts = { learned, host };

    const fillId = `fill_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
    const autoConsent = document.getElementById("chk-agree-consent")?.checked ?? true;
    const longFormFields = [];

    // 2. Stage 1: Deterministic fast match for atomic fields + pre-drafted Cover Letter check
    for (let i = 0; i < doc.fields.length; i++) {
      const field = doc.fields[i];

      // Rule A: Skip conditional follow-up fields ("If so, ...", "If yes, ...") when preceding boolean field is false ("No")
      if (/^(if so|if yes|if applicable|please specify if)/i.test((field.query || "").trim())) {
        const prevField = i > 0 ? doc.fields[i - 1] : null;
        if (!prevField || prevField.value === false || prevField.value === "No" || prevField.value === null || prevField.value === "") {
          field.value = ""; // Explicitly leave blank
          field.skipLLM = true;
          continue;
        }
      }

      if (field.skipLLM) continue;
      if (field.value !== null && field.value !== undefined && field.value !== "" && field.value !== false) continue;

      const q = (field.query || "").toLowerCase().trim();

      // Rule B: Expand long-form / open-ended synthesis detection to catch project & engineering prompts:
      const isLongFormEssay =
        field.inputType === "textarea" ||
        /cover letter|why do you|why are you|motivation|share something cool|something you've built|tell us about|describe a project|what failed|note to hiring|additional info|why openmind|why us/i.test(q);

      if (isLongFormEssay) {
        // If vault has a direct deterministic match for this essay/project prompt (e.g. "Share something cool you've built"), use it!
        const essayMatch = matchField(fieldMatchInput(field, q), userContextVault, matchOpts);
        const matchedVaultEssay = essayMatch ? essayMatch.entry.value : null;
        if (essayMatch) tagMatch(field, essayMatch);
        if (matchedVaultEssay) {
          field.value = matchedVaultEssay;
          field.confidence = essayMatch.confidence;
          continue;
        }

        // If we already pre-drafted a tailored cover letter for activeSessionContext, use it in 0ms!
        if (activeSessionContext?.preDraftedCoverLetter && /cover letter|note|motivation|why/i.test(q)) {
          field.value = activeSessionContext.preDraftedCoverLetter;
          field.confidence = 0.99;
          field.pass = "prompt";
        } else {
          longFormFields.push(field);
        }
        continue;
      }

      // Check consent checkboxes
      if (field.inputType === "checkbox" && (field.semanticCategory === "legal.consent" || /agree|terms|privacy/i.test(q))) {
        if (autoConsent) {
          field.value = true;
          field.confidence = 0.95;
        }
        continue;
      }

      // Deterministic match for standard atomic inputs
      const atomicMatch = matchField(fieldMatchInput(field, q), userContextVault, matchOpts);
      if (atomicMatch) {
        const { entry, pass, confidence } = atomicMatch;
        tagMatch(field, atomicMatch);
        if (pass === "autocomplete") {
          field.value = entry.value;
        } else if (field.inputType === "checkbox") {
          field.value = /^(yes|true|1)$/i.test(String(entry.value).trim());
        } else {
          field.value = entry.value;
        }
        field.confidence = confidence;
      }
    }

    // 3. Immediately hydrate Stage 1 fields on the live DOM so user sees instant results (<15ms)!
    const stage1T0 = performance.now();
    const stage1Result = await sendToActiveTab({
      action: "HYDRATE_PAGE_FORMS",
      document: doc,
      options: { highlightConfidence: true, trackFill: true, fillId }
    });
    Telemetry.logTrace({
      type: "HYDRATE",
      title: "Hydrate Stage 1 Atomic Fields",
      durationMs: performance.now() - stage1T0,
      details: summarizeHydrationResult(stage1Result)
    });
    reportFillVerdicts(doc, stage1Result);
    renderFillStats();
    statusBox.textContent = `Stage 1 filled ${stage1Result.filledCount} fields instantly...`;

    // 4. Stage 2: On-Device LLM synthesis ONLY for remaining tailored essays or unmapped fields
    if (longFormFields.length > 0) {
      btn.textContent = `🧠 Stage 2: Tailoring ${longFormFields.length} Essay/Cover Letter Field(s)...`;
      Telemetry.updateStage(`[3/4] Streaming Gemini Nano Synthesis for ${longFormFields.length} field(s)...`, 65);
      const session = await getClonedSession();

      if (session) {
        const portfolioUrl =
          userContextVault.find((v) => v.id === "social.portfolio" || /portfolio|website/i.test(v.key))?.value || "";
        const githubUrl =
          userContextVault.find((v) => v.id === "social.github" || /github/i.test(v.key))?.value || "";

        for (const field of longFormFields) {
          if (field.skipLLM) continue;
          const ctx = activeSessionContext || doc.pageContext || {};
          const bioEntry = userContextVault.find((v) => v.id === "custom.bio")?.value || "";
          const expEntries = userContextVault
            .filter((v) => ["professional", "education", "social"].includes(v.category))
            .map((v) => `${v.key}: ${v.value}`)
            .join("\n");

          const prompt = getPrompt("fill_answer", {
            guidance: promptGuidance,
            vars: {
              field: field.query,
              context:
                `- Role: ${ctx.role || ctx.roleOrTopic || document.title}\n` +
                `- Company: ${ctx.organization || "this organization"}\n` +
                `- Requirements: ${(ctx.keyRequirements || []).join("; ")}\n` +
                `- Tech Stack: ${(ctx.techStack || ctx.keywords || []).join(", ")}`,
              facts:
                `${bioEntry}\n${expEntries}\n` +
                (portfolioUrl ? `- Portfolio URL: ${portfolioUrl}\n` : "") +
                (githubUrl ? `- GitHub URL: ${githubUrl}\n` : ""),
              org: ctx.organization || "the role"
            }
          });

          let lastLiveUpdate = 0;
          const synthesized = await runObservablePrompt(session, prompt, {
            label: `Tailor "${field.query}"`,
            signal: abortSignal,
            onChunk: (chunk) => {
              const now = performance.now();
              if (now - lastLiveUpdate > 250) {
                lastLiveUpdate = now;
                sendToActiveTab({
                  action: "HYDRATE_PAGE_FORMS",
                  document: { fields: [{ ...field, value: chunk }] },
                  options: { highlightConfidence: false }
                }).catch(() => {});
              }
            }
          });

          if (synthesized && synthesized.trim()) {
            let finalText = synthesized.trim();
            // Deterministic Portfolio Link Guarantee in Stage 2 Live Synthesis
            const isProjectOrShowcase = /share something cool|built|project|portfolio|repo|link|work sample|cover letter/i.test(field.query);
            if (isProjectOrShowcase && portfolioUrl && !finalText.includes(portfolioUrl)) {
              const linkLine = `Portfolio: ${portfolioUrl}` + (githubUrl ? ` | GitHub: ${githubUrl}` : "");
              finalText = `${finalText}\n\n${linkLine}`;
            }
            field.value = finalText;
            field.confidence = 0.92;
            field.pass = "prompt";
          }
        }
        session.destroy?.();

        // Hydrate the newly synthesized essay fields
        Telemetry.updateStage("[4/4] Hydrating Final Form on Live DOM...", 90);
        const stage2T0 = performance.now();
        const finalResult = await sendToActiveTab({
          action: "HYDRATE_PAGE_FORMS",
          document: doc,
          options: { highlightConfidence: true, trackFill: true, fillId }
        });
        Telemetry.logTrace({
          type: "HYDRATE",
          title: "Hydrate Stage 2 Essays",
          durationMs: performance.now() - stage2T0,
          details: finalResult
        });
        statusBox.textContent = `Auto-Fill Completed!\n\n` + JSON.stringify(finalResult, null, 2);
      }
    } else {
      statusBox.textContent = `Auto-Fill Completed (Instant Pre-Draft Used)!\n\n` + JSON.stringify(stage1Result, null, 2);
    }
  } catch (err) {
    statusBox.textContent = describeTaskFailure(err, "Auto-Fill");
    Telemetry.logTrace({
      type: "ERROR",
      title: "Auto-Fill Error",
      durationMs: 0,
      details: redactSecrets(err.message)
    });
  } finally {
    Telemetry.endTask();
    btn.disabled = false;
    btn.textContent = "✨ Auto-Fill Active Form with AI";
  }
});

// Fill outcomes (#1539): "Last fill: N kept, M edited, K cleared" plus the
// kept-rate over the last 20 fills. Read-only; the worker owns the data.
async function renderFillStats() {
  const el = document.getElementById("fill-stats-line");
  if (!el) return;
  try {
    const res = await chrome.runtime.sendMessage({ type: "learning:getOutcomes" });
    if (!res?.last) {
      el.textContent = "Last fill: no submitted fills recorded yet.";
      return;
    }
    const { kept, edited, cleared } = res.last;
    const rate = Math.round(res.keptRate * 100);
    el.textContent = `Last fill: ${kept} kept, ${edited} edited, ${cleared} cleared. Kept-rate over last ${res.fills} fills: ${rate}%`;
  } catch {
    el.textContent = "";
  }
}
renderFillStats();

// Input for matchField. name/type rebuild the signature the content script
// records at submit time (type is the element's type attribute or tag name).
function fieldMatchInput(field, label) {
  const t = field.inputType || "";
  const type = t.startsWith("select") ? "select" : t === "radio-group" ? "radio" : t;
  return {
    label,
    autocomplete: field.constraints?.autocomplete,
    name: field.locator?.name || "",
    type
  };
}

// Tag a doc field with the vault entry and pass that filled it, so the content
// script can report the outcome after submit.
function tagMatch(field, match) {
  field.entryId = match.entry.id;
  field.entryKey = match.entry.key;
  field.pass = match.pass;
}

// Phase 3: Manual JSON Import
document.getElementById("btn-run-manual-import").addEventListener("click", async () => {
  const fileInput = document.getElementById("file-import-input");
  const statusBox = document.getElementById("autofill-status-box");
  if (!fileInput.files.length) {
    alert("Please select a JSON file first.");
    return;
  }

  try {
    const text = await fileInput.files[0].text();
    const doc = JSON.parse(text);
    const res = await sendToActiveTab({
      action: "HYDRATE_PAGE_FORMS",
      document: doc,
      options: { highlightConfidence: true }
    });

    statusBox.textContent = `Manual Import Complete:\n` + JSON.stringify(res, null, 2);
  } catch (err) {
    statusBox.textContent = `Import Error: ${err.message}`;
  }
});

// ----------------------------------------------------------------------------
// PHASE 5: USER-CONTEXT VAULT (CRUD & LOCAL STORAGE)
// ----------------------------------------------------------------------------
// Entry deduplication is lib/vault.js's deduplicateVault (imported above).

async function renderVault() {
  const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
  // Read-only dedupe for display: the worker owns the stored form and
  // sanitizes on write, so the panel never writes here (the old write-back
  // fired on every vault-search keystroke and could clobber a concurrent
  // observer delta merge).
  const cleanedVault = deduplicateVault(userContextVault);

  const container = document.getElementById("vault-entries-container");
  const search = document.getElementById("vault-search-input").value.toLowerCase().trim();
  const categoryFilter = document.getElementById("vault-category-filter").value;

  const filtered = cleanedVault.filter((entry) => {
    const matchesCat = categoryFilter === "all" || entry.category === categoryFilter;
    const matchesSearch =
      !search ||
      entry.key.toLowerCase().includes(search) ||
      String(entry.value).toLowerCase().includes(search) ||
      (entry.tags && entry.tags.some((t) => t.toLowerCase().includes(search)));
    return matchesCat && matchesSearch;
  });

  document.getElementById("vault-count-badge").textContent = `${cleanedVault.length} facts`;
  renderVaultOnboarding(cleanedVault);

  // First-run flow: the vault ships empty (no seed PII). Point the user at the
  // import path — the file format is exactly what "Export Vault JSON" produces.
  if (cleanedVault.length === 0) {
    container.innerHTML = `
      <div class="item-card" style="text-align:center; padding:16px;">
        <div style="font-weight:600; color:#e2e8f0; margin-bottom:6px;">Your vault is empty</div>
        <div style="color:var(--text-muted); font-size:11px; margin-bottom:10px;">
          Import a previously exported Vault JSON file to restore your profile locally, or add facts manually below.
          Facts stay in this browser unless you press Sync with Google Drive, which stores them in this extension's private Drive app folder.
        </div>
        <button id="btn-first-run-import" class="btn btn-secondary btn-sm">Import Profile JSON</button>
      </div>`;
    document.getElementById("btn-first-run-import")?.addEventListener("click", () => {
      document.getElementById("file-import-vault").click();
    });
    return;
  }

  if (filtered.length === 0) {
    container.innerHTML = `<div style="text-align:center; color:var(--text-muted); padding:16px;">No matching facts found.</div>`;
    return;
  }

  container.innerHTML = filtered
    .map(
      (entry) => `
    <div class="item-card" data-id="${escapeHtml(entry.id)}">
      <div class="item-header">
        <span class="item-key">${escapeHtml(entry.key)}</span>
        <div style="display:flex; align-items:center; gap:6px;">
          <span class="item-cat">${escapeHtml(entry.category)}</span>
          <button class="btn-delete-fact" data-id="${escapeHtml(entry.id)}" style="background:transparent; border:none; color:var(--rose); cursor:pointer; font-size:12px;">✕</button>
        </div>
      </div>
      <input type="text" class="vault-value-input" data-id="${escapeHtml(entry.id)}" value="${escapeHtml(String(entry.value))}" style="margin-top:2px;" />
    </div>`
    )
    .join("");

  // Attach delete listeners
  container.querySelectorAll(".btn-delete-fact").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await chrome.runtime.sendMessage({ type: "vault:delete", id: btn.dataset.id });
      renderVault();
    });
  });

  // Attach inline edit listeners
  container.querySelectorAll(".vault-value-input").forEach((inp) => {
    inp.addEventListener("change", async () => {
      const id = inp.dataset.id;
      const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
      const target = userContextVault.find((e) => e.id === id);
      if (!target) return;
      await chrome.runtime.sendMessage({ type: "vault:upsert", entry: { ...target, value: inp.value } });
    });
  });
}

document.getElementById("vault-search-input").addEventListener("input", renderVault);
document.getElementById("vault-category-filter").addEventListener("change", renderVault);

// Add New Fact
document.getElementById("btn-add-fact").addEventListener("click", async () => {
  const keyInput = document.getElementById("new-fact-key");
  const valInput = document.getElementById("new-fact-value");
  const catInput = document.getElementById("new-fact-category");

  const key = keyInput.value.trim();
  const value = valInput.value.trim();
  const category = catInput.value;

  if (!key || !value) {
    alert("Please provide both a label and a value.");
    return;
  }

  const entry = {
    id: `${category}.${Date.now()}`,
    category,
    key,
    aliases: [key.toLowerCase()],
    value,
    tags: [category, "manual"],
    updatedAt: new Date().toISOString()
  };

  await chrome.runtime.sendMessage({ type: "vault:upsert", entry });
  keyInput.value = "";
  valInput.value = "";
  renderVault();
});

// Drive sync (#1709): one button, one status line. The worker does the work.
const syncButton = document.getElementById("btn-drive-sync");
const syncStatus = document.getElementById("drive-sync-status");

function showSyncedAt(ts) {
  syncStatus.textContent = ts ? `Last synced: ${new Date(ts).toLocaleString()}` : "Last synced: never";
}

chrome.storage.local.get(["driveSyncedAt"]).then((r) => showSyncedAt(r.driveSyncedAt));
syncButton.addEventListener("click", async () => {
  syncButton.disabled = true;
  syncStatus.textContent = "Syncing...";
  const started = Date.now();
  try {
    const res = await chrome.runtime.sendMessage({ type: "drive:sync" });
    if (!res) throw new Error("The extension background worker did not answer. Reload the extension and try again.");
    if (res.error) throw Object.assign(new Error(res.error), { info: res.errorInfo });
    if (res.promptGuidance) await chrome.storage.local.set({ promptGuidance: res.promptGuidance });
    showSyncedAt(res.syncedAt);
    renderVault();
  } catch (err) {
    const info = err.info || syncErrorInfo(err);
    syncStatus.textContent = formatSyncFailure(info);
    Telemetry.logTrace({ type: "ERROR", title: `Drive sync failed: ${info.code}`, durationMs: Date.now() - started, details: info });
  } finally {
    syncButton.disabled = false;
  }
});

// Export & Import Vault JSON
document.getElementById("btn-export-vault").addEventListener("click", async () => {
  const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
  const blob = new Blob([JSON.stringify(userContextVault, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `user-context-vault-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

document.getElementById("btn-import-vault-trigger").addEventListener("click", () => {
  document.getElementById("file-import-vault").click();
});

document.getElementById("file-import-vault").addEventListener("change", async (e) => {
  if (!e.target.files.length) return;
  try {
    const text = await e.target.files[0].text();
    const imported = JSON.parse(text);
    if (!Array.isArray(imported)) throw new Error("Expected an array of context entries.");
    const { userContextVault } = await chrome.storage.local.get(["userContextVault"]);
    const d = describeVaultImport(userContextVault, imported);
    if (d.importCount === 0) {
      alert("No valid entries were found in that file. Your vault was not changed.");
      return;
    }
    if (!confirm(d.message)) return;
    await chrome.runtime.sendMessage({ type: "vault:replace", entries: imported });
    renderVault();
    alert(d.dropped === 0 ? "Vault successfully restored!" : `Vault restored. ${d.dropped} invalid entr${d.dropped === 1 ? "y was" : "ies were"} skipped.`);
  } catch (err) {
    alert("Failed to import vault JSON: " + err.message);
  } finally {
    e.target.value = "";
  }
});

// ----------------------------------------------------------------------------
// PHASE 7: OBSERVER & DIFF APPROVAL QUEUE
// ----------------------------------------------------------------------------
// Verified-fill verdicts for fields matched to a vault entry. The content
// script itemizes failures (failedFields) and successes (filledFields); only
// fields carrying an entryId can be attributed. The worker records them.
function reportFillVerdicts(doc, result) {
  const entryByField = new Map((doc.fields || []).filter((f) => f.entryId).map((f) => [f.fieldId, f.entryId]));
  const verdicts = [
    ...(result?.filledFields || []).map((f) => ({ ...f, ok: true })),
    ...(result?.failedFields || []).map((f) => ({ ...f, ok: false }))
  ]
    .filter((f) => entryByField.has(f.fieldId))
    .map((f) => ({ label: f.query, entryId: entryByField.get(f.fieldId), ok: f.ok }));
  if (verdicts.length === 0) return;
  chrome.runtime.sendMessage({ type: "learning:recordVerdicts", verdicts }).catch(() => {});
}

// Learned section: deterministic proposals, optionally refined by the
// on-device LanguageModel (untrusted output, strict schema + verifyProposal).
async function refineWithLanguageModel(proposals, cases, vault) {
  try {
    const lm = self.LanguageModel || self.ai?.languageModel;
    if (!lm || proposals.length === 0) return proposals;
    const avail = await lm.availability?.();
    // "downloadable" is skipped on purpose: creating a session would start a
    // multi-GB model download from a queue render. Deterministic fallback.
    if (avail !== "available") return proposals;
    const session = await createPromptSession();
    try {
      const text = await session.prompt(buildRefinementPrompt(proposals), { responseConstraint: REFINEMENT_SCHEMA });
      return mergeRefinement(proposals, parseRefinement(text, proposals, cases, vault));
    } finally {
      session.destroy?.();
    }
  } catch {
    return proposals;
  }
}

async function renderLearned() {
  const box = document.getElementById("learned-container");
  if (!box) return;
  let res;
  let mres;
  try {
    res = await chrome.runtime.sendMessage({ type: "learning:getProposals" });
    mres = await chrome.runtime.sendMessage({ type: "learning:getMappings" });
  } catch {
    return;
  }
  if (!res || res.error) return;
  const proposals = await refineWithLanguageModel(res.proposals || [], res.cases || [], res.vault || []);
  const keyOf = (id) => (res.vault || []).find((e) => e.id === id)?.key || id;
  // One flat list: aliases you added (#1529) and field mappings learned from
  // your edits (#1540). Delete on every row, Confirm only on unconfirmed rows.
  const rows = mres?.rows || [];
  if (proposals.length === 0 && rows.length === 0) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `
    <div style="font-weight:700; font-size:11.5px; margin: 8px 0 4px;">Learned</div>
    ${proposals
      .map(
        (p, i) => `
      <div class="item-card" style="margin-bottom:6px;">
        <div style="font-size:11px;">Alias <b>${escapeHtml(p.alias)}</b> &rarr; ${escapeHtml(keyOf(p.entryId))}
          <span style="color:var(--text-muted);">(${Number(p.count) || 0} corrections)</span></div>
        <div class="btn-row" style="margin-top:4px;">
          <button class="btn btn-primary btn-sm btn-promote-alias" data-index="${i}">Add alias</button>
        </div>
      </div>`
      )
      .join("")}
    ${rows
      .map(
        (r, i) => `
      <div class="item-card" style="margin-bottom:6px;">
        <div style="font-size:11px;"><b>${escapeHtml(r.from)}</b> &rarr; ${escapeHtml(r.to)}
          <span style="color:var(--text-muted);">(${escapeHtml(r.origin)})</span></div>
        <div class="btn-row" style="margin-top:4px;">
          ${r.unconfirmed ? `<button class="btn btn-primary btn-sm btn-confirm-learned" data-index="${i}">Confirm</button>` : ""}
          <button class="btn btn-sm btn-delete-learned" data-index="${i}">Delete</button>
        </div>
      </div>`
      )
      .join("")}`;
  box.querySelectorAll(".btn-promote-alias").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const { label, entryId, alias } = proposals[Number(btn.dataset.index)];
      const r = await chrome.runtime.sendMessage({ type: "learning:promote", proposal: { label, entryId, alias } });
      if (r && r.ok === false) alert(`Alias not added: ${r.reason}`);
      renderVault();
      renderLearned();
    });
  });
  box.querySelectorAll(".btn-confirm-learned").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await chrome.runtime.sendMessage({ type: "learning:confirmMapping", sig: rows[Number(btn.dataset.index)].sig });
      renderLearned();
    });
  });
  box.querySelectorAll(".btn-delete-learned").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const r = rows[Number(btn.dataset.index)];
      await chrome.runtime.sendMessage(
        r.kind === "alias"
          ? { type: "learning:deleteAlias", entryId: r.entryId, alias: r.from }
          : { type: "learning:deleteMapping", sig: r.sig }
      );
      if (r.kind === "alias") renderVault();
      renderLearned();
    });
  });
}

async function renderObserverQueue() {
  renderLearned();
  const { pendingDeltas = [] } = await chrome.storage.local.get(["pendingDeltas"]);
  const container = document.getElementById("observer-deltas-container");
  const countBadge = document.getElementById("observer-queue-count");
  const tabLabel = document.getElementById("tab-observer-label");
  const bulkRow = document.getElementById("observer-bulk-actions");

  countBadge.textContent = `${pendingDeltas.length} pending`;
  tabLabel.textContent = pendingDeltas.length > 0 ? `Observer (${pendingDeltas.length})` : "Observer";
  bulkRow.style.display = pendingDeltas.length > 0 ? "flex" : "none";

  if (pendingDeltas.length === 0) {
    container.innerHTML = `
      <div style="text-align: center; color: var(--text-muted); padding: 16px 0;">
        No pending fact updates. Snapshot a page or submit a form to learn new facts.
      </div>`;
    return;
  }

  container.innerHTML = pendingDeltas
    .map((delta) => {
      const isCreate = delta.action === "create";
      const badgeClass = isCreate ? "delta-badge-create" : "delta-badge-update";
      const actionText = isCreate ? "New Fact" : "Update Existing";

      return `
      <div class="item-card" data-delta-id="${escapeHtml(delta.deltaId)}" style="margin-bottom:8px;">
        <div class="item-header">
          <span class="${badgeClass}">${actionText}</span>
          <span style="font-size:10px; color:var(--text-muted);">${Math.round(delta.confidence * 100)}% confidence</span>
        </div>
        <div style="font-weight:700; color:white; font-size:11.5px; margin: 2px 0;">${escapeHtml(delta.key)}</div>
        ${delta.previousValue ? `<div style="font-size:10.5px; color:var(--rose); text-decoration:line-through;">Previous: ${escapeHtml(delta.previousValue)}</div>` : ""}
        <div style="font-size:11px; color:var(--accent-light); font-weight:600;">Proposed: ${escapeHtml(delta.proposedValue)}</div>
        <div style="font-size:10px; color:var(--text-muted); font-style:italic; margin-top:2px;">${escapeHtml(delta.evidenceSnippet || "")}</div>
        <div class="btn-row" style="margin-top:6px;">
          <button class="btn btn-primary btn-sm btn-approve-delta" data-delta-id="${escapeHtml(delta.deltaId)}">Approve & Merge</button>
          <button class="btn btn-subtle btn-sm btn-discard-delta" data-delta-id="${escapeHtml(delta.deltaId)}">Discard</button>
        </div>
      </div>`;
    })
    .join("");

  // Attach Approve listeners
  container.querySelectorAll(".btn-approve-delta").forEach((btn) => {
    btn.addEventListener("click", () => approveDelta(btn.dataset.deltaId));
  });

  // Attach Discard listeners
  container.querySelectorAll(".btn-discard-delta").forEach((btn) => {
    btn.addEventListener("click", () => discardDelta(btn.dataset.deltaId));
  });
}

// Counts approve/discard per category for the guidance suggester. Written by
// the panel to its own key; the vault and pendingDeltas stay worker-only.
let deltaFeedbackChain = Promise.resolve();
function noteDeltaFeedback(deltaIds, decision) {
  const run = deltaFeedbackChain.then(() => writeDeltaFeedback(deltaIds, decision));
  deltaFeedbackChain = run.catch(() => {});
  return run;
}

async function writeDeltaFeedback(deltaIds, decision) {
  const { pendingDeltas = [], deltaFeedback = {} } = await chrome.storage.local.get(["pendingDeltas", "deltaFeedback"]);
  let next = deltaFeedback;
  for (const d of pendingDeltas) {
    if (deltaIds.includes(d.deltaId)) next = recordDeltaFeedback(next, d.category, decision);
  }
  if (next !== deltaFeedback) await chrome.storage.local.set({ deltaFeedback: next });
}

async function pendingDeltaIds() {
  const { pendingDeltas = [] } = await chrome.storage.local.get(["pendingDeltas"]);
  return pendingDeltas.map((d) => d.deltaId);
}

async function approveDelta(deltaId) {
  await noteDeltaFeedback([deltaId], "approved");
  // The worker applies the delta and dedupes the vault; the panel re-renders
  // from storage afterwards.
  await chrome.runtime.sendMessage({ type: "delta:approve", deltaId });
  renderVault();
  renderObserverQueue();
}

async function discardDelta(deltaId) {
  await noteDeltaFeedback([deltaId], "discarded");
  await chrome.runtime.sendMessage({ type: "delta:discard", deltaId });
  renderObserverQueue();
}

document.getElementById("btn-approve-all-deltas").addEventListener("click", async () => {
  await noteDeltaFeedback(await pendingDeltaIds(), "approved");
  await chrome.runtime.sendMessage({ type: "delta:approveAll" });
  renderVault();
  renderObserverQueue();
});

document.getElementById("btn-discard-all-deltas").addEventListener("click", async () => {
  await noteDeltaFeedback(await pendingDeltaIds(), "discarded");
  await chrome.runtime.sendMessage({ type: "delta:discardAll" });
  renderObserverQueue();
});

// ----------------------------------------------------------------------------
// OBSERVER OPT-IN (PER-DOMAIN ALLOWLIST)
// Passive submit observation is off for every site until the user enables it
// for that domain here; the content script checks the same allowlist at
// submit time and fails closed.
// ----------------------------------------------------------------------------
async function getActiveHostname() {
  try {
    const tab = await getActiveTab();
    return webHostname(tab?.url);
  } catch {
    return "";
  }
}

async function renderObserverDomainToggle() {
  const btn = document.getElementById("btn-observe-toggle");
  if (!btn) return;
  const hostname = await getActiveHostname();
  let enabled = false;
  if (hostname) {
    const { observerDomainAllowlist = [] } = await chrome.storage.local.get(["observerDomainAllowlist"]);
    enabled = Array.isArray(observerDomainAllowlist) && observerDomainAllowlist.includes(hostname);
  }
  const view = observeView({ hostname, enabled });
  btn.textContent = view.label;
  btn.disabled = view.disabled;
  btn.setAttribute("aria-pressed", String(view.pressed));
  btn.classList.toggle("btn-primary", !view.pressed);
  btn.classList.toggle("btn-secondary", view.pressed);
  const hint = document.getElementById("observe-hint");
  if (hint) hint.textContent = view.hint;
}

async function setObserveDomain(hostname, enabled) {
  const { observerDomainAllowlist = [] } = await chrome.storage.local.get(["observerDomainAllowlist"]);
  const updated = new Set(Array.isArray(observerDomainAllowlist) ? observerDomainAllowlist : []);
  if (enabled) updated.add(hostname);
  else updated.delete(hostname);
  await chrome.storage.local.set({ observerDomainAllowlist: Array.from(updated) });

  Telemetry.logTrace({
    type: "OBSERVER",
    title: `Submit observer ${enabled ? "enabled" : "disabled"} for ${hostname}`,
    durationMs: 0,
    details: { hostname, enabled }
  });
}

document.getElementById("btn-observe-toggle")?.addEventListener("click", async (e) => {
  const hostname = await getActiveHostname();
  if (!hostname) return;
  await setObserveDomain(hostname, e.currentTarget.getAttribute("aria-pressed") !== "true");
  await renderObserverDomainToggle();
});

// ----------------------------------------------------------------------------
// SESSION CONTEXT CONTROLLER (TARGET JOB / EPHEMERAL PAGE CONTEXT)
// ----------------------------------------------------------------------------
async function renderSessionContext() {
  const { activeSessionContext = null } = await chrome.storage.local.get(["activeSessionContext"]);

  const observerCard = document.getElementById("session-context-card");
  const observerSummary = document.getElementById("session-context-summary");
  const observerBadge = document.getElementById("session-context-badge");
  const autofillCard = document.getElementById("autofill-target-context-card");
  const autofillRole = document.getElementById("autofill-target-role");
  const autofillDesc = document.getElementById("autofill-target-desc");

  if (activeSessionContext) {
    const targetLabel = document.getElementById("ask-src-target-label");
    if (targetLabel) targetLabel.style.display = "";
    if (observerCard && observerSummary) {
      observerCard.style.display = "block";
      const parts = [];
      const roleStr = activeSessionContext.role || activeSessionContext.roleOrTopic;
      if (roleStr) parts.push(`Role: ${roleStr}`);
      if (activeSessionContext.organization) parts.push(`Organization: ${activeSessionContext.organization}`);
      const stack = activeSessionContext.techStack || activeSessionContext.keywords;
      if (stack && stack.length) {
        parts.push(`Tech Stack / Keywords: ${stack.join(", ")}`);
      }
      if (activeSessionContext.keyRequirements && activeSessionContext.keyRequirements.length) {
        parts.push(`Requirements: ${activeSessionContext.keyRequirements.join("; ")}`);
      }
      const sum = activeSessionContext.missionSummary || activeSessionContext.summary;
      if (sum) parts.push(`Summary: ${sum}`);
      if (activeSessionContext.sourceUrl) {
        parts.push(`Source: ${activeSessionContext.sourceUrl}`);
      }
      observerSummary.textContent = parts.join("\n");

      if (observerBadge) {
        if (activeSessionContext.preDraftedCoverLetter) {
          observerBadge.textContent = "✅ Tailored Cover Letter Ready (0ms Instant Fill)";
        } else {
          observerBadge.textContent = "🎯 Target Context Saved";
        }
      }
    }

    if (autofillCard && autofillRole && autofillDesc) {
      autofillCard.style.display = "block";
      const title = [
        activeSessionContext.role || activeSessionContext.roleOrTopic,
        activeSessionContext.organization
      ].filter(Boolean).join(" @ ") || "Target Opportunity";
      autofillRole.textContent = title;
      const descText = activeSessionContext.preDraftedCoverLetter
        ? "✅ 0ms Cover Letter Ready | Grounded in target job"
        : activeSessionContext.missionSummary || (activeSessionContext.keyRequirements || []).join(", ") || "";
      autofillDesc.textContent = descText;
    }
  } else {
    const targetLabel = document.getElementById("ask-src-target-label");
    if (targetLabel) targetLabel.style.display = "none";
    const targetBox = document.getElementById("ask-src-target");
    if (targetBox) targetBox.checked = false;
    if (observerCard) observerCard.style.display = "none";
    if (autofillCard) autofillCard.style.display = "none";
  }
}

// Background speculative cover letter pre-drafter ("Pre-Hit Go")
async function preDraftTailoredCoverLetter(ctx, vault) {
  const session = await getClonedSession();
  if (!session || !ctx) return;

  const badgeEl = document.getElementById("session-context-badge");
  if (badgeEl) badgeEl.textContent = "⚡ Pre-drafting tailored Cover Letter...";

  try {
    Telemetry.startTask(`Pre-drafting tailored response for ${ctx.organization || "target role"}...`, 35);

    const portfolioUrl =
      vault.find((v) => v.id === "social.portfolio" || /portfolio|website/i.test(v.key))?.value || "";
    const githubUrl =
      vault.find((v) => v.id === "social.github" || /github/i.test(v.key))?.value || "";

    const bioEntry = vault.find((v) => v.id === "custom.bio")?.value || "";
    const allFacts = vault
      .filter((v) => ["professional", "education", "social"].includes(v.category))
      .map((v) => `- ${v.key}: ${v.value}`)
      .join("\n");

    const prompt =
      `Write a concise, high-impact 2-paragraph technical application response for the candidate applying to:\n` +
      `- Target Role: ${ctx.role || ctx.roleOrTopic || "Software Engineer"}\n` +
      `- Target Organization: ${ctx.organization || "Company"}\n` +
      `- Key Requirements: ${(ctx.keyRequirements || []).join("; ")}\n` +
      `- Tech Stack / Keywords: ${(ctx.techStack || ctx.keywords || []).join(", ")}\n\n` +
      `CANDIDATE FACTS (USE ONLY THESE REAL FACTS):\n` +
      (portfolioUrl ? `- Portfolio URL: ${portfolioUrl}\n` : "") +
      (githubUrl ? `- GitHub URL: ${githubUrl}\n` : "") +
      `${bioEntry}\n${allFacts}\n\n` +
      `STRICT RULES:\n` +
      `1. Directly connect the candidate's hands-on experience and education from CANDIDATE FACTS to ${ctx.organization || "the team"}'s stack.\n` +
      (portfolioUrl ? `2. Explicitly include the portfolio link (${portfolioUrl})${githubUrl ? ` and GitHub (${githubUrl})` : ""}.\n` : "") +
      `3. Keep it under 175 words, concrete, first-person, and free of placeholders.`;

    const drafted = await runObservablePrompt(session, prompt, {
      label: `Pre-Draft (${ctx.organization || "Job"})`,
      signal: Telemetry.activeAbortController?.signal
    });
    session.destroy?.();

    if (drafted && drafted.trim().length > 40) {
      let cleanText = drafted.trim();
      // Deterministic Link Guarantee: if the vault has a portfolio URL and Nano omitted it, append a clean link line
      if (portfolioUrl && !cleanText.includes(portfolioUrl)) {
        cleanText = `${cleanText}\n\nPortfolio & Projects: ${portfolioUrl}` + (githubUrl ? ` | GitHub: ${githubUrl}` : "");
      }
      ctx.preDraftedCoverLetter = cleanText;
      await chrome.storage.local.set({ activeSessionContext: ctx });
      if (badgeEl) badgeEl.textContent = "✅ Tailored Cover Letter Ready (0ms Instant Fill)";
      renderSessionContext();
    }
  } catch (err) {
    console.warn("Background cover letter pre-draft failed:", err);
    if (badgeEl) badgeEl.textContent = "⚠️ Pre-draft unavailable";
  } finally {
    Telemetry.endTask();
  }
}

function normalizeForDedup(str) {
  return String(str || "")
    .toLowerCase()
    .replace(/^https?:\/\/(www\.)?/, "") // Strip protocol + www so URL variants dedupe together
    .replace(/[^a-z0-9]/g, "");
}

async function queueVerifiedDeltas(rawFacts, sourceTitle = "", sourceUrl = "") {
  const { userContextVault = [], pendingDeltas = [], activeSessionContext = null } = await chrome.storage.local.get([
    "userContextVault",
    "pendingDeltas",
    "activeSessionContext"
  ]);

  const queued = [];
  for (const f of rawFacts) {
    const propText = String(f.proposedValue || "").trim();
    const normProp = normalizeForDedup(propText);
    if (!normProp || normProp.length < 2) continue;

    // GUARDRAIL 1: Strict URL / Value Deduplication (ignores http/https/www/hyphens)
    const alreadyInVault = userContextVault.some((v) => {
      const normVault = normalizeForDedup(v.value);
      return normVault === normProp || (normProp.length > 10 && normVault.includes(normProp));
    });
    if (alreadyInVault) continue;

    const alreadyInQueue = pendingDeltas.some((d) => {
      const normQueue = normalizeForDedup(d.proposedValue);
      return normQueue === normProp || (normProp.length > 10 && normQueue.includes(normProp));
    });
    if (alreadyInQueue) continue;

    let key = f.key;
    let category = f.category || "custom";

    // GUARDRAIL 2: Never label a linkedin.com URL as "GitHub" or vice versa
    if (/linkedin\.com/i.test(propText) && /github/i.test(key)) {
      key = "LinkedIn Profile URL";
      category = "social";
    } else if (/github\.com/i.test(propText) && /linkedin/i.test(key)) {
      key = "GitHub Profile URL";
      category = "social";
    }

    // GUARDRAIL 3: Never allow a company-specific Cover Letter ("I am applying for ... at [Company]")
    // to overwrite canonical "Professional Bio & Summary" (custom.bio)!
    const isCompanySpecificCoverLetter =
      /i am applying for|writing to apply|role at |position at /i.test(propText) ||
      /cover letter/i.test(key);

    let action = f.action || "create";
    let targetEntryId = f.targetEntryId;

    if (isCompanySpecificCoverLetter) {
      const companyOrg = activeSessionContext?.organization || sourceTitle.split("|")[0].trim() || "Recent Application";
      key = `Cover Letter Sample (${companyOrg})`;
      category = "custom";
      action = "create";
      targetEntryId = undefined;
    }

    queued.unshift({
      deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      action,
      targetEntryId,
      category,
      key,
      previousValue: f.previousValue,
      proposedValue: propText,
      evidenceSnippet: f.evidenceSnippet || `Extracted from "${sourceTitle || "portfolio/resume"}"`,
      sourceUrl: f.sourceUrl || sourceUrl || "",
      confidence: f.confidence || 0.92
    });
  }

  let totalPending = pendingDeltas.length;
  if (queued.length > 0) {
    // The worker owns the pendingDeltas write; it merges (and re-counts)
    // under the same serialized queue the observer merge uses, so this can
    // no longer clobber a concurrently-merged delta.
    const res = await chrome.runtime.sendMessage({ type: "delta:queue", deltas: queued });
    if (res && typeof res.pendingDeltas === "number") totalPending = res.pendingDeltas;
  }

  Telemetry.logTrace({
    type: "OBSERVER",
    title: `Observer Queued ${queued.length} Fact(s)`,
    durationMs: 0,
    details: { addedCount: queued.length, totalPending }
  });

  return queued.length;
}

// Vault onboarding: prompt for the user's own chatbot, then validated import
// into the Review & Merge Queue (queueVerifiedDeltas sends delta:queue).
// The prompt lists existing field names (never values) when the vault has any
// and the checkbox is on, so the chatbot only returns what is new.
let onboardingVault = [];
function currentVaultPrompt() {
  const include = document.getElementById("vault-onboarding-include-existing").checked;
  return buildVaultPrompt({ existing: include ? existingKeysForPrompt(onboardingVault) : [] });
}

function renderVaultOnboarding(vault) {
  const card = document.getElementById("vault-onboarding-card");
  onboardingVault = vault;
  // Open only when the vault is empty; never force it closed on later renders.
  if (vault.length === 0) card.open = true;
  document.getElementById("vault-onboarding-title").textContent = vault.length === 0 ? "New here? Start from your AI chatbot" : "Add more from my chatbot";
  document.getElementById("vault-onboarding-include-row").style.display = vault.length === 0 ? "none" : "flex";
  document.getElementById("vault-onboarding-prompt").value = currentVaultPrompt();
}

document.getElementById("vault-onboarding-include-existing").addEventListener("change", () => {
  document.getElementById("vault-onboarding-prompt").value = currentVaultPrompt();
});

document.getElementById("btn-copy-vault-prompt").addEventListener("click", async () => {
  const btn = document.getElementById("btn-copy-vault-prompt");
  try {
    await navigator.clipboard.writeText(currentVaultPrompt());
    btn.textContent = "Copied!";
  } catch {
    btn.textContent = "Copy failed. Select the text and copy it.";
  }
  setTimeout(() => (btn.textContent = "Copy prompt"), 1500);
});

document.getElementById("vault-onboarding-file").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  document.getElementById("vault-onboarding-input").value = await file.text();
});

document.getElementById("btn-vault-onboarding-import").addEventListener("click", async () => {
  const status = document.getElementById("vault-onboarding-status");
  const result = parseVaultImport(document.getElementById("vault-onboarding-input").value);
  if (!result.ok) {
    status.style.color = "#f87171";
    status.textContent = `Nothing was queued. Fix these and try again:\n${result.errors.slice(0, 10).join("\n")}`;
    return;
  }
  const { pendingDeltas = [] } = await chrome.storage.local.get(["pendingDeltas"]);
  if (pendingDeltas.length + result.facts.length > MAX_PENDING_DELTAS) {
    status.style.color = "#f87171";
    status.textContent = `The review queue holds ${pendingDeltas.length} of ${MAX_PENDING_DELTAS} facts. Review or clear it in the Observer tab, then try again.`;
    return;
  }
  const queued = await queueVerifiedDeltas(result.facts, "Pasted from my AI chatbot");
  await renderObserverQueue();
  status.style.color = "#34d399";
  status.textContent = `Queued ${queued} of ${result.facts.length} facts (duplicates skipped). Open the Observer tab > Review & Merge Queue to approve.`;
});

// Clear Active Target Context Button
document.getElementById("btn-clear-target-context")?.addEventListener("click", async () => {
  await chrome.storage.local.remove(["activeSessionContext"]);
  await renderSessionContext();
});

// Action 1: Capture Target / Job Context (Session Memory, does NOT pollute Vault)
document.getElementById("btn-capture-target-context")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-capture-target-context");
  const originalText = btn.textContent;
  btn.textContent = "Analyzing target page...";
  btn.disabled = true;

  try {
    const tab = await getActiveTab();
    if (!tab?.id) throw new Error("No active web page found.");

    const snapshot = await sendToActiveTab({
      action: "CAPTURE_PAGE_SNAPSHOT"
    });

    if (!snapshot) throw new Error("Could not capture page content.");

    let targetContext = {
      role: "",
      organization: "",
      keyRequirements: [],
      techStack: [],
      missionSummary: "",
      sourceTitle: snapshot.title || "",
      sourceUrl: snapshot.url || "",
      capturedAt: new Date().toISOString()
    };

    // Use Gemini Nano if available to extract structured target context
    if (snapshot.extractedText && ("LanguageModel" in self || self.ai?.languageModel)) {
      try {
        const session = await createPromptSession();
        const promptText = getPrompt("target_context", {
          guidance: promptGuidance,
          vars: {
            title: snapshot.title || "",
            url: snapshot.url || "",
            text: snapshot.extractedText.slice(0, 3500)
          }
        });
        const aiResponse = await session.prompt(promptText);
        session.destroy();

        const match = aiResponse.match(/\{[\s\S]*\}/);
        if (match) {
          const parsed = JSON.parse(match[0]);
          if (parsed.role) targetContext.role = parsed.role;
          if (parsed.organization) targetContext.organization = parsed.organization;
          if (Array.isArray(parsed.keyRequirements)) targetContext.keyRequirements = parsed.keyRequirements;
          if (Array.isArray(parsed.techStack)) targetContext.techStack = parsed.techStack;
          if (parsed.missionSummary) targetContext.missionSummary = parsed.missionSummary;
        }
      } catch (aiErr) {
        console.warn("AI target extraction failed, falling back to heuristics:", aiErr);
      }
    }

    // Heuristic fallbacks if AI was unavailable or missed role/org
    if (!targetContext.role || !targetContext.organization) {
      const titleParts = (snapshot.title || "").split(/[-|–•]/).map((s) => s.trim());
      if (titleParts.length >= 2) {
        if (!targetContext.role) targetContext.role = titleParts[0];
        if (!targetContext.organization) targetContext.organization = titleParts[1];
      } else if (!targetContext.role) {
        targetContext.role = snapshot.title || "Target Opportunity";
      }
    }
    if (!targetContext.missionSummary) {
      targetContext.missionSummary = (snapshot.extractedText || "").slice(0, 300).trim();
    }

    await chrome.storage.local.set({ activeSessionContext: targetContext });
    await renderSessionContext();
    btn.textContent = "🎯 Context Captured!";

    // Speculative Pre-Computation: kick off background pre-draft of tailored cover letter
    const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
    preDraftTailoredCoverLetter(targetContext, userContextVault).catch(() => {});

    setTimeout(() => {
      btn.textContent = originalText;
      btn.disabled = false;
    }, 1500);
  } catch (err) {
    alert("Capture Target Context failed: " + err.message);
    btn.textContent = originalText;
    btn.disabled = false;
  }
});

// Profile learning pipeline: chunk the text, one fresh session per chunk
// (token budget), constrained JSON output, then the Review & Merge Queue.
async function learnFactsFromText(text, title, url, { onProgress, maxChunks = MAX_CHUNKS_PER_PAGE } = {}) {
  const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
  const existingKeys = userContextVault.map((v) => v.key).filter(Boolean);
  const signal = Telemetry.activeAbortController?.signal;
  const t0 = performance.now();
  const { facts, chunkCount, skippedChunks } = await extractFactsFromText(
    text,
    async (prompt) => {
      const session = await createPromptSession();
      try {
        return await runObservablePrompt(session, prompt, {
          label: "Extract Profile Facts",
          signal,
          responseConstraint: PROFILE_FACTS_SCHEMA
        });
      } finally {
        session.destroy();
      }
    },
    { existingKeys, guidance: promptGuidance, signal, onProgress, maxChunks }
  );
  Telemetry.logTrace({
    type: "SCAN",
    title: "Profile Fact Extraction",
    durationMs: Math.round(performance.now() - t0),
    details: { url, chunks: chunkCount, skippedChunks, facts: facts.length }
  });
  await queueVerifiedDeltas(facts, title, url);
  return { count: facts.length, chunks: chunkCount, skipped: skippedChunks };
}

// --- Learn from my sources ---------------------------------------------------
async function getProfileSources() {
  const { profileSources = [] } = await chrome.storage.local.get(["profileSources"]);
  return profileSources;
}

async function renderProfileSources() {
  const list = document.getElementById("source-list");
  if (!list) return;
  const sources = await getProfileSources();
  list.textContent = "";
  for (const url of sources) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex; justify-content:space-between; gap:6px; margin-bottom:3px;";
    const label = document.createElement("span");
    label.textContent = url;
    label.style.cssText = "overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
    const rm = document.createElement("button");
    rm.className = "btn btn-danger";
    rm.textContent = "Remove";
    rm.style.cssText = "width:auto; padding:1px 8px; font-size:10px; margin:0;";
    rm.addEventListener("click", async () => {
      await chrome.storage.local.set({ profileSources: (await getProfileSources()).filter((u) => u !== url) });
      renderProfileSources();
    });
    row.append(label, rm);
    list.append(row);
  }
}

document.getElementById("btn-add-source")?.addEventListener("click", async () => {
  const input = document.getElementById("source-url-input");
  const progress = document.getElementById("source-progress");
  let url;
  try {
    url = new URL(input.value.trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("scheme");
  } catch {
    progress.textContent = "Enter a full http(s) URL.";
    return;
  }
  const sources = await getProfileSources();
  if (!sources.includes(url.href)) await chrome.storage.local.set({ profileSources: [...sources, url.href] });
  input.value = "";
  progress.textContent = "";
  renderProfileSources();
});

document.getElementById("source-file-input")?.addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  const progress = document.getElementById("source-progress");
  if (!file) return;
  if (/\.pdf$/i.test(file.name)) {
    progress.textContent = "PDF files are not supported. Open the PDF, copy its text and paste it into the text box.";
    e.target.value = "";
    return;
  }
  const raw = await file.text();
  const text = /\.html?$/i.test(file.name) ? htmlToText(raw) : raw;
  const box = document.getElementById("source-pasted-text");
  box.value = box.value ? `${box.value}\n\n${text}` : text;
  progress.textContent = `Loaded ${file.name} (${text.length} characters).`;
  e.target.value = "";
});

// Google Drive import: pick files, fetch their text through the worker and
// append it to the text box, so it runs through the same chunk -> extract ->
// Review & Merge Queue path as pasted text. Read-only; no vault writes.
let drivePageToken = "";
const driveButton = document.getElementById("btn-drive-import");

async function initDriveImport() {
  if (!driveButton) return;
  let configured = false;
  try {
    configured = Boolean((await chrome.runtime.sendMessage({ type: "drive:status" }))?.configured);
  } catch {
    configured = false;
  }
  driveButton.disabled = !configured;
  document.getElementById("drive-hint").textContent = configured
    ? "Read-only. Pick Google Docs, .txt, .md, .html or .json files (500KB max each)."
    : "Not configured: set oauth2.client_id in manifest.json (see README).";
}

async function loadDriveFiles({ append = false } = {}) {
  const progress = document.getElementById("source-progress");
  const list = document.getElementById("drive-file-list");
  progress.textContent = "Loading Drive files...";
  const res = await chrome.runtime.sendMessage({
    type: "drive:list",
    search: document.getElementById("drive-search").value,
    pageToken: append ? drivePageToken : undefined
  });
  if (!res || res.error) {
    progress.textContent = `Drive: ${res?.error || "No response from service worker"}`;
    return;
  }
  if (!append) list.textContent = "";
  for (const f of res.files) {
    const row = document.createElement("label");
    row.style.cssText = "display:flex; gap:6px; align-items:center; margin-bottom:3px; cursor:pointer;";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.fileId = f.id;
    box.style.width = "auto";
    const name = document.createElement("span");
    name.textContent = f.name;
    name.style.cssText = "overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
    row.append(box, name);
    list.append(row);
  }
  if (!list.children.length) list.textContent = "No supported files found.";
  drivePageToken = res.nextPageToken || "";
  document.getElementById("btn-drive-more").style.display = drivePageToken ? "" : "none";
  progress.textContent = "";
}

driveButton?.addEventListener("click", async () => {
  document.getElementById("drive-picker").style.display = "block";
  await loadDriveFiles();
});
document.getElementById("btn-drive-search")?.addEventListener("click", () => loadDriveFiles());
document.getElementById("btn-drive-more")?.addEventListener("click", () => loadDriveFiles({ append: true }));
document.getElementById("btn-drive-add")?.addEventListener("click", async () => {
  const progress = document.getElementById("source-progress");
  const ids = [...document.querySelectorAll("#drive-file-list input[type=checkbox]:checked")].map((b) => b.dataset.fileId);
  if (!ids.length) {
    progress.textContent = "Tick at least one Drive file.";
    return;
  }
  const failures = [];
  const texts = [];
  for (const id of ids.slice(0, MAX_DRIVE_FILES_PER_RUN)) {
    progress.textContent = `Fetching Drive file ${texts.length + failures.length + 1}/${Math.min(ids.length, MAX_DRIVE_FILES_PER_RUN)}...`;
    const res = await chrome.runtime.sendMessage({ type: "drive:fetch", fileId: id });
    if (!res || res.error) failures.push(res?.error || "No response from service worker");
    else texts.push(`${res.title}\n${res.text}`);
  }
  if (texts.length) {
    const box = document.getElementById("source-pasted-text");
    box.value = [box.value, ...texts].filter(Boolean).join("\n\n");
  }
  progress.textContent =
    `Added ${texts.length} Drive file(s) to the text box; press Learn from all.` +
    (ids.length > MAX_DRIVE_FILES_PER_RUN ? ` Only the first ${MAX_DRIVE_FILES_PER_RUN} were fetched.` : "") +
    (failures.length ? ` Failed: ${failures.join("; ")}` : "");
});
initDriveImport();

async function fetchSourcePage(url) {
  const res = await chrome.runtime.sendMessage({ type: "source:fetch", url });
  if (!res || res.error) throw new Error(res?.error || "No response from service worker");
  return res;
}

document.getElementById("btn-learn-sources")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-learn-sources");
  const progress = document.getElementById("source-progress");
  if (!("LanguageModel" in self || self.ai?.languageModel)) {
    progress.textContent = "On-device AI is not available.";
    return;
  }
  btn.disabled = true;
  let total = 0;
  const failures = [];
  try {
    const pages = [];
    const pasted = document.getElementById("source-pasted-text").value.trim();
    if (pasted) pages.push({ title: "Pasted resume/bio", url: "", text: pasted });
    const follow = document.getElementById("chk-follow-links")?.checked;
    const fetched = await collectSourcePages(
      await getProfileSources(),
      (url) => {
        progress.textContent = `Fetching ${url}...`;
        return fetchSourcePage(url);
      },
      { follow }
    );
    pages.push(...fetched.pages);
    failures.push(...fetched.failures);
    let chunksLeft = MAX_CHUNKS_PER_RUN;
    let skipped = 0;
    for (let i = 0; i < pages.length; i++) {
      const p = pages[i];
      const r = await learnFactsFromText(p.text, p.title, p.url, {
        maxChunks: Math.min(MAX_CHUNKS_PER_PAGE, chunksLeft),
        onProgress: (n, of) => {
          progress.textContent = `Source ${i + 1}/${pages.length}: chunk ${n}/${of} (run cap ${MAX_CHUNKS_PER_RUN} chunks)`;
        }
      });
      total += r.count;
      skipped += r.skipped;
      chunksLeft -= r.chunks;
    }
    renderObserverQueue();
    progress.textContent =
      `Queued ${total} candidate facts from ${pages.length} source(s) for review.` +
      (skipped ? ` Skipped ${skipped} chunk(s) over the cap (${MAX_CHUNKS_PER_PAGE} per page, ${MAX_CHUNKS_PER_RUN} per run).` : "") +
      (failures.length ? ` Failed: ${failures.join("; ")}` : "");
  } catch (err) {
    progress.textContent = `Learning stopped: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});

// Action 2: Extract My Profile Facts (Permanent User Identity -> Review & Merge Queue)
document.getElementById("btn-snapshot-user-facts")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-snapshot-user-facts");
  const originalText = btn.textContent;
  btn.textContent = "Learning from this page...";
  btn.disabled = true;

  try {
    const tab = await getActiveTab();
    if (!tab?.id) throw new Error("No active web page.");

    const snapshot = await sendToActiveTab({
      action: "CAPTURE_PAGE_SNAPSHOT"
    });

    if (!snapshot) throw new Error("Could not capture page snapshot.");

    Telemetry.logTrace({
      type: "SCAN",
      title: "Capture Profile Page Snapshot",
      durationMs: 0,
      details: { url: snapshot.url, length: snapshot.extractedText?.length || 0 }
    });

    // Extract user profile facts via on-device LanguageModel strictly for personal portfolio/resume
    if (snapshot.extractedText && ("LanguageModel" in self || self.ai?.languageModel)) {
      try {
        await learnFactsFromText(snapshot.extractedText, snapshot.title || "portfolio/resume", snapshot.url || "");
      } catch (aiErr) {
        console.warn("LanguageModel snapshot extraction error:", aiErr);
      }
    }

    // Pass snapshot to service worker observation handler for any filled inputs
    await chrome.runtime.sendMessage({
      action: "FORM_SUBMIT_DETECTED",
      payload: snapshot
    });

    renderObserverQueue();
    btn.textContent = "👤 Learned!";
    setTimeout(() => {
      btn.textContent = originalText;
      btn.disabled = false;
    }, 1500);
  } catch (err) {
    alert("Profile extraction failed: " + err.message);
    btn.textContent = originalText;
    btn.disabled = false;
  }
});

// ----------------------------------------------------------------------------
// INITIALIZATION
// ----------------------------------------------------------------------------
async function init() {
  // Vault sanitization moved to the worker (startup + on-write); the panel is
  // read-only for vault/delta state.
  applyModelView(null);
  checkBuiltInAIStatus();
  renderVault();
  renderObserverQueue();
  renderSessionContext();
  renderProfileSources();
  renderObserverDomainToggle();
}
init();

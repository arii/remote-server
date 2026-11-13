// ============================================================================
// LocalForm AI - Side Panel Controller (Manifest V3)
// Implements Phases 1 through 7 for Chrome's Built-in On-Device AI
// ============================================================================

// Global state cache & pre-warmed AI pool
let currentExportDoc = null;
let baseWarmSession = null;

async function getClonedSession() {
  const lmGlobal = self.LanguageModel || self.ai?.languageModel;
  if (!lmGlobal) return null;
  try {
    if (!baseWarmSession) {
      baseWarmSession = await lmGlobal.create({
        outputLanguage: "en",
        systemPrompt:
          "You are an expert career application assistant. Write tailored, concrete, first-person responses grounded ONLY in the candidate facts supplied in each prompt. Never invent employers, schools, or links."
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

// Strict alias matching for vault entries (audit high #4). Short aliases must
// match exactly; longer aliases may match as whole words inside the query.
// The old bidirectional substring test (q.includes(a) || a.includes(q)) made a
// two-letter-overlap alias like "name" match "Company name".
function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, (ch) => "\\" + ch);
}

const aliasRegexCache = new Map(); // compiled word-boundary matchers per normalized alias

function aliasMatches(query, alias) {
  const q = String(query || "").toLowerCase().replace(/\s+/g, " ").trim();
  const a = String(alias || "").toLowerCase().replace(/\s+/g, " ").trim();
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
    const entry = {
      id: Date.now() + Math.random(),
      ts: new Date().toLocaleTimeString(),
      type, // 'SCAN' | 'HYDRATE' | 'LLM_PROMPT' | 'OBSERVER' | 'ERROR'
      title,
      durationMs: Math.round(durationMs || 0),
      details: typeof details === "string" ? details : JSON.stringify(details, null, 2)
    };
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
      title: `${label} Failed: ${err.message}`,
      durationMs: performance.now() - t0,
      details: err.stack || err.message
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
    }
  });
});

// ----------------------------------------------------------------------------
// PHASE 1: BUILT-IN AI DETECTION & BOOTSTRAP
// ----------------------------------------------------------------------------
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

  document.getElementById("model-details-box").textContent = JSON.stringify(report, null, 2);
  return report;
}

async function createPromptSession(onProgress, options = {}) {
  const monitor = (m) => {
    m.addEventListener("downloadprogress", (e) => {
      const pct = Math.round((e.loaded || 0) * 100);
      if (onProgress) onProgress(pct);
    });
  };

  const sessionOptions = {
    monitor,
    ...options,
    outputLanguage: options.outputLanguage || "en"
  };

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

// Model Bootstrap Button
document.getElementById("btn-bootstrap-model").addEventListener("click", async () => {
  const box = document.getElementById("model-details-box");
  const progressContainer = document.getElementById("download-progress-container");
  const progressFill = document.getElementById("download-progress-fill");
  const progressText = document.getElementById("download-progress-text");

  progressContainer.style.display = "block";
  progressFill.style.width = "5%";
  progressText.textContent = "5%";
  box.textContent = "Requesting Gemini Nano model download...";

  try {
    const session = await createPromptSession((pct) => {
      progressFill.style.width = `${pct}%`;
      progressText.textContent = `${pct}%`;
      box.textContent = `Downloading Gemini Nano model weights: ${pct}%...`;
    });

    const ping = await session.prompt("Respond with READY.");
    progressFill.style.width = "100%";
    progressText.textContent = "100%";
    box.textContent = `Model Ready! Diagnostic test response:\n${ping}`;
    session.destroy();
    await checkBuiltInAIStatus();
  } catch (err) {
    box.textContent = `Model initialization error: ${err.message}\n\nMake sure chrome://flags/#optimization-guide-on-device-model and chrome://flags/#prompt-api-for-gemini-nano are enabled.`;
  }
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
    const result = await session.prompt(input);
    const elapsed = Math.round(performance.now() - startTime);
    box.textContent = `[${elapsed}ms local inference]\n\n${result}`;
    session.destroy();
  } catch (err) {
    box.textContent = `Prompt Error: ${err.message}`;
  }
});

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
    preview.textContent = `Scan failed: ${err.message}\nMake sure you are on an http/https web page and refresh the page if the extension was just installed.`;
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
    preview.textContent = "UX Audit Error: " + err.message;
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
        let matchedVaultEssay = null;
        for (const entry of userContextVault) {
          const aliases = [entry.key, ...(entry.aliases || [])];
          if (aliases.some((a) => aliasMatches(q, a))) {
            matchedVaultEssay = entry.value;
            break;
          }
        }
        if (matchedVaultEssay) {
          field.value = matchedVaultEssay;
          field.confidence = 0.98;
          continue;
        }

        // If we already pre-drafted a tailored cover letter for activeSessionContext, use it in 0ms!
        if (activeSessionContext?.preDraftedCoverLetter && /cover letter|note|motivation|why/i.test(q)) {
          field.value = activeSessionContext.preDraftedCoverLetter;
          field.confidence = 0.99;
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
      const autoAttr = field.constraints?.autocomplete;
      for (const entry of userContextVault) {
        if (autoAttr && entry.autocomplete && entry.autocomplete === autoAttr) {
          field.value = entry.value;
          field.confidence = 0.99;
          break;
        }
        const aliases = [entry.key, ...(entry.aliases || [])];
        if (aliases.some((a) => aliasMatches(q, a))) {
          if (field.inputType === "checkbox") {
            field.value = /^(yes|true|1)$/i.test(String(entry.value).trim());
          } else {
            field.value = entry.value;
          }
          field.confidence = 0.98;
          break;
        }
      }
    }

    // 3. Immediately hydrate Stage 1 fields on the live DOM so user sees instant results (<15ms)!
    const stage1T0 = performance.now();
    const stage1Result = await sendToActiveTab({
      action: "HYDRATE_PAGE_FORMS",
      document: doc,
      options: { highlightConfidence: true }
    });
    Telemetry.logTrace({
      type: "HYDRATE",
      title: "Hydrate Stage 1 Atomic Fields",
      durationMs: performance.now() - stage1T0,
      details: { filledCount: stage1Result.filledCount, failedCount: stage1Result.failedFields?.length || 0 }
    });
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

          const prompt =
            `Draft a tailored, first-person response for the form field: "${field.query}".\n` +
            `TARGET CONTEXT:\n` +
            `- Role: ${ctx.role || ctx.roleOrTopic || document.title}\n` +
            `- Company: ${ctx.organization || "this organization"}\n` +
            `- Requirements: ${(ctx.keyRequirements || []).join("; ")}\n` +
            `- Tech Stack: ${(ctx.techStack || ctx.keywords || []).join(", ")}\n\n` +
            `CANDIDATE FACTS (from your local vault):\n${bioEntry}\n${expEntries}\n` +
            (portfolioUrl ? `- Portfolio URL: ${portfolioUrl}\n` : "") +
            (githubUrl ? `- GitHub URL: ${githubUrl}\n` : "") +
            `\nINSTRUCTIONS:\n` +
            `- DO NOT repeat the candidate bio word-for-word.\n` +
            `- Explicitly connect the candidate's experience from CANDIDATE FACTS to ${ctx.organization || "the role"}'s specific tech stack and domain.\n` +
            `- Output ONLY the final response text (no markdown headers, no placeholders, max 175 words).`;

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
          }
        }
        session.destroy?.();

        // Hydrate the newly synthesized essay fields
        Telemetry.updateStage("[4/4] Hydrating Final Form on Live DOM...", 90);
        const stage2T0 = performance.now();
        const finalResult = await sendToActiveTab({
          action: "HYDRATE_PAGE_FORMS",
          document: doc,
          options: { highlightConfidence: true }
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
    statusBox.textContent = `Auto-Fill Error: ${err.message}`;
    Telemetry.logTrace({
      type: "ERROR",
      title: "Auto-Fill Error",
      durationMs: 0,
      details: err.message
    });
  } finally {
    Telemetry.endTask();
    btn.disabled = false;
    btn.textContent = "✨ Auto-Fill Active Form with AI";
  }
});

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
function deduplicateVault(entries) {
  if (!Array.isArray(entries)) return [];
  // Sort so canonical IDs (like "identity.full_name" without random timestamps) come FIRST
  const sorted = [...entries].sort((a, b) => {
    const aIsTimestamp = /\.\d{10,}$/.test(a.id || "");
    const bIsTimestamp = /\.\d{10,}$/.test(b.id || "");
    return Number(aIsTimestamp) - Number(bIsTimestamp);
  });

  const cleaned = [];
  for (const item of sorted) {
    const normKey = String(item.key || "").toLowerCase().trim();
    const normVal = String(item.value || "").toLowerCase().replace(/\s+/g, " ").trim();
    if (!normVal) continue;

    // If another entry already has the exact same normalized value and overlapping key/category, merge aliases instead of duplicating
    const existing = cleaned.find((c) => {
      const cVal = String(c.value || "").toLowerCase().replace(/\s+/g, " ").trim();
      const cKey = String(c.key || "").toLowerCase().trim();
      return cVal === normVal && (cKey === normKey || c.category === item.category || item.tags?.includes("observed"));
    });

    if (existing) {
      const mergedAliases = new Set([...(existing.aliases || []), ...(item.aliases || []), normKey]);
      existing.aliases = Array.from(mergedAliases);
      continue;
    }
    cleaned.push(item);
  }
  return cleaned;
}

async function renderVault() {
  const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
  const cleanedVault = deduplicateVault(userContextVault);
  if (cleanedVault.length !== userContextVault.length) {
    await chrome.storage.local.set({ userContextVault: cleanedVault });
  }

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

  // First-run flow: the vault ships empty (no seed PII). Point the user at the
  // import path — the file format is exactly what "Export Vault JSON" produces.
  if (cleanedVault.length === 0) {
    container.innerHTML = `
      <div class="item-card" style="text-align:center; padding:16px;">
        <div style="font-weight:600; color:#e2e8f0; margin-bottom:6px;">Your vault is empty</div>
        <div style="color:var(--text-muted); font-size:11px; margin-bottom:10px;">
          Import a previously exported Vault JSON file to restore your profile locally, or add facts manually below.
          Nothing is bundled with the extension and nothing leaves this browser.
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
      const id = btn.dataset.id;
      const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
      const updated = userContextVault.filter((e) => e.id !== id);
      await chrome.storage.local.set({ userContextVault: updated });
      renderVault();
    });
  });

  // Attach inline edit listeners
  container.querySelectorAll(".vault-value-input").forEach((inp) => {
    inp.addEventListener("change", async () => {
      const id = inp.dataset.id;
      const val = inp.value;
      const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
      const target = userContextVault.find((e) => e.id === id);
      if (target) {
        target.value = val;
        target.updatedAt = new Date().toISOString();
        await chrome.storage.local.set({ userContextVault });
      }
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

  const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
  userContextVault.unshift({
    id: `${category}.${Date.now()}`,
    category,
    key,
    aliases: [key.toLowerCase()],
    value,
    tags: [category, "manual"],
    updatedAt: new Date().toISOString()
  });

  await chrome.storage.local.set({ userContextVault });
  keyInput.value = "";
  valInput.value = "";
  renderVault();
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
    const cleaned = deduplicateVault(imported);
    await chrome.storage.local.set({ userContextVault: cleaned });
    renderVault();
    alert("Vault successfully restored!");
  } catch (err) {
    alert("Failed to import vault JSON: " + err.message);
  }
});

// ----------------------------------------------------------------------------
// PHASE 7: OBSERVER & DIFF APPROVAL QUEUE
// ----------------------------------------------------------------------------
async function renderObserverQueue() {
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

async function approveDelta(deltaId) {
  const { userContextVault = [], pendingDeltas = [] } = await chrome.storage.local.get([
    "userContextVault",
    "pendingDeltas"
  ]);

  const targetDelta = pendingDeltas.find((d) => d.deltaId === deltaId);
  if (!targetDelta) return;

  if (targetDelta.action === "update" && targetDelta.targetEntryId) {
    const existing = userContextVault.find((e) => e.id === targetDelta.targetEntryId);
    if (existing) {
      existing.value = targetDelta.proposedValue;
      existing.updatedAt = new Date().toISOString();
    }
  } else {
    userContextVault.unshift({
      id: `${targetDelta.category || "custom"}.${Date.now()}`,
      category: targetDelta.category || "custom",
      key: targetDelta.key,
      aliases: [targetDelta.key.toLowerCase()],
      value: targetDelta.proposedValue,
      tags: ["observed", targetDelta.category || "custom"],
      updatedAt: new Date().toISOString()
    });
  }

  const remaining = pendingDeltas.filter((d) => d.deltaId !== deltaId);
  const dedupedVault = deduplicateVault(userContextVault);
  await chrome.storage.local.set({ userContextVault: dedupedVault, pendingDeltas: remaining });
  chrome.runtime.sendMessage({ action: "UPDATE_BADGE_COUNT", count: remaining.length });
  renderVault();
  renderObserverQueue();
}

async function discardDelta(deltaId) {
  const { pendingDeltas = [] } = await chrome.storage.local.get(["pendingDeltas"]);
  const remaining = pendingDeltas.filter((d) => d.deltaId !== deltaId);
  await chrome.storage.local.set({ pendingDeltas: remaining });
  chrome.runtime.sendMessage({ action: "UPDATE_BADGE_COUNT", count: remaining.length });
  renderObserverQueue();
}

document.getElementById("btn-approve-all-deltas").addEventListener("click", async () => {
  const { userContextVault = [], pendingDeltas = [] } = await chrome.storage.local.get([
    "userContextVault",
    "pendingDeltas"
  ]);

  for (const delta of pendingDeltas) {
    if (delta.action === "update" && delta.targetEntryId) {
      const existing = userContextVault.find((e) => e.id === delta.targetEntryId);
      if (existing) existing.value = delta.proposedValue;
    } else {
      userContextVault.unshift({
        id: `${delta.category || "custom"}.${Date.now()}`,
        category: delta.category || "custom",
        key: delta.key,
        aliases: [delta.key.toLowerCase()],
        value: delta.proposedValue,
        tags: ["observed"],
        updatedAt: new Date().toISOString()
      });
    }
  }

  const dedupedVault = deduplicateVault(userContextVault);
  await chrome.storage.local.set({ userContextVault: dedupedVault, pendingDeltas: [] });
  chrome.runtime.sendMessage({ action: "UPDATE_BADGE_COUNT", count: 0 });
  renderVault();
  renderObserverQueue();
});

document.getElementById("btn-discard-all-deltas").addEventListener("click", async () => {
  await chrome.storage.local.set({ pendingDeltas: [] });
  chrome.runtime.sendMessage({ action: "UPDATE_BADGE_COUNT", count: 0 });
  renderObserverQueue();
});

// ----------------------------------------------------------------------------
// OBSERVER OPT-IN (PER-DOMAIN ALLOWLIST)
// Passive submit observation is off for every site until the user enables it
// for that domain here; the content script checks the same allowlist at
// submit time and fails closed.
// ----------------------------------------------------------------------------
async function renderObserverDomainToggle() {
  const chk = document.getElementById("chk-observer-domain");
  const domainEl = document.getElementById("observer-current-domain");
  if (!chk || !domainEl) return;

  const tab = await getActiveTab();
  let hostname = "";
  try {
    hostname = tab?.url ? new URL(tab.url).hostname : "";
  } catch {
    hostname = ""; // chrome:// and other non-standard URLs have no hostname
  }

  if (!hostname) {
    domainEl.textContent = "this site";
    chk.checked = false;
    chk.disabled = true;
    return;
  }

  domainEl.textContent = hostname;
  chk.disabled = false;
  const { observerDomainAllowlist = [] } = await chrome.storage.local.get(["observerDomainAllowlist"]);
  chk.checked = Array.isArray(observerDomainAllowlist) && observerDomainAllowlist.includes(hostname);
}

document.getElementById("chk-observer-domain")?.addEventListener("change", async (e) => {
  const chk = e.target;
  let hostname = "";
  try {
    const tab = await getActiveTab();
    hostname = tab?.url ? new URL(tab.url).hostname : "";
  } catch {
    hostname = "";
  }
  if (!hostname) {
    chk.checked = false;
    return;
  }

  const { observerDomainAllowlist = [] } = await chrome.storage.local.get(["observerDomainAllowlist"]);
  const updated = new Set(Array.isArray(observerDomainAllowlist) ? observerDomainAllowlist : []);
  if (chk.checked) updated.add(hostname);
  else updated.delete(hostname);
  await chrome.storage.local.set({ observerDomainAllowlist: Array.from(updated) });

  Telemetry.logTrace({
    type: "OBSERVER",
    title: `Submit observer ${chk.checked ? "enabled" : "disabled"} for ${hostname}`,
    durationMs: 0,
    details: { hostname, enabled: chk.checked }
  });
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

  let addedCount = 0;
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

    pendingDeltas.unshift({
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
    addedCount++;
  }

  if (addedCount > 0) {
    await chrome.storage.local.set({ pendingDeltas });
    chrome.runtime.sendMessage({ action: "UPDATE_BADGE_COUNT", count: pendingDeltas.length });
  }

  Telemetry.logTrace({
    type: "OBSERVER",
    title: `Observer Queued ${addedCount} Fact(s)`,
    durationMs: 0,
    details: { addedCount, totalPending: pendingDeltas.length }
  });

  return addedCount;
}

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
        const promptText = `Analyze this job posting / target page and extract the target context.
Page Title: "${snapshot.title || ""}"
URL: "${snapshot.url || ""}"
Text Content:
"${snapshot.extractedText.slice(0, 3500)}"

Respond ONLY with a JSON object in this format:
{
  "role": "Title of the job, grant, or opportunity (e.g. Senior Software Engineer)",
  "organization": "Company or organization name (e.g. Acme Robotics)",
  "keyRequirements": ["requirement 1", "requirement 2"],
  "techStack": ["tech 1", "tech 2"],
  "missionSummary": "1-2 sentence overview of what the company/team does and what they need."
}`;
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

// Action 2: Extract My Profile Facts (Permanent User Identity -> Review & Merge Queue)
document.getElementById("btn-snapshot-user-facts")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-snapshot-user-facts");
  const originalText = btn.textContent;
  btn.textContent = "Extracting my profile facts...";
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
        const session = await createPromptSession();
        const promptText = `Extract 1-3 personal profile facts about the user/author (such as full name, email, phone number, location, GitHub URL, portfolio, or current employer) from this portfolio or resume text. Text: "${snapshot.extractedText.slice(0, 2000)}". Respond ONLY with a JSON array of objects with "key", "proposedValue", "category".`;
        const aiResponse = await runObservablePrompt(session, promptText, {
          label: "Extract Profile Facts",
          signal: Telemetry.activeAbortController?.signal
        });
        session.destroy();

        try {
          const match = aiResponse.match(/\[[\s\S]*\]/);
          if (match) {
            const parsedFacts = JSON.parse(match[0]);
            await queueVerifiedDeltas(parsedFacts, snapshot.title || "portfolio/resume", snapshot.url || "");
          }
        } catch (parseErr) {
          console.warn("AI fact parse notice:", parseErr);
        }
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
    btn.textContent = "👤 Extracted!";
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
  try {
    const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
    if (userContextVault.length > 0) {
      const cleaned = deduplicateVault(userContextVault);
      if (cleaned.length !== userContextVault.length) {
        await chrome.storage.local.set({ userContextVault: cleaned });
      }
    }
  } catch (err) {
    console.warn("Vault initialization sanitization warning:", err);
  }
  checkBuiltInAIStatus();
  renderVault();
  renderObserverQueue();
  renderSessionContext();
  renderObserverDomainToggle();
}
init();

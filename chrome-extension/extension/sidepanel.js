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
          "You are an expert technical career assistant for Dr. Ariel Anders (MIT PhD Roboticist, former Waymo Motion Planning Senior SWE, Robust.AI first roboticist, Civ Robotics Senior Algorithms Engineer). Always write tailored, concrete, first-person responses."
      });
    }
    if (typeof baseWarmSession.clone === "function") {
      return await baseWarmSession.clone();
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
    outputLanguage: "en",
    monitor,
    ...options
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

  try {
    const tab = await getActiveTab();
    if (!tab?.id) throw new Error("No active web page found.");

    // 1. Scan current form (reads fresh or pre-scanned cache)
    const doc = await sendToActiveTab({
      action: "SCAN_PAGE_FORMS",
      options: { includePageContext: true, includeCurrentValues: true }
    });

    if (!doc?.fields || doc.fields.length === 0) {
      statusBox.textContent = "No fillable interactive form inputs found on this page.";
      return;
    }

    const { userContextVault = [], activeSessionContext = null } = await chrome.storage.local.get([
      "userContextVault",
      "activeSessionContext"
    ]);

    const autoConsent = document.getElementById("chk-agree-consent")?.checked ?? true;
    const longFormFields = [];

    // 2. Stage 1: Deterministic fast match for atomic fields + pre-drafted Cover Letter check
    for (const field of doc.fields) {
      if (field.value && field.value !== "" && field.value !== false) continue;

      const q = (field.query || "").toLowerCase().trim();
      const isCoverLetterOrEssay =
        field.inputType === "textarea" ||
        /cover letter|why do you|why are you|motivation|note to hiring|tell us about|additional info|why openmind|why us/i.test(q);

      if (isCoverLetterOrEssay) {
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
        const aliases = [entry.key.toLowerCase(), ...(entry.aliases || []).map((a) => a.toLowerCase())];
        if (aliases.some((a) => q === a || q.includes(a) || a.includes(q))) {
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
    const stage1Result = await sendToActiveTab({
      action: "HYDRATE_PAGE_FORMS",
      document: doc,
      options: { highlightConfidence: true }
    });
    statusBox.textContent = `Stage 1 filled ${stage1Result.filledCount} fields instantly...`;

    // 4. Stage 2: On-Device LLM synthesis ONLY for remaining tailored essays or unmapped fields
    if (longFormFields.length > 0) {
      btn.textContent = `🧠 Stage 2: Tailoring ${longFormFields.length} Essay/Cover Letter Field(s)...`;
      const session = await getClonedSession();

      if (session) {
        for (const field of longFormFields) {
          const ctx = activeSessionContext || doc.pageContext || {};
          const bioEntry = userContextVault.find((v) => v.id === "custom.bio")?.value || "";
          const expEntries = userContextVault
            .filter((v) => ["professional", "education"].includes(v.category))
            .map((v) => `${v.key}: ${v.value}`)
            .join("\n");

          const prompt =
            `Draft a tailored, first-person response for the form field: "${field.query}".\n` +
            `TARGET CONTEXT:\n` +
            `- Role: ${ctx.role || ctx.roleOrTopic || document.title}\n` +
            `- Company: ${ctx.organization || "this organization"}\n` +
            `- Requirements: ${(ctx.keyRequirements || []).join("; ")}\n` +
            `- Tech Stack: ${(ctx.techStack || ctx.keywords || []).join(", ")}\n\n` +
            `CANDIDATE FACTS (Dr. Ariel Anders):\n${bioEntry}\n${expEntries}\n\n` +
            `INSTRUCTIONS:\n` +
            `- DO NOT repeat the candidate bio word-for-word.\n` +
            `- Explicitly connect Ariel's MIT PhD, Waymo motion planning, Robust.AI behavior trees, and Civ Robotics C++/Python/ROS 2 work to ${ctx.organization || "the role"}'s specific tech stack and domain.\n` +
            `- Output ONLY the final response text (no markdown headers, no placeholders, max 175 words).`;

          const synthesized = await session.prompt(prompt);
          if (synthesized && synthesized.trim()) {
            field.value = synthesized.trim();
            field.confidence = 0.92;
          }
        }
        session.destroy?.();

        // Hydrate the newly synthesized essay fields
        const finalResult = await sendToActiveTab({
          action: "HYDRATE_PAGE_FORMS",
          document: doc,
          options: { highlightConfidence: true }
        });
        statusBox.textContent = `Auto-Fill Completed!\n\n` + JSON.stringify(finalResult, null, 2);
      }
    } else {
      statusBox.textContent = `Auto-Fill Completed (Instant Pre-Draft Used)!\n\n` + JSON.stringify(stage1Result, null, 2);
    }
  } catch (err) {
    statusBox.textContent = `Auto-Fill Error: ${err.message}`;
  } finally {
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
async function renderVault() {
  const { userContextVault = [] } = await chrome.storage.local.get(["userContextVault"]);
  const container = document.getElementById("vault-entries-container");
  const search = document.getElementById("vault-search-input").value.toLowerCase().trim();
  const categoryFilter = document.getElementById("vault-category-filter").value;

  const filtered = userContextVault.filter((entry) => {
    const matchesCat = categoryFilter === "all" || entry.category === categoryFilter;
    const matchesSearch =
      !search ||
      entry.key.toLowerCase().includes(search) ||
      String(entry.value).toLowerCase().includes(search) ||
      (entry.tags && entry.tags.some((t) => t.toLowerCase().includes(search)));
    return matchesCat && matchesSearch;
  });

  document.getElementById("vault-count-badge").textContent = `${userContextVault.length} facts`;

  if (filtered.length === 0) {
    container.innerHTML = `<div style="text-align:center; color:var(--text-muted); padding:16px;">No matching facts found.</div>`;
    return;
  }

  container.innerHTML = filtered
    .map(
      (entry) => `
    <div class="item-card" data-id="${entry.id}">
      <div class="item-header">
        <span class="item-key">${entry.key}</span>
        <div style="display:flex; align-items:center; gap:6px;">
          <span class="item-cat">${entry.category}</span>
          <button class="btn-delete-fact" data-id="${entry.id}" style="background:transparent; border:none; color:var(--rose); cursor:pointer; font-size:12px;">✕</button>
        </div>
      </div>
      <input type="text" class="vault-value-input" data-id="${entry.id}" value="${escapeHtml(String(entry.value))}" style="margin-top:2px;" />
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

function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
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
    await chrome.storage.local.set({ userContextVault: imported });
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
      <div class="item-card" data-delta-id="${delta.deltaId}" style="margin-bottom:8px;">
        <div class="item-header">
          <span class="${badgeClass}">${actionText}</span>
          <span style="font-size:10px; color:var(--text-muted);">${Math.round(delta.confidence * 100)}% confidence</span>
        </div>
        <div style="font-weight:700; color:white; font-size:11.5px; margin: 2px 0;">${delta.key}</div>
        ${delta.previousValue ? `<div style="font-size:10.5px; color:var(--rose); text-decoration:line-through;">Previous: ${escapeHtml(delta.previousValue)}</div>` : ""}
        <div style="font-size:11px; color:var(--accent-light); font-weight:600;">Proposed: ${escapeHtml(delta.proposedValue)}</div>
        <div style="font-size:10px; color:var(--text-muted); font-style:italic; margin-top:2px;">${escapeHtml(delta.evidenceSnippet || "")}</div>
        <div class="btn-row" style="margin-top:6px;">
          <button class="btn btn-primary btn-sm btn-approve-delta" data-delta-id="${delta.deltaId}">Approve & Merge</button>
          <button class="btn btn-subtle btn-sm btn-discard-delta" data-delta-id="${delta.deltaId}">Discard</button>
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
  await chrome.storage.local.set({ userContextVault, pendingDeltas: remaining });
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

  await chrome.storage.local.set({ userContextVault, pendingDeltas: [] });
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
    const bioEntry = vault.find((v) => v.id === "custom.bio")?.value || "";
    const expEntries = vault
      .filter((v) => ["professional", "education"].includes(v.category))
      .map((v) => `${v.key}: ${v.value}`)
      .join("\n");

    const prompt =
      `Write a concise, high-impact 2-paragraph Cover Letter for Ariel Anders applying to:\n` +
      `- Target Role: ${ctx.role || ctx.roleOrTopic || "Software Engineer"}\n` +
      `- Target Company: ${ctx.organization || "Company"}\n` +
      `- Key Job Requirements: ${(ctx.keyRequirements || []).join("; ")}\n` +
      `- Job Tech Stack / Keywords: ${(ctx.techStack || ctx.keywords || []).join(", ")}\n` +
      `- Company Summary: ${ctx.missionSummary || ctx.summary || ""}\n\n` +
      `CANDIDATE BACKGROUND (USE ONLY THESE REAL FACTS):\n` +
      `${bioEntry}\n${expEntries}\n\n` +
      `STRICT RULES:\n` +
      `1. DO NOT copy the candidate bio verbatim. Mention ${ctx.organization || "the team"} and the "${ctx.role || ctx.roleOrTopic || "role"}" role explicitly in sentence 1.\n` +
      `2. Connect Ariel's specific experience (MIT PhD in robotic manipulation under uncertainty, Waymo C++ motion planning & trajectory optimization, Robust.AI behavior trees, Civ Robotics C++/Python/ROS 2 localization & navigation) directly to ${ctx.organization || "the team"}'s requirements (${(ctx.techStack || ctx.keywords || []).slice(0, 5).join(", ")}).\n` +
      `3. Keep it under 180 words, natural, direct, and ready to paste without placeholders.`;

    const drafted = await session.prompt(prompt);
    session.destroy?.();

    if (drafted && drafted.trim().length > 40) {
      ctx.preDraftedCoverLetter = drafted.trim();
      await chrome.storage.local.set({ activeSessionContext: ctx });
      if (badgeEl) badgeEl.textContent = "✅ Tailored Cover Letter Ready (0ms Instant Fill)";
      renderSessionContext();
    }
  } catch (err) {
    console.warn("Background cover letter pre-draft failed:", err);
    if (badgeEl) badgeEl.textContent = "⚠️ Pre-draft unavailable";
  }
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
  "organization": "Company or organization name (e.g. OpenMind, Waymo)",
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

    // Extract user profile facts via on-device LanguageModel strictly for personal portfolio/resume
    if (snapshot.extractedText && ("LanguageModel" in self || self.ai?.languageModel)) {
      try {
        const session = await createPromptSession();
        const promptText = `Extract 1-3 personal profile facts about the user/author (such as full name, email, phone number, location, GitHub URL, portfolio, or current employer) from this portfolio or resume text. Text: "${snapshot.extractedText.slice(0, 2000)}". Respond ONLY with a JSON array of objects with "key", "proposedValue", "category".`;
        const aiResponse = await session.prompt(promptText);
        session.destroy();

        try {
          const match = aiResponse.match(/\[[\s\S]*\]/);
          if (match) {
            const parsedFacts = JSON.parse(match[0]);
            const { pendingDeltas = [] } = await chrome.storage.local.get(["pendingDeltas"]);
            let addedCount = 0;
            for (const item of parsedFacts) {
              if (item.key && item.proposedValue) {
                pendingDeltas.unshift({
                  deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
                  action: "create",
                  category: item.category || "custom",
                  key: item.key,
                  proposedValue: String(item.proposedValue),
                  evidenceSnippet: `Extracted from "${snapshot.title || "portfolio/resume"}"`,
                  sourceUrl: snapshot.url || "",
                  confidence: 0.92
                });
                addedCount++;
              }
            }
            if (addedCount > 0) {
              await chrome.storage.local.set({ pendingDeltas });
              chrome.runtime.sendMessage({ action: "UPDATE_BADGE_COUNT", count: pendingDeltas.length });
            }
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
checkBuiltInAIStatus();
renderVault();
renderObserverQueue();
renderSessionContext();

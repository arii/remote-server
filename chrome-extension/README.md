# LocalForm AI — Chrome Extension Suite (Manifest V3)

A local-first Chrome Extension using Chrome's Built-in On-Device AI foundation models (Gemini Nano via Prompt API and Summarizer API). All LLM inference runs locally inside the browser with zero cloud spend, zero data leakage, and zero latency.

---

## Architecture & Phases

1. **Phase 1: Built-in AI Readiness & Model Manager**
   - Detects `LanguageModel`, `Summarizer`, and `Writer` APIs.
   - Monitors Gemini Nano weight downloads with live progress callbacks.
   - Built-in diagnostic sandbox for testing local prompts.

2. **Phase 2: Form-Export (v1)**
   - Scans DOM across inputs, textareas, selects, and contenteditable elements.
   - Resolves human-readable labels via waterfall: `aria-labelledby` → `aria-label` → `<label for="...">` → wrapping `<label>` → `placeholder` → container text → `name`/`id`.
   - Coalesces radio groups and extracts constraints.
   - One-click export to portable JSON schema.

3. **Phase 3: Form-Import (v1)**
   - Upload any exported JSON schema file.
   - Bypasses React/Vue synthetic event suppression via native property setters (`prototypeSetter.call(el, value)`).
   - Generates execution summary (filled count, skipped, and failure diagnostics).

4. **Phase 4: Form-Export++ (v2)**
   - Enriches exported schemas with page URL, title, meta description, and heading hierarchy.
   - Generates on-device page digests using Chrome's native `Summarizer` API.
   - Inactive/ambiguous fields receive inferred semantic categories (`identity.full_name`, `contact.email`, etc.).

5. **Phase 5: User-Context Vault**
   - Local on-device personal profile and custom knowledge facts (`chrome.storage.local`).
   - Categorization (`identity`, `contact`, `location`, `professional`, `social`, `preferences`, `custom`).
   - Real-time search, category filtering, inline editing, and JSON export/import backup.

6. **Phase 6: Form-Import++ (Autonomous 1-Click Auto-Fill)**
   - Fast deterministic pass (matching `autocomplete` standard tokens and aliases).
   - Batch Prompt API pass ($\le 8$ fields per prompt) with **JSON Schema constrained decoding** (`responseConstraint`).
   - Populates page forms and applies visual confidence outlines (green $\ge 85\%$, amber $< 85\%$).

7. **Phase 7: Observer (Knowledge Collector & Diff Approval Queue)**
   - **Active snapshot**: Captures filled form fields and page text on demand.
   - **Passive submit observer**: Detects form submissions and compares values against the vault.
   - Zero silent overwrites: presents proposed updates in the **Review & Merge Queue** with approve/discard actions and toolbar badge counters.

---

## Building the Extension

No website or web simulator required. Everything builds directly from the command line:

```bash
# Syntax verification & build into dist/
npm run check

# Direct build
npm run build
```

This validates `manifest.json`, checks all referenced scripts and icons, outputs the ready-to-load extension in `dist/`, and packages `dist/localform-ai-extension.zip`.

---

## Loading into Google Chrome

1. Ensure you are running **Google Chrome 138+** (or Chrome Canary/Dev).
2. Enable Chrome Built-in AI flags:
   - Navigate to `chrome://flags/#optimization-guide-on-device-model` and set to **Enabled BypassPerfRequirement**.
   - Navigate to `chrome://flags/#prompt-api-for-gemini-nano` and set to **Enabled**.
   - Relaunch Chrome.
3. Open `chrome://extensions/` in Chrome.
4. Toggle **Developer mode** (top-right corner).
5. Click **Load unpacked** and select the `/dist` (or `/extension`) directory.
6. Click the extension toolbar icon to open the **LocalForm AI Side Panel**.

---

## Directory Structure

```text
chrome-extension/
├── package.json               # Build and syntax test scripts
├── scripts/
│   └── build.mjs             # Validation, asset packaging, and zip builder
├── extension/                 # Source extension files (Manifest V3)
│   ├── manifest.json         # Extension manifest
│   ├── service-worker.js     # Background worker, badge updates, observation relay
│   ├── content-script.js     # DOM scanner, React-safe hydrator, submit observer
│   ├── sidepanel.html        # Clean tabbed Side Panel UI
│   ├── sidepanel.css         # Modern dark theme styles
│   ├── sidepanel.js          # Controller connecting Prompt & Summarizer APIs
│   └── icons/
│       ├── icon16.png
│       ├── icon48.png
│       └── icon128.png
├── dist/                     # Built and validated unpacked extension + zip
└── technical_specs.md        # Complete PRD and technical design document
```

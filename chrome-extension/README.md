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
   - **Learn from my sources**: add your website, GitHub, public LinkedIn page or portfolio URLs (kept in `profileSources`), paste resume text or load a `.txt`/`.md`/`.html`/`.json` file (PDFs are not parsed; paste their text), optionally follow profile links one level (max 5 pages), then press **Learn from all**. Text is split into chunks, each chunk gets a fresh on-device session with a schema-constrained prompt asking for every distinct fact, and results go to the Review & Merge Queue only. The same pipeline backs **Extract My Profile Facts** on the active tab.
   - **Correction learning**: repeated approved corrections for the same field label become alias proposals in a "Learned" section (on-device, aliases only, verified before promotion).
   - **Fill outcomes**: on allowlisted domains, submitting a filled form records per field whether it was kept, edited or cleared (never the values). The Fill tab shows `Last fill: N kept, M edited, K cleared` and the kept-rate over the last 20 fills.
   - **Learned mappings**: when you correct a fill to another vault entry's value, the field is remembered as a quarantined mapping. It is only used after 3 agreeing submissions (you keep or re-enter that entry) or your Confirm click, and the first time you overwrite it, it is deleted. The Learned section lists aliases and mappings in one flat list with Delete (and Confirm for unconfirmed rows). Never stores values.
   - **Prompt guidance**: on the AI Model tab, add global or per-prompt guidance (max 800 characters each). It is inserted into every on-device prompt (profile extraction, form answers, target context, ask, system) under a "User guidance" heading, always before the format instructions, so it cannot replace the output schema. **Revert last** undoes the latest edit (20-step history). **Suggest improvements** asks the on-device model for guidance lines based on fill outcomes and approve/discard counts; each suggestion is checked (known prompt id, short, no URLs, emails, phone numbers or vault values) and only applied when you press Accept.

   - **Import from Google Drive**: in the same card, pick Google Docs, `.txt`, `.md`, `.html` or `.json` files (500KB max each, 5 per run). Their text is added to the text box and then follows the same path as pasted text into the Review & Merge Queue. Read-only (`drive.readonly`); nothing is written to Drive or the vault. The button is disabled until a Google OAuth client is configured (below).
   - **Ask**: on the AI Model tab, type any question and the on-device model answers using only the sources you tick: the current page, other open tabs (each ticked individually; tabs without a content script are skipped with a visible note), relevant vault facts (keyword ranked, sensitive entries never included) and the captured target context. Output streams and can be stopped. **Save answer as fact** sends the answer to the Review & Merge Queue; Ask never writes to the vault.

---

## Building the Extension

No website or web simulator required. Everything builds directly from the command line:

```bash
# Syntax verification & build into dist/
npm run check

# Direct build
npm run build

# Offline fill-accuracy eval (writes eval/last-run.json)
npm run eval
```

`npm run eval` runs the shared matcher over the Ashby, Greenhouse and Lever fixtures plus synthetic ambiguous (billing vs shipping), mismatched-control, multi-step and abstain fixtures against a sample vault (`eval/eval-vault.json`). It scores correct fill, wrong fill, correct abstain and wrong abstain, and prints accuracy per fixture, per case type and in total. It makes no network or model calls. `npm test` fails if accuracy drops below the committed `eval/last-run.json`; after an intended improvement, rerun `npm run eval` and commit the new file.

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
├── eval/                      # Offline fill-accuracy eval (run-eval.mjs, eval-vault.json, last-run.json)
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

## Google Drive import setup

Drive import uses `chrome.identity.getAuthToken`, which reads the client from `oauth2.client_id` in `extension/manifest.json`. The repo ships the placeholder `YOUR_CLIENT_ID.apps.googleusercontent.com`, which the extension treats as unset (the Drive button stays disabled). To enable it, reuse your existing Chrome OAuth client or create one:

1. In Google Cloud Console, open the project that owns the client and enable the **Google Drive API** (APIs & Services, Library).
2. Under APIs & Services, Credentials, create (or reuse) an OAuth client of type **Chrome Extension** and set its **Item ID** to this extension's ID (shown at `chrome://extensions`). Load the extension with a stable ID, for example by adding a `key` to the manifest, so the ID does not change between installs.
3. On the OAuth consent screen, add the `https://www.googleapis.com/auth/drive.readonly` scope. While the app is in Testing, add your Google account under **Test users**.
4. Put the client ID in `oauth2.client_id` in `extension/manifest.json` and rebuild with `npm run check`.

An OAuth client of type Web or Desktop will not work with `getAuthToken`; it must be the Chrome Extension type bound to the extension ID.

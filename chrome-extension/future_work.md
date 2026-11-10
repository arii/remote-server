To evolve this extension from a reactive form-filler (Phases 1–7) into a proactive, autonomous web-browsing personal assistant (**Phases 8–10**), you can layer in specialized browser-agent tooling, hybrid local/daemon execution, and semantic memory.

### 1. Recommended Open-Source Tools & Frameworks to Integrate

| Category | Tool / API | Why It Fits Your Architecture |
| --- | --- | --- |
| **DOM & Accessibility Grounding** | **`chrome.debugger` API (CDP inside MV3)** | Allows your extension to attach directly to the active tab's Chrome DevTools Protocol (`Accessibility.getFullAXTree`, `DOMSnapshot.captureSnapshot`, `Input.dispatchMouseEvent`) without external Playwright servers. Produces true OS-level trusted clicks/keystrokes (`isTrusted: true`) that bypass synthetic event blockers. |
| **Visual + DOM Set-of-Marks (SoM)** | **`browser-use` / WebVoyager `mark_page.js` pattern** | Injects numbered bounding-box badges (`[1]`, `[2]`, `[3]`) over interactive elements and captures `chrome.tabs.captureVisibleTab()`. Enables a multimodal model (or local VLM) to pick actions by numeric ID rather than brittle CSS selectors. |
| **Natural Language Browser Primitives** | **Stagehand / LaVague patterns** | Decomposes high-level goals into three atomic primitives: **`act()`** (execute a single step), **`extract()`** (pull structured Zod/JSON data), and **`observe()`** (list available next actions on the current DOM). |
| **In-Browser Semantic Memory** | **PGlite (`@electric-sql/pglite` + `pgvector`) or `orama` / `Voyage` WASM** | Upgrades Phase 5's `IndexedDB` vault into a full hybrid vector + relational store running 100% in WebAssembly inside the Service Worker or Offscreen Document—enabling semantic retrieval over thousands of past pages, workflows, and facts. |
| **Local Host Bridge** | **`chrome.runtime.connectNative` or Local WebSocket MCP Server** | Connects the Chrome Extension to your local Linux environment, local Ollama models (`qwen3`, `glm`), or LangGraph state machines when a task exceeds Gemini Nano's 9k token window or requires multi-tab orchestration. |

---

### 2. Architectural Evolution: Phases 8 to 10

#### Phase 8: Workflow Recorder & Macro Replay ("Learn by Demonstration")

Before letting an LLM guess multi-step navigation paths, let the extension learn procedural workflows by watching you perform a task once:

* **Action Trace Capture:** In `dom-observer.ts`, record sequences of `(URLPattern, ElementRole/Label, ActionType, ValueTemplate)` when you complete a multi-page flow (e.g., logging into a portal, navigating to an invoice tab, or registering for an event).
* **Parameterized Playbooks:** Use the built-in `LanguageModel` API to generalize recorded values into variables linked to your `User-Context Vault` (e.g., replacing `"San Francisco"` with `{{location.city}}`).
* **Self-Healing Selectors:** Store both the Accessibility Tree node (`role` + `name`) and the DOM selector. If a site redesign breaks the CSS selector during replay, run a fast local `LanguageModel` prompt over the current Accessibility Tree to relocate the moved button.

#### Phase 9: Proactive Intent Detection & Next-Action Copilot

Shift from waiting for a button click to anticipating intent as you browse:

* **Zero-Cost Heuristic Triggers:** Use `chrome.webNavigation` + lightweight DOM mutation observers to detect high-intent page archetypes (e.g., flight/hotel checkout, event registration, calendar scheduling links, PR review pages, or multi-step wizards) before waking up the LLM.
* **Non-Intrusive Ambient Pill:** Inject a Shadow-DOM floating pill in the bottom corner when a match is found:
* *"Detected a multi-step registration form (8 fields match your Vault). **[Auto-Fill & Advance]**"*
* *"You've visited 3 event pages with dates. **[Extract Schedule to Markdown/Calendar]**"*


* **Episodic Memory Consolidation:** Periodically summarize raw browsing snapshots from Phase 7 in an **Offscreen Document** (`chrome.offscreen`) during browser idle states (`chrome.idle.onStateChanged`) so your `User-Context Vault` stays deduplicated and temporal facts stay current.

#### Phase 10: Multi-Step Autonomous Loop (`Observe` $\rightarrow$ `Plan` $\rightarrow$ `Act` $\rightarrow$ `Verify`)

To execute end-to-end tasks across page loads (e.g., *"Find the registration page on this site, fill my details, and stop at the payment screen"*):

1. **Persistent State Machine in Service Worker / IndexedDB:** Content scripts die on every page navigation. Keep the active task state (`goal`, `stepHistory[]`, `currentSubgoal`, `maxSteps: 15`) in `chrome.storage.session` or `IndexedDB`, orchestrated by a LangGraph-style state loop in the Background Service Worker (or bridged to a local daemon).
2. **Distilled Accessibility Tree Input:** Instead of feeding raw HTML to the model (which blows past Chrome's local token limit), serialize only interactive and landmark nodes into a compact markdown tree:
```text
[1] button "Register Now"
[2] input (text) "Promo Code" value=""
[3] link "View Schedule"

```


3. **Post-Action Verification Loop:** After every `click` or `fill`, wait for `networkIdle` / DOM stabilization, diff the before/after Accessibility Tree, and verify whether the expected state transition occurred (e.g., did a validation error banner appear, or did the URL/heading advance?).
4. **Hard Safety Gateways (Human-in-the-Loop):** Enforce a deterministic blocklist in the content script that halts autonomous execution and requests a single click of approval whenever an action targets buttons matching `/submit|pay|purchase|delete|send|confirm|book/i` or inputs inside a payment iframe.

---

### 3. Hybrid Routing Strategy (Chrome Built-in Nano vs. Local Daemon/Cloud)

* Tier 1 — Instant In-Browser (Gemini Nano via Prompt / Summarizer API):


* Field-to-vault semantic mapping, single-page summarization, DOM element recovery, and PII/fact extraction. Zero network latency, works offline.


* **Tier 2 — Local Workstation Bridge (WebSocket / Native Messaging to Local Ollama or LangGraph):**
* Multi-page planning, constrained-decoding state graphs, and vision-grounded Set-of-Marks reasoning when DOM trees are obfuscated (Canvas/custom WebGL UIs).


* **Tier 3 — MCP (Model Context Protocol) Tool Exposure:**
* Expose your extension's `scanCurrentPageForms`, `hydratePageForms`, `capturePageSnapshot`, and `ContextRepository` as an **MCP Client/Server bridge** over a local WebSocket so any external agent or CLI tool on your machine can read your active tab's forms or query your `User-Context Vault` on demand.




This technical specification suite covers your 7-phase iterative Chrome Extension architecture, utilizing Chrome's built-in on-device AI APIs (LanguageModel / Prompt API, Summarizer, Writer, and Rewriter).

## **1\. Product Requirements Document (PRD)**

### **1.1 Product Vision & Core Principles**

A zero-latency, 100% local-first Chrome Extension (Manifest V3) that evolves from a hardware-verified AI readiness utility into an autonomous, context-aware form filler and knowledge observer.

> * **Strict Ship-Ready Milestones:** Every phase produces a standalone, immediately usable tool before the next phase begins.  
> * **Privacy & Zero Cloud Spend:** All LLM inference runs locally via Chrome's built-in foundation models (Gemini Nano via Prompt API and Summarizer API, shipped in Chrome 138+ for Extensions and Chrome 148+ for Web/sampling parameters). All personal context resides strictly in local browser storage ( IndexedDB / chrome.storage.local).  
> * **Deterministic DOM Resilience:** Form serialization and hydration handle modern reactive frameworks (React, Vue, Angular) and Shadow DOMs without brittle XPath dependencies.

### **1.2 Phased Delivery Roadmap**

| Phase | Milestone Name | Standalone Utility | Primary Success Metric |
| :---- | :---- | :---- | :---- |
| **Phase 1** | **AI Readiness & Model Manager** | Diagnostic Side Panel that checks hardware/flag eligibility, triggers local model downloads, and provides a live test console. | 100% accurate detection of LanguageModel & Summarizer states; real-time download progress tracking. |
| **Phase 2** | **Form-Export (v1)** | One-click extraction of all interactive inputs on the active tab into a clean, portable JSON/Markdown schema file. | Captures \\ge 98\\% of visible/accessible form inputs with resolved human-readable labels and stable CSS selectors. |
| **Phase 3** | **Form-Import (v1)** | Upload a completed Form-Export JSON file to automatically populate and dispatch events to all matching page inputs. | Successfully populates React/controlled inputs, native selects, checkboxes, and radios without state loss on submit. |
| **Phase 4** | **Form-Export++ (v2)** | Enriches exported form schemas with page metadata, URL, section hierarchy, and an on-device AI summary of the form's purpose. | Exports include Summarizer page digest and LanguageModel semantic tags for ambiguous fields. |
| **Phase 5** | **User-Context Vault** | CRUD UI inside the extension Side Panel/Options page managing a structured personal profile and custom key-value facts. | Instant local search, JSON backup/restore, and categorization of identity, address, work, and custom snippets. |
| **Phase 6** | **Form-Import++ (Auto-Fill)** | One-click autonomous form fill: runs Form-Export++ in memory, matches fields against User-Context via Prompt API JSON schema constraint, and executes Form-Import. | \\ge 90\\% field match accuracy on unseen forms; clear visual confidence highlights (green \= high confidence, amber \= needs review). |
| **Phase 7** | **Observer (Knowledge Collector)** | "Snapshot Page" action and optional passive submit listener that extracts new facts from filled forms or browsed pages to update User-Context. | Zero silent overwrites; presents a diff approval toast/card in Side Panel to merge new or updated facts into the vault. |

## **2\. Feature Requirements & Use Cases Document**

### **Phase 1: Built-in AI Readiness & Model Bootstrapper**

> * **Use Case:** You install the extension on a workstation or laptop and immediately verify whether Chrome's on-device models are ready, need downloading, or require chrome://flags adjustments.  
> * **Functional Requirements:**  
  * **FR-1.1:** Query LanguageModel.availability() and Summarizer.availability() on Side Panel open.  
  * **FR-1.2:** Render status badges (available, downloadable, downloading, unavailable) for Prompt, Summarizer, Writer, and Rewriter APIs.  
  * **FR-1.3:** Provide a **"Download & Initialize Models"** button that invokes LanguageModel.create({ monitor }) within a user gesture handler and renders a progress bar bound to downloadprogress (e.loaded \* 100).  
  * **FR-1.4:** If unavailable, display copyable chrome://flags links (\#optimization-guide-on-device-model, \#prompt-api-for-gemini-nano) and chrome://components (Optimization Guide On Device Model) instructions.  
  * **FR-1.5:** Include a built-in diagnostic prompt sandbox to test latency and token generation speed.

### **Phase 2: Form-Export (v1)**

> * **Use Case:** You land on a complex application, registration, or configuration page and export its blank structure as a JSON file to fill out offline, version-control, or pass to a script/LLM.  
> * **Functional Requirements:**  
  * **FR-2.1:** Traverse the active tab's DOM (including open Shadow Roots and same-origin iframes) to locate input (excluding hidden, submit, button, image), textarea, select, and \[contenteditable="true"\].  
  * **FR-2.2:** Resolve the semantic query/label for each field using a strict priority waterfall:  
    1. aria-labelledby target text  
    2. aria-label attribute  
    3. Explicit \<label for="id"\> text  
    4. Wrapping \<label\> text  
    5. placeholder attribute  
    6. Preceding sibling or parent container text node / \<legend\>  
    7. name or id attribute fallback  
  * **FR-2.3:** Extract field constraints (type, required, pattern, min, max, maxlength) and enumerated options for \<select\>, \<datalist\>, and radio/checkbox groups.  
  * **FR-2.4:** Trigger a browser file download (\<domain\>-form-schema-\<timestamp\>.json) or copy directly to clipboard.

### **Phase 3: Form-Import (v1)**

> * **Use Case:** You select a filled JSON file generated from Phase 2, and the extension populates every corresponding input on the live page.  
> * **Functional Requirements:**  
  * **FR-3.1:** Accept a .json file via file picker or drag-and-drop in the Side Panel.  
  * **FR-3.2:** Locate each target element using a fallback locator chain: cssSelector \\rightarrow id \\rightarrow name \+ inputType \\rightarrow fuzzy label match.  
  * **FR-3.3:** Bypass React/Vue synthetic event suppression by invoking the native HTMLInputElement.prototype.value (or HTMLTextAreaElement / HTMLSelectElement) setter before dispatching new Event('input', { bubbles: true }) and new Event('change', { bubbles: true }).  
  * **FR-3.4:** Return an execution summary report (Filled: X, Skipped: Y, Not Found: Z) and scroll to or highlight any failed fields.

### **Phase 4: Form-Export++ (v2)**

> * **Use Case:** Exported forms include full page context so an LLM or automated pipeline understands *why* a field is asking a question (e.g., distinguishing "Billing Address" from "Shipping Address" or understanding a grant application prompt).  
> * **Functional Requirements:**  
  * **FR-4.1:** Capture page-level context: url, origin, title, meta\[name="description"\], and heading hierarchy (h1–h3).  
  * **FR-4.2:** Group fields logically by their nearest enclosing \<form\>, \<fieldset\> (\<legend\>), or semantic section heading.  
  * **FR-4.3:** Pass visible page text surrounding the form to the built-in Summarizer API (type: 'key-points', length: 'short') to attach a pageSummary block to the export file.  
  * **FR-4.4:** Optionally run a single batch pass via LanguageModel to infer a normalized semanticCategory (e.g., person.full\_name, work.organization, custom.free\_response) for each exported field.

### **Phase 5: User-Context Vault**

> * **Use Case:** Maintain a transparent, editable local database of your personal details, standard bio paragraphs, links, and recurring form answers.  
> * **Functional Requirements:**  
  * **FR-5.1:** Store structured entities in IndexedDB across two collections:  
    * **Core Profile:** Standardized key-value pairs (Legal Name, Preferred Name, Email, Phone, Street Address, City, State, Zip, Country, Portfolio URL, GitHub, LinkedIn, Role/Title, Company).  
    * **Knowledge Snippets & Custom Facts:** Freeform or tagged entries (e.g., dietary restrictions, T-shirt size, short bio, project descriptions, frequent application answers) with key, value, tags\[\], sourceUrl, and updatedAt.  
  * **FR-5.2:** Provide a clean Side Panel / Options Tab UI with instant search, inline editing, tag filtering, and one-click JSON export/import of the entire vault.

### **Phase 6: Form-Import++ (Autonomous AI Form Fill)**

> * **Use Case:** Click **"Auto-Fill Page"** on any web form; the extension inspects the page via Form-Export++, queries your User-Context Vault, uses the local LanguageModel to map and synthesize answers, and populates the form via Form-Import.  
> * **Functional Requirements:**  
  * **FR-6.1:** Execute a fast deterministic pass first (exact/regex matching of autocomplete attributes and common field names against Core Profile keys) to save tokens and time.  
  * **FR-6.2:** Batch remaining unresolved or open-ended fields into a structured prompt sent to LanguageModel.prompt() using responseConstraint (JSON Schema) so the local model outputs guaranteed valid JSON mappings.  
  * **FR-6.3:** For long-form textareas, use the Writer / Rewriter API or LanguageModel grounded strictly on relevant User-Context snippets to draft responses matching character limits.  
  * **FR-6.4:** Render an interactive preview or highlight filled fields on the page with a floating revert/edit pill per field.

### **Phase 7: Observer (Contextual Knowledge Collector)**

> * **Use Case:** Click **"Snapshot Page"** on a profile page, portfolio, or filled form (or toggle "Learn from Form Submits") so the extension extracts new/updated personal facts and proposes adding them to User-Context.  
> * **Functional Requirements:**  
  * **FR-7.1:** **Active Snapshot Mode:** Extract clean readable text and filled input values from the current tab when the user clicks "Snapshot Page".  
  * **FR-7.2:** **Form Submit Listener Mode (Opt-in per domain):** Intercept submit events on forms, diffing user-entered values against existing User-Context entries.  
  * **FR-7.3:** Run a LanguageModel extraction prompt with a JSON Schema constraint to output candidate FactDelta\[\] items (action: 'create' \| 'update', key, proposedValue, previousValue, confidence, evidence).  
  * **FR-7.4:** Present a **Review & Merge Queue** in the Side Panel so you approve, edit, or discard proposed context updates with a single click.

## **3\. Design Specification & Technical Design Document (TDD)**

### **3.1 System Architecture (Manifest V3)**

`+-----------------------------------------------------------------------+`  
`|                         CHROME BROWSER (MV3)                          |`  
`|                                                                       |`  
`|  +-------------------------+       chrome.runtime.sendMessage         |`  
`|  |   Content Script        | <=====================================>  |`  
`|  |   (DOM Isolated World)  |                                          |`  
`|  |  - FormScanner          |       +-------------------------------+  |`  
`|  |  - FormHydrator         |       |   Background Service Worker   |  |`  
`|  |  - PageObserver         |       |  - MessageRouter              |  |`  
`|  |  - HighlightOverlay     |       |  - AIOrchestrator (Prompt/Sum)|  |`  
`|  +-------------------------+       |  - ContextRepository (IDB)    |  |`  
`|               ^                    +-------------------------------+  |`  
`|               |                                    ^                  |`  
`|               | DOM Events /                       | Port / RPC       |`  
`|               v CustomEvent                        v                  |`  
`|  +-------------------------+       +-------------------------------+  |`  
`|  |   Active Web Page DOM   |       |   Side Panel & Options UI     |  |`  
`|  |  (React/Vue/Native/     |       |  - Phase 1: Model Status UI   |  |`  
`|  |   Shadow DOM Inputs)    |       |  - Phase 2/3: Export/Import   |  |`  
`|  +-------------------------+       |  - Phase 5: User-Context CRUD |  |`  
`|                                    |  - Phase 6/7: AutoFill & Diffs|  |`  
`|                                    +-------------------------------+  |`  
`+-----------------------------------------------------------------------+`

### **3.2 Execution Context for Built-in AI**

> * In Chrome 138+, the Built-in AI APIs (LanguageModel, Summarizer, Writer, Rewriter) are exposed directly in extension page contexts (chrome-extension:// Side Panel, Popup, Options, Offscreen Documents) and Service Workers.  
> * **Architectural Choice:** Run the **Side Panel (sidepanel.html)** as the primary UI and AI execution host during interactive use, backed by an IndexedDB singleton (ContextDB) shared across extension contexts. Because LanguageModel.create() requires a user activation gesture when downloading a model for the first time, triggering bootstrap directly from the Side Panel button satisfies Chrome's user-gesture requirement cleanly.

### **3.3 Core Data Schemas (TypeScript Interfaces)**

#### **Form Export Schema (v1 & v2 Compatible)**

`export type InputDataType =`  
  `| 'text' | 'email' | 'tel' | 'url' | 'number' | 'date'`  
  `| 'password' | 'textarea' | 'select-one' | 'select-multiple'`  
  `| 'checkbox' | 'radio-group' | 'contenteditable';`

`export interface FieldOption {`  
  `label: string;`  
  `value: string;`  
  `selected?: boolean;`  
`}`

`export interface FormFieldSchema {`  
  `fieldId: string;              // Deterministic hash of locator + label`  
  `query: string;                // Resolved human-readable question/label`  
  `inputType: InputDataType;`  
  `value: string | boolean | string[] | null; // Current or to-be-imported value`  
  `locator: {`  
    `cssSelector: string;`  
    `id?: string;`  
    `name?: string;`  
    `ariaLabel?: string;`  
    `shadowPath?: string[];      // Array of host selectors if inside Shadow DOM`  
  `};`  
  `constraints?: {`  
    `required: boolean;`  
    `placeholder?: string;`  
    `pattern?: string;`  
    `maxLength?: number;`  
    `autocomplete?: string;`  
    `options?: FieldOption[];    // Populated for select, radio-group, datalist`  
  `};`  
  `// Phase 4 (Form-Export++) additions:`  
  `sectionHeading?: string;      // Nearest fieldset legend or h1-h4`  
  `surroundingText?: string;     // Nearby helper text / instructions`  
  `semanticCategory?: string;    // AI-inferred category (e.g., "address.postal_code")`  
  `confidence?: number;          // Phase 6 AI fill confidence (0.0 - 1.0)`  
`}`

`export interface FormExportDocument {`  
  `schemaVersion: '1.0' | '2.0';`  
  `exportedAt: string;           // ISO-8601`  
  `// Phase 4 (Form-Export++) page context block:`  
  `pageContext?: {`  
    `url: string;`  
    `domain: string;`  
    `title: string;`  
    `description?: string;`  
    `pageSummary?: string;       // Generated by Summarizer API`  
    `headings?: string[];`  
  `};`  
  `fields: FormFieldSchema[];`  
`}`

#### **User-Context Database Schema (Phase 5 & 7\)**

`export type ContextCategory =`  
  `| 'identity' | 'contact' | 'location' | 'professional'`  
  `| 'education' | 'social' | 'preferences' | 'custom';`

`export interface UserContextEntry {`  
  `id: string;                   // UUID or normalized slug (e.g., "identity.full_name")`  
  `category: ContextCategory;`  
  `key: string;                  // Human label, e.g., "Legal Full Name"`  
  `aliases: string[];            // ["full name", "your name", "applicant name"]`  
  `value: string;                // Stored value`  
  `tags: string[];               // Context tags (e.g., ["work", "shipping", "bio"])`  
  `metadata: {`  
    `updatedAt: string;`  
    `source: 'manual' | 'form-submit' | 'page-snapshot';`  
    `sourceUrl?: string;`  
    `confidence: number;`  
  `};`  
`}`

`export interface FactDelta {`  
  `deltaId: string;`  
  `action: 'create' | 'update';`  
  `targetEntryId?: string;`  
  `category: ContextCategory;`  
  `key: string;`  
  `proposedValue: string;`  
  `previousValue?: string;`  
  `evidenceSnippet: string;`  
  `sourceUrl: string;`  
  `confidence: number;`  
`}`

### **3.4 Constrained Decoding for Phase 6 (Form-Import++) & Phase 7 (Observer)**

To prevent hallucinated formatting from Gemini Nano, all LanguageModel.prompt() calls in Phases 6 and 7 pass a strict JSON Schema via responseConstraint:  
`const autoFillResponseSchema = {`  
  `type: "object",`  
  `properties: {`  
    `mappings: {`  
      `type: "array",`  
      `items: {`  
        `type: "object",`  
        `properties: {`  
          `fieldId: { type: "string" },`  
          `filledValue: { type: ["string", "boolean", "null"] },`  
          `matchedContextKey: { type: ["string", "null"] },`  
          `confidence: { type: "number", minimum: 0, maximum: 1 }`  
        `},`  
        `required: ["fieldId", "filledValue", "confidence"],`  
        `additionalProperties: false`  
      `}`  
    `}`  
  `},`  
  `required: ["mappings"],`  
  `additionalProperties: false`  
`};`

`const resultJson = await session.prompt(promptText, {`  
  `responseConstraint: autoFillResponseSchema`  
`});`  
`const parsed = JSON.parse(resultJson);`

**Token Budget Optimization:** Because on-device models have a constrained context window (\~4k–6k tokens) and perform best on small batches, AIOrchestrator chunks FormFieldSchema\[\] into batches of **at most 8 fields per prompt call** and pre-filters the UserContextEntry\[\] list using keyword/tag overlap before constructing the prompt.

## **4\. Component & Module Specification**

### **4.1 Directory Structure**

`src/`  
`├── manifest.json`  
`├── types/`  
`│   └── schema.ts                # Shared interfaces (FormExportDocument, UserContextEntry, MessageRPC)`  
`├── core/`  
`│   ├── ai-engine.ts             # Phase 1, 4, 6, 7: Built-in AI wrapper (LanguageModel, Summarizer)`  
`│   ├── context-db.ts            # Phase 5, 6, 7: IndexedDB wrapper + default profile seeding`  
`│   └── matcher.ts               # Phase 6: Deterministic + LLM hybrid field-to-context matcher`  
`├── content/`  
`│   ├── index.ts                 # Content script message listener`  
`│   ├── dom-scanner.ts           # Phase 2, 4: DOM/ShadowDOM traversal, label resolver, context extractor`  
`│   ├── dom-hydrator.ts          # Phase 3, 6: React-safe value setter, event dispatcher, highlight UI`  
`│   └── dom-observer.ts          # Phase 7: Page snapshot reader & opt-in form submit interceptor`  
`├── background/`  
`│   └── service-worker.ts        # Side panel opener, tab routing, badge notifications`  
`└── sidepanel/`  
    `├── index.html`  
    `├── App.tsx                  # Tabbed UI shell progressing through Phases 1 -> 7`  
    `└── components/`  
        `├── ModelStatusCard.tsx  # Phase 1`  
        `├── FormExporter.tsx     # Phase 2 & 4`  
        `├── FormImporter.tsx     # Phase 3 & 6`  
        `├── ContextVault.tsx     # Phase 5`  
        `└── ObserverQueue.tsx    # Phase 7`

### **4.2 Detailed Module Specifications**

#### **Module 1: src/core/ai-engine.ts (Phases 1, 4, 6, 7\)**

> * **Responsibilities:** Manages lifecycle, availability checks, download monitoring, session caching, and structured prompting for Chrome's built-in AI APIs.  
> * **Public Interface:**  
>   `export interface ModelCapabilitiesReport {`  
>     `promptApi: 'available' | 'downloadable' | 'downloading' | 'unavailable';`  
>     `summarizerApi: 'available' | 'downloadable' | 'downloading' | 'unavailable';`  
>     `chromeVersion: string;`  
>   `}`

>   `export class BuiltInAIEngine {`  
>     `async checkCapabilities(): Promise<ModelCapabilitiesReport>;`  
>     `async bootstrapModels(onProgress: (loadedPct: number) => void): Promise<void>;`  
>     `async summarizePageContext(rawText: string): Promise<string>;`  
>     `async mapFieldsToContext(`  
>       `fields: FormFieldSchema[],`  
>       `contextEntries: UserContextEntry[],`  
>       `pageSummary?: string`  
>     `): Promise<Array<{ fieldId: string; value: any; confidence: number }>>;`  
>     `async extractFactsFromSnapshot(`  
>       `pageTextOrFormPairs: string,`  
>       `existingContext: UserContextEntry[]`  
>     `): Promise<FactDelta[]>;`  
>   `}`

> * **Key Implementation Details:**  
  * Uses session.clone() after initializing a base LanguageModel session with the system prompt to avoid repeating system prompt prefill overhead across batched field chunks.  
  * Automatically destroys sessions on completion or when contextUsage approaches contextWindow \- 512\.

#### **Module 2: src/content/dom-scanner.ts (Phases 2 & 4\)**

> * **Responsibilities:** Discovers form controls, computes resilient selectors, extracts human-readable queries (label), and gathers surrounding page context.  
> * **Public Interface:**  
>   `export interface ScanOptions {`  
>     `includePageContext: boolean; // False for Phase 2, True for Phase 4`  
>     `includeCurrentValues: boolean;`  
>   `}`

>   `export function scanCurrentPageForms(options: ScanOptions): FormExportDocument;`

> * **Key Implementation Details:**  
  * **Radio Group Coalescing:** Collapses multiple \<input type="radio" name="groupName"\> elements into a single FormFieldSchema item of inputType: 'radio-group', storing each radio's individual label and value inside constraints.options\[\].  
  * **Shadow DOM Traversal:** Recursively walks elements where el.shadowRoot \!== null and records the host chain in locator.shadowPath so dom-hydrator.ts can re-enter the shadow boundary during import.  
  * **Visibility Filter:** Skips elements with offsetParent \=== null (unless position: fixed), aria-hidden="true", or disabled attribute.

#### **Module 3: src/content/dom-hydrator.ts (Phases 3 & 6\)**

> * **Responsibilities:** Takes a FormExportDocument (or AI-mapped array of field values) and populates the live DOM reliably across vanilla HTML and virtual-DOM frameworks.  
> * **Public Interface:**  
>   `export interface HydrationResult {`  
>     `totalFields: number;`  
>     `filledCount: number;`  
>     `failedFields: Array<{ fieldId: string; query: string; reason: string }>;`  
>   `}`

>   `export function hydratePageForms(`  
>     `doc: FormExportDocument,`  
>     `options?: { highlightConfidence?: boolean }`  
>   `): HydrationResult;`

> * **Key Implementation Details:**  
  * **React/Vue Native Value Setter Trick:**  
    `function setNativeValue(element: HTMLElement, value: string) {`  
      `const valueSetter = Object.getOwnPropertyDescriptor(element, 'value')?.set;`  
      `const prototype = Object.getPrototypeOf(element);`  
      `const prototypeValueSetter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;`

      `if (valueSetter && valueSetter !== prototypeValueSetter && prototypeValueSetter) {`  
        `prototypeValueSetter.call(element, value);`  
      `} else if (valueSetter) {`  
        `valueSetter.call(element, value);`  
      `} else {`  
        `(element as HTMLInputElement).value = value;`  
      `}`  
      `element.dispatchEvent(new Event('input', { bubbles: true }));`  
      `element.dispatchEvent(new Event('change', { bubbles: true }));`  
      `element.dispatchEvent(new Event('blur', { bubbles: true }));`  
    `}`

  * **Select & Radio Resolution:** Matches value first by exact option value, then case-insensitive option label, and finally substring match.

#### **Module 4: src/core/context-db.ts (Phases 5, 6, 7\)**

> * **Responsibilities:** Wrapper around IndexedDB (dbName: 'LocalFormAI\_UserContext', object stores: entries, pending\_deltas, form\_templates).  
> * **Public Interface:**  
>   `export class ContextRepository {`  
>     `async getAllEntries(): Promise<UserContextEntry[]>;`  
>     `async upsertEntry(entry: Omit<UserContextEntry, 'id'> & { id?: string }): Promise<UserContextEntry>;`  
>     `async deleteEntry(id: string): Promise<void>;`  
>     `async searchRelevantContext(queries: string[]): Promise<UserContextEntry[]>;`  
>     `async queueFactDeltas(deltas: FactDelta[]): Promise<void>;`  
>     `async applyFactDelta(deltaId: string, editedValue?: string): Promise<void>;`  
>     `async exportVaultJson(): Promise<string>;`  
>     `async importVaultJson(rawJson: string, mergeStrategy: 'overwrite' | 'merge'): Promise<void>;`  
>   `}`

#### **Module 5: src/core/matcher.ts (Phase 6\)**

> * **Responsibilities:** Coordinates the two-stage pipeline for Form-Import++:  
  1. **Tier 1 (Zero-Token Deterministic Match):** Maps fields with standard autocomplete attributes (given-name, family-name, email, tel, street-address, postal-code, organization, url) or high-similarity alias matches directly from ContextRepository.  
  2. **Tier 2 (On-Device LLM Semantic Match):** Sends remaining unmapped fields \+ pageContext \+ relevant UserContextEntry candidates to BuiltInAIEngine.mapFieldsToContext().

#### **Module 6: src/content/dom-observer.ts (Phase 7\)**

> * **Responsibilities:** Captures user knowledge from active browsing and form interactions.  
> * **Public Interface:**  
>   `export interface PageSnapshotPayload {`  
>     `url: string;`  
>     `title: string;`  
>     `extractedText: string;`  
>     `filledFormFields: FormFieldSchema[];`  
>   `}`

>   `export function capturePageSnapshot(): PageSnapshotPayload;`  
>   `export function attachFormSubmitObserver(onFormCaptured: (payload: PageSnapshotPayload) => void): void;`

> * **Key Implementation Details:**  
  * **Security & Noise Guardrails:** Automatically excludes \<input type="password"\>, credit card fields (autocomplete="cc-\*"), CVV, and SSN patterns (\\b\\d{3}-\\d{2}-\\d{4}\\b) before text or form values ever reach the AI engine or storage.  
  * Strips navbars, footers, scripts, and styles when capturing readable text for "Snapshot Page", capping text payload at 6,000 characters to fit comfortably inside Gemini Nano's context window.

### **4.3 Message Passing Protocol (chrome.runtime.sendMessage)**

| Message Type (action) | Sender \\rightarrow Receiver | Payload | Response | Phase |
| :---- | :---- | :---- | :---- | :---- |
| SCAN\_PAGE\_FORMS | SidePanel \\rightarrow ContentScript | { includePageContext: boolean } | FormExportDocument | 2, 4, 6 |
| HYDRATE\_PAGE\_FORMS | SidePanel \\rightarrow ContentScript | { document: FormExportDocument, highlight: boolean } | HydrationResult | 3, 6 |
| CAPTURE\_PAGE\_SNAPSHOT | SidePanel \\rightarrow ContentScript | {} | PageSnapshotPayload | 7 |
| FORM\_SUBMIT\_DETECTED | ContentScript \\rightarrow ServiceWorker | PageSnapshotPayload | { queuedDeltas: number } | 7 |

### **4.4 Iterative Build & Verification Checklist**

> 1. **Phase 1 Build:** Scaffold Vite \+ TypeScript \+ Manifest V3 (sidePanel, activeTab, scripting, storage permissions) and @types/dom-chromium-ai. Verify LanguageModel.availability() and model download progress bar in the Side Panel.  
> 2. **Phase 2 Build:** Implement dom-scanner.ts. Test on a multi-input page (e.g., GitHub settings or a sample job application) and verify the downloaded .json file captures all labels and input types.  
> 3. **Phase 3 Build:** Implement dom-hydrator.ts. Edit values in the Phase 2 .json file, upload it in the Side Panel, and verify all inputs populate and trigger validation states properly.  
> 4. **Phase 4 Build:** Wire Summarizer and LanguageModel into the export pipeline to enrich the JSON with pageSummary and semanticCategory.  
> 5. **Phase 5 Build:** Implement context-db.ts and ContextVault.tsx. Populate your core profile and custom snippets.  
> 6. **Phase 6 Build:** Connect matcher.ts to combine Form-Export++ \+ ContextVault \+ Form-Import. Test one-click auto-fill.  
> 7. **Phase 7 Build:** Enable dom-observer.ts and ObserverQueue.tsx to snapshot pages and approve fact diffs into ContextVault.

Would you like me to generate the Phase 1 \+ Phase 2 starter codebase (complete manifest.json, TypeScript configuration, ai-engine.ts, and dom-scanner.ts) so you can load it directly into chrome://extensions?
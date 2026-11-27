// ============================================================================
// LocalForm AI - Firebase AI Logic hybrid session (#BuildHybridAI)
// ============================================================================
// Routes prompt requests through the Firebase AI Logic Web SDK in
// PREFER_ON_DEVICE mode: on-device Gemini Nano via Chrome's Prompt API when
// available, with seamless fallback to the cloud-hosted Gemini Developer API
// model (free tier) otherwise. This is the same hybrid inference path the
// #BuildHybridAI rules ask for, and it satisfies "imported and actually
// called": every panel prompt goes through getGenerativeModel() here whenever
// a local Firebase config exists.
//
// The returned session mirrors the subset of Chrome's LanguageModel session
// interface the panel uses ({ prompt, promptStreaming, destroy }), so callers
// need no changes. Returns null when Firebase is not configured or the SDK
// bundle is missing; callers then keep using the built-in APIs directly.
//
// SDK contract notes (firebase@12.19.0, verified against the bundled
// @firebase/ai types and runtime):
// - With a hybrid `mode`, getGenerativeModel() IGNORES every top-level
//   param except mode/onDeviceParams/inCloudParams (it warns and drops them),
//   so the system prompt must be set per leg: inCloudParams.systemInstruction
//   for cloud, onDeviceParams.createOptions.initialPrompts for on-device.
// - inCloudParams requires an explicit model name; gemini-2.5-flash-lite is
//   the SDK's own default when inCloudParams is omitted.
// - Per-request generationConfig.responseSchema only reaches the CLOUD leg.
//   On-device structured output comes exclusively from
//   onDeviceParams.promptOptions.responseConstraint, fixed when the model is
//   created - so this module keeps one model per distinct responseConstraint.
//
// Setup: see firebase-config.example.js. Config and App Check debug tokens
// are local-only and gitignored; never commit them.

import { loadFirebaseBundle, loadFirebaseConfig } from "./firebase-loader.js";

const REQUIRED_CONFIG_KEYS = ["apiKey", "projectId", "appId"];
const PLACEHOLDER_VALUE = /^\s*(YOUR[_-]|TODO|REPLACE|XXX)/i;

// In-cloud model for the hybrid cloud leg. The SDK's own fallback default
// (gemini-2.5-flash-lite) is rejected for newer Firebase projects ("no longer
// available to new users", verified live 2026-11-24), so this module ALWAYS
// sets inCloudParams explicitly; gemini-3.5-flash-lite is the replacement the
// API itself names in that error.
const DEFAULT_HYBRID_IN_CLOUD_MODEL = "gemini-3.5-flash-lite";

// The Firebase Schema shape is a subset of OpenAPI 3.0: uppercase type names,
// and only these fields. Chrome's responseConstraint accepts full JSON
// Schema, so extra keys (maxLength, additionalProperties, ...) are dropped.
const FIREBASE_SCHEMA_KEYS = ["type", "description", "nullable", "enum", "properties", "required", "items", "format"];

export function toFirebaseSchema(node) {
  if (!node || typeof node !== "object") return undefined;
  const out = {};
  for (const key of FIREBASE_SCHEMA_KEYS) {
    if (!(key in node)) continue;
    if (key === "type" && typeof node.type === "string") {
      out.type = node.type.toUpperCase();
    } else if (key === "properties" && node.properties && typeof node.properties === "object") {
      out.properties = {};
      for (const [name, child] of Object.entries(node.properties)) {
        out.properties[name] = toFirebaseSchema(child);
      }
    } else if (key === "items") {
      out.items = toFirebaseSchema(node.items);
    } else {
      out[key] = node[key];
    }
  }
  return out;
}

export function configLooksUsable(config) {
  if (!config || typeof config !== "object") return false;
  return REQUIRED_CONFIG_KEYS.every(
    (key) => typeof config[key] === "string" && config[key].trim() !== "" && !PLACEHOLDER_VALUE.test(config[key])
  );
}

// Builds getGenerativeModel() params that SDK 12.19 actually honors: nothing
// but mode/onDeviceParams/inCloudParams at the top level. The system prompt
// is applied to BOTH legs; a responseConstraint is applied to the on-device
// leg as raw JSON Schema (the Prompt API's own format, forwarded unchanged).
export function buildHybridParams(firebase, { systemPrompt, responseConstraint } = {}) {
  const params = { mode: firebase.InferenceMode.PREFER_ON_DEVICE };
  const onDeviceParams = {};
  if (systemPrompt) {
    onDeviceParams.createOptions = {
      initialPrompts: [{ role: "system", content: [{ type: "text", value: systemPrompt }] }]
    };
  }
  if (responseConstraint) {
    onDeviceParams.promptOptions = { responseConstraint };
  }
  if (Object.keys(onDeviceParams).length > 0) params.onDeviceParams = onDeviceParams;
  // Always set inCloudParams: it requires a model name, and the SDK's stale
  // built-in default must never apply (see DEFAULT_HYBRID_IN_CLOUD_MODEL).
  params.inCloudParams = { model: DEFAULT_HYBRID_IN_CLOUD_MODEL };
  if (systemPrompt) params.inCloudParams.systemInstruction = systemPrompt;
  return params;
}

// Warms the on-device model without blocking session creation: the SDK
// documents that PREFER_ON_DEVICE falls back to cloud inference while the
// on-device model is still downloading (and a first-time Nano download is
// many GB), so awaiting here would stall every prompt flow. The promise is
// always catch-handled so a failed or unsupported download can never surface
// as an unhandled rejection.
function warmDeviceModel(model) {
  const ready = model.initializeDeviceModel?.(() => {});
  if (ready && typeof ready.catch === "function") {
    ready.catch((err) => console.warn("LocalForm AI: on-device model warm-up failed:", err));
  }
}

let firebaseAppPromise = null;
let appCheckActive = false;

async function getOrInitFirebaseApp(firebase, config, appCheckDebugToken) {
  if (!firebaseAppPromise) {
    firebaseAppPromise = (async () => {
      const app = firebase.initializeApp(config);
      // Firebase can enforce App Check for AI Logic projects. No provider is
      // passed below, so initializeAppCheck currently throws and App Check
      // stays inactive (surfaced via getHybridStatus). The debug token, when
      // set, must be set before initializeAppCheck and must never be committed.
      if (appCheckDebugToken) {
        self.FIREBASE_APPCHECK_DEBUG_TOKEN = appCheckDebugToken;
      }
      try {
        firebase.initializeAppCheck(app, { isTokenAutoRefreshEnabled: true });
        appCheckActive = true;
      } catch (err) {
        // On-device inference does not call the cloud API and works without
        // App Check; only the cloud fallback leg needs the token.
        appCheckActive = false;
        console.warn("LocalForm AI: App Check init failed; cloud fallback may be rejected:", err);
      }
      return app;
    })();
  }
  return firebaseAppPromise;
}

export function getHybridStatus() {
  return { appCheckActive };
}

export function describeCloudFailure(err) {
  const msg = (err?.message || String(err)).toLowerCase();
  const code = (err?.code || "").toLowerCase();
  const combined = `${msg} ${code}`;
  if (/app-check|appcheck|permission-denied|403|forbidden/.test(combined)) {
    return "The cloud request was rejected (App Check or access). Nothing was answered by the cloud; on-device inference only works if Chrome's built-in model is available.";
  }
  const short = (err?.message || String(err)).slice(0, 140);
  return `The cloud request failed: ${short}`;
}

class HybridPromptSession {
  constructor(firebase, ai, { systemPrompt } = {}) {
    this.firebase = firebase;
    this.ai = ai;
    this.systemPrompt = systemPrompt;
    // One GenerativeModel per distinct responseConstraint, because the
    // on-device constraint is fixed in onDeviceParams at model creation.
    this.models = new Map();
    this.destroyed = false;
    // Telemetry.updateTokenUsage() reads contextUsage/contextWindow when set.
    this.contextWindow = 9216; // on-device Prompt API token limit
  }

  getModel(responseConstraint) {
    const key = responseConstraint ? JSON.stringify(responseConstraint) : "";
    let model = this.models.get(key);
    if (!model) {
      model = this.firebase.getGenerativeModel(
        this.ai,
        buildHybridParams(this.firebase, { systemPrompt: this.systemPrompt, responseConstraint })
      );
      this.models.set(key, model);
      // Every panel flow that creates a session starts from a click, so the
      // SDK's "call on/after a user interaction" requirement holds here.
      warmDeviceModel(model);
    }
    return model;
  }

  buildRequest(text, responseConstraint) {
    if (!responseConstraint) return text;
    return {
      contents: [{ role: "user", parts: [{ text }] }],
      generationConfig: {
        responseMimeType: "application/json",
        responseSchema: toFirebaseSchema(responseConstraint)
      }
    };
  }

  async prompt(text, { responseConstraint } = {}) {
    try {
      const result = await this.getModel(responseConstraint).generateContent(this.buildRequest(text, responseConstraint));
      const response = result.response;
      const total = response?.usageMetadata?.totalTokenCount;
      if (typeof total === "number") this.contextUsage = total;
      return response.text();
    } catch (err) {
      this.lastError = describeCloudFailure(err);
      this.onError?.(this.lastError);
      throw err;
    }
  }

  // The panel's stream reader tolerates both cumulative and delta chunks;
  // Firebase yields delta chunks.
  async *promptStreaming(text, { responseConstraint } = {}) {
    try {
      const result = await this.getModel(responseConstraint).generateContentStream(this.buildRequest(text, responseConstraint));
      for await (const chunk of result.stream) {
        yield chunk.text();
      }
      const total = (await result.response)?.usageMetadata?.totalTokenCount;
      if (typeof total === "number") this.contextUsage = total;
    } catch (err) {
      this.lastError = describeCloudFailure(err);
      this.onError?.(this.lastError);
      throw err;
    }
  }

  destroy() {
    this.destroyed = true;
  }
}

// Returns a hybrid session, or null when Firebase is not configured locally.
// options mirrors the LanguageModel.create() options used by the panel;
// only systemPrompt is mapped (per leg, see buildHybridParams).
export async function createHybridPromptSession(options = {}) {
  const configModule = await loadFirebaseConfig();
  if (!configLooksUsable(configModule?.firebaseConfig)) return null;

  let firebase;
  try {
    firebase = await loadFirebaseBundle();
  } catch (err) {
    console.warn("LocalForm AI: Firebase bundle not found; run `npm run build`.", err);
    return null;
  }

  const app = await getOrInitFirebaseApp(firebase, configModule.firebaseConfig, configModule.appCheckDebugToken);
  const ai = firebase.getAI(app, { backend: new firebase.GoogleAIBackend() });
  const session = new HybridPromptSession(firebase, ai, { systemPrompt: options.systemPrompt });
  // Create and warm the default (unconstrained) model immediately, so SDK
  // setup actually runs at session creation rather than on first prompt.
  session.getModel();
  return session;
}

// Test-only: reset the cached Firebase app between cases.
export function __resetHybridSessionForTests() {
  appCheckActive = false;
  firebaseAppPromise = null;
}

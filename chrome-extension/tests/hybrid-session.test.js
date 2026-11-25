import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  createHybridPromptSession,
  buildHybridParams,
  configLooksUsable,
  toFirebaseSchema,
  __resetHybridSessionForTests
} from "../extension/lib/hybrid-session.js";
import { loadFirebaseBundle, loadFirebaseConfig } from "../extension/lib/firebase-loader.js";
import { SUGGESTION_SCHEMA } from "../extension/lib/prompt-guidance.js";
import { PROFILE_FACTS_SCHEMA } from "../extension/lib/profile-ingest.js";

vi.mock("../extension/lib/firebase-loader.js", () => ({
  loadFirebaseBundle: vi.fn(),
  loadFirebaseConfig: vi.fn()
}));

const VALID_CONFIG = {
  apiKey: "test-key",
  authDomain: "demo.firebaseapp.com",
  projectId: "demo-project",
  appId: "1:123:web:abc"
};

function fakeFirebase() {
  const generateContent = vi.fn(async () => ({
    response: {
      text: () => '{"ok":true}',
      usageMetadata: { totalTokenCount: 42 }
    }
  }));
  const chunks = [{ text: () => "he" }, { text: () => "llo" }];
  const generateContentStream = vi.fn(async () => ({
    stream: (async function* () { for (const c of chunks) yield c; })(),
    response: Promise.resolve({ usageMetadata: { totalTokenCount: 7 } })
  }));
  const models = [];
  const makeModel = () => {
    const m = { generateContent, generateContentStream, initializeDeviceModel: vi.fn(() => Promise.resolve()) };
    models.push(m);
    return m;
  };
  const model = makeModel();
  const getGenerativeModel = vi.fn(() => model);
  const firebase = {
    initializeApp: vi.fn(() => ({ name: "[DEFAULT]" })),
    initializeAppCheck: vi.fn(),
    getAI: vi.fn(() => ({})),
    GoogleAIBackend: class {},
    InferenceMode: { PREFER_ON_DEVICE: "PREFER_ON_DEVICE" },
    getGenerativeModel,
    __model: model
  };
  return firebase;
}

describe("configLooksUsable", () => {
  it("rejects missing, empty, or placeholder configs", () => {
    expect(configLooksUsable(null)).toBe(false);
    expect(configLooksUsable({})).toBe(false);
    expect(configLooksUsable({ apiKey: "", projectId: "p", appId: "a" })).toBe(false);
    expect(configLooksUsable({ apiKey: "YOUR_KEY", projectId: "p", appId: "a" })).toBe(false);
  });
  it("accepts a filled config", () => {
    expect(configLooksUsable(VALID_CONFIG)).toBe(true);
  });
});

describe("toFirebaseSchema", () => {
  it("maps JSON Schema to the Firebase subset with uppercase types", () => {
    const out = toFirebaseSchema(PROFILE_FACTS_SCHEMA);
    expect(out.type).toBe("ARRAY");
    expect(out.items.type).toBe("OBJECT");
    expect(out.items.properties.key.type).toBe("STRING");
    expect(out.items.properties.category.enum).toBe(PROFILE_FACTS_SCHEMA.items.properties.category.enum);
    expect(out.items.required).toEqual(["key", "proposedValue", "category"]);
  });
  it("drops unsupported JSON Schema keywords", () => {
    const out = toFirebaseSchema(SUGGESTION_SCHEMA);
    const item = out.properties.suggestions.items;
    expect(item.additionalProperties).toBeUndefined();
    expect(item.properties.guidance.maxLength).toBeUndefined();
  });
  it("handles empty and non-object input", () => {
    expect(toFirebaseSchema(undefined)).toBeUndefined();
    expect(toFirebaseSchema("nope")).toBeUndefined();
  });
});

describe("createHybridPromptSession", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    __resetHybridSessionForTests();
    delete self.FIREBASE_APPCHECK_DEBUG_TOKEN;
  });

  it("returns null when no local config file exists", async () => {
    loadFirebaseConfig.mockResolvedValue(null);
    expect(await createHybridPromptSession()).toBeNull();
    expect(loadFirebaseBundle).not.toHaveBeenCalled();
  });

  it("returns null for a placeholder config", async () => {
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: { apiKey: "", projectId: "", appId: "" } });
    expect(await createHybridPromptSession()).toBeNull();
    expect(loadFirebaseBundle).not.toHaveBeenCalled();
  });

  it("returns null when the bundle is missing", async () => {
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockRejectedValue(new Error("no vendor bundle"));
    expect(await createHybridPromptSession()).toBeNull();
  });

  it("initializes Firebase with App Check debug token and PREFER_ON_DEVICE", async () => {
    const firebase = fakeFirebase();
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG, appCheckDebugToken: "tok-1" });
    loadFirebaseBundle.mockResolvedValue(firebase);

    const session = await createHybridPromptSession({ systemPrompt: "SYS" });
    expect(session).not.toBeNull();
    expect(self.FIREBASE_APPCHECK_DEBUG_TOKEN).toBe("tok-1");
    expect(firebase.initializeApp).toHaveBeenCalledWith(VALID_CONFIG);
    expect(firebase.initializeAppCheck).toHaveBeenCalled();
    expect(firebase.getGenerativeModel).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        mode: "PREFER_ON_DEVICE",
        onDeviceParams: {
          createOptions: { initialPrompts: [{ role: "system", content: [{ type: "text", value: "SYS" }] }] }
        },
        inCloudParams: { model: "gemini-3.5-flash-lite", systemInstruction: "SYS" }
      })
    );
    // No ignored top-level systemInstruction (SDK 12.19 drops it with a warning).
    const params = firebase.getGenerativeModel.mock.calls[0][1];
    expect(params.systemInstruction).toBeUndefined();
    expect(firebase.__model.initializeDeviceModel).toHaveBeenCalled();
  });

  it("reuses one Firebase app across sessions", async () => {
    const firebase = fakeFirebase();
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockResolvedValue(firebase);
    await createHybridPromptSession();
    await createHybridPromptSession();
    expect(firebase.initializeApp).toHaveBeenCalledTimes(1);
  });

  it("prompt() maps responseConstraint to structured output and returns text", async () => {
    const firebase = fakeFirebase();
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockResolvedValue(firebase);
    const session = await createHybridPromptSession();

    const text = await session.prompt("fill this", { responseConstraint: SUGGESTION_SCHEMA });
    expect(text).toBe('{"ok":true}');
    const request = firebase.__model.generateContent.mock.calls[0][0];
    expect(request.contents[0].parts[0].text).toBe("fill this");
    expect(request.generationConfig.responseMimeType).toBe("application/json");
    expect(request.generationConfig.responseSchema.type).toBe("OBJECT");
    expect(session.contextUsage).toBe(42);
  });

  it("prompt() without a constraint sends a bare string", async () => {
    const firebase = fakeFirebase();
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockResolvedValue(firebase);
    const session = await createHybridPromptSession();
    await session.prompt("plain");
    expect(firebase.__model.generateContent).toHaveBeenCalledWith("plain");
  });

  it("promptStreaming yields delta chunks", async () => {
    const firebase = fakeFirebase();
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockResolvedValue(firebase);
    const session = await createHybridPromptSession();
    let out = "";
    for await (const chunk of session.promptStreaming("hi")) out += chunk;
    expect(out).toBe("hello");
    expect(session.contextUsage).toBe(7);
  });

  it("destroy() marks the session destroyed", async () => {
    const firebase = fakeFirebase();
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockResolvedValue(firebase);
    const session = await createHybridPromptSession();
    session.destroy();
    expect(session.destroyed).toBe(true);
  });

  it("creates one model per distinct responseConstraint with on-device constraint options", async () => {
    const firebase = fakeFirebase();
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockResolvedValue(firebase);
    const session = await createHybridPromptSession({ systemPrompt: "SYS" });
    expect(firebase.getGenerativeModel).toHaveBeenCalledTimes(1); // default model

    await session.prompt("fill this", { responseConstraint: SUGGESTION_SCHEMA });
    expect(firebase.getGenerativeModel).toHaveBeenCalledTimes(2);
    const constraintParams = firebase.getGenerativeModel.mock.calls[1][1];
    // On-device leg gets the RAW JSON Schema via promptOptions.responseConstraint.
    expect(constraintParams.onDeviceParams.promptOptions.responseConstraint).toBe(SUGGESTION_SCHEMA);
    expect(constraintParams.onDeviceParams.createOptions.initialPrompts[0].role).toBe("system");

    // Same constraint reuses the cached model; a plain prompt uses the default.
    await session.prompt("again", { responseConstraint: SUGGESTION_SCHEMA });
    await session.prompt("plain");
    expect(firebase.getGenerativeModel).toHaveBeenCalledTimes(2);
  });

  it("survives on-device warm-up rejection without an unhandled rejection", async () => {
    const firebase = fakeFirebase();
    firebase.__model.initializeDeviceModel.mockReturnValue(Promise.reject(new Error("no nano here")));
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    loadFirebaseBundle.mockResolvedValue(firebase);
    const session = await createHybridPromptSession();
    expect(session).not.toBeNull();
    await new Promise((r) => setTimeout(r, 0)); // let the rejection settle through our catch
  });

  it("buildHybridParams keeps the cloud default model and omits empty legs", () => {
    const firebase = { InferenceMode: { PREFER_ON_DEVICE: "PREFER_ON_DEVICE" } };
    expect(buildHybridParams(firebase, {})).toEqual({
      mode: "PREFER_ON_DEVICE",
      inCloudParams: { model: "gemini-3.5-flash-lite" } // always explicit: the SDK's built-in default is stale for new projects
    });
    const withSys = buildHybridParams(firebase, { systemPrompt: "SYS" });
    expect(withSys.inCloudParams).toEqual({ model: "gemini-3.5-flash-lite", systemInstruction: "SYS" });
    expect(withSys.onDeviceParams.promptOptions).toBeUndefined();
  });
});

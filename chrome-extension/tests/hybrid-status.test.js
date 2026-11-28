import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  createHybridPromptSession,
  describeCloudFailure,
  getHybridStatus,
  __resetHybridSessionForTests
} from "../extension/lib/hybrid-session.js";
import { loadFirebaseBundle, loadFirebaseConfig } from "../extension/lib/firebase-loader.js";

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

function makeFakeFirebase({ appCheckThrows = false } = {}) {
  const generateContent = vi.fn(async () => ({
    response: {
      text: () => '{"ok":true}',
      usageMetadata: { totalTokenCount: 42 }
    }
  }));
  const generateContentStream = vi.fn(async () => ({
    stream: (async function* () {})(),
    response: Promise.resolve({ usageMetadata: { totalTokenCount: 7 } })
  }));
  const model = {
    generateContent,
    generateContentStream,
    initializeDeviceModel: vi.fn(() => Promise.resolve())
  };
  const firebase = {
    initializeApp: vi.fn(() => ({ name: "[DEFAULT]" })),
    initializeAppCheck: vi.fn(() => {
      if (appCheckThrows) throw new Error("App Check init failed");
    }),
    getAI: vi.fn(() => ({})),
    GoogleAIBackend: class {},
    InferenceMode: { PREFER_ON_DEVICE: "PREFER_ON_DEVICE" },
    getGenerativeModel: vi.fn(() => model),
    __model: model
  };
  return firebase;
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetHybridSessionForTests();
});

describe("getHybridStatus", () => {
  it("reports appCheckActive false when initializeAppCheck throws", async () => {
    loadFirebaseBundle.mockResolvedValue(makeFakeFirebase({ appCheckThrows: true }));
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    const hybrid = await createHybridPromptSession(VALID_CONFIG);
    expect(hybrid).not.toBeNull();
    expect(getHybridStatus().appCheckActive).toBe(false);
  });

  it("reports appCheckActive true when initializeAppCheck succeeds", async () => {
    loadFirebaseBundle.mockResolvedValue(makeFakeFirebase());
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    const hybrid = await createHybridPromptSession(VALID_CONFIG);
    expect(hybrid).not.toBeNull();
    expect(getHybridStatus().appCheckActive).toBe(true);
  });
});

describe("describeCloudFailure", () => {
  it("handles permission-denied errors", () => {
    const err = new Error("permission-denied: access denied");
    const msg = describeCloudFailure(err);
    expect(msg).toBe("The cloud request was rejected (App Check or access). Nothing was answered by the cloud; on-device inference only works if Chrome's built-in model is available.");
  });

  it("handles generic errors", () => {
    const err = new Error("some random failure");
    const msg = describeCloudFailure(err);
    expect(msg).toBe(`The cloud request failed: some random failure`);
    expect(msg.length).toBeLessThanOrEqual(140);
  });
});

describe("HybridPromptSession.prompt", () => {
  it("rethrows original error and calls onError", async () => {
    const firebase = makeFakeFirebase();
    loadFirebaseBundle.mockResolvedValue(firebase);
    loadFirebaseConfig.mockResolvedValue({ firebaseConfig: VALID_CONFIG });
    const hybrid = await createHybridPromptSession(VALID_CONFIG);
    const originalError = new Error("network error");
    firebase.__model.generateContent.mockRejectedValue(originalError);
    const onError = vi.fn();
    hybrid.onError = onError;
    await expect(hybrid.prompt("test")).rejects.toThrow(originalError);
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0][0]).toBe("The cloud request failed: network error");
    expect(hybrid.lastError).toBe("The cloud request failed: network error");
  });
});

describe("createHybridPromptSession", () => {
  it("returns null without config", async () => {
    loadFirebaseConfig.mockResolvedValue(null);
    const hybrid = await createHybridPromptSession({});
    expect(hybrid).toBeNull();
  });
});

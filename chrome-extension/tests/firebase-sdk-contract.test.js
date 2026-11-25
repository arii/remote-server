// Contract tests against the REAL installed Firebase AI Logic SDK
// (firebase@12.19.0 -> @firebase/ai), not a mock of its shape. These pin the
// hybrid getGenerativeModel() contract this integration depends on, so an SDK
// upgrade that changes HybridParams semantics fails loudly here.
import { describe, it, expect, afterAll } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { initializeApp, deleteApp } from "firebase/app";
import { getAI, getGenerativeModel, GoogleAIBackend, InferenceMode } from "firebase/ai";
import { buildHybridParams } from "../extension/lib/hybrid-session.js";

const require = createRequire(import.meta.url);
const firebaseVersion = require("firebase/package.json").version;

const app = initializeApp(
  { apiKey: "contract-test", authDomain: "demo.firebaseapp.com", projectId: "demo-contract", appId: "1:0:web:contract" },
  "sdk-contract-test"
);
const ai = getAI(app, { backend: new GoogleAIBackend() });
// buildHybridParams only needs InferenceMode off the namespace; pass the real one.
const realFirebase = { InferenceMode };

afterAll(async () => {
  await deleteApp(app);
});

describe("firebase/ai hybrid contract (installed SDK " + firebaseVersion + ")", () => {
  it("HybridParams in the installed .d.ts has exactly mode/onDeviceParams/inCloudParams", () => {
    // @firebase/ai's exports map hides the .d.ts subpath; locate it via the
    // resolved firebase/ai entry inside node_modules instead.
    const aiEntry = require.resolve("firebase/ai");
    const nm = aiEntry.slice(0, aiEntry.lastIndexOf("node_modules") + "node_modules".length);
    const dts = readFileSync(path.join(nm, "@firebase", "ai", "dist", "ai-public.d.ts"), "utf8");
    const block = dts.match(/interface HybridParams \{([\s\S]*?)\n\}/);
    expect(block, "HybridParams interface not found in installed @firebase/ai types").toBeTruthy();
    const keys = [...block[1].matchAll(/^    (\w+)\??:/gm)].map((m) => m[1]).sort();
    expect(keys).toEqual(["inCloudParams", "mode", "onDeviceParams"]);
  });

  it("real getGenerativeModel applies inCloudParams.systemInstruction to the model", () => {
    const params = buildHybridParams(realFirebase, { systemPrompt: "SYS-CONTRACT" });
    expect(params.mode).toBe(InferenceMode.PREFER_ON_DEVICE);
    const model = getGenerativeModel(ai, params);
    const si = model.systemInstruction;
    expect(si, "systemInstruction from inCloudParams must reach the model").toBeTruthy();
    expect(si.parts[0].text).toBe("SYS-CONTRACT");
  });

  it("real getGenerativeModel IGNORES a top-level systemInstruction (the v3 bug)", () => {
    // The SDK warns and drops it; assert the drop so this test fails if a
    // future SDK starts honoring top-level params in hybrid mode.
    const model = getGenerativeModel(ai, {
      mode: InferenceMode.PREFER_ON_DEVICE,
      systemInstruction: "SYS-TOP-LEVEL"
    });
    expect(model.systemInstruction).toBeUndefined();
  });

  it("real getGenerativeModel uses our explicit model; SDK built-in default is documented stale", () => {
    // SDK fact: with no inCloudParams it falls back to gemini-2.5-flash-lite,
    // which newer projects cannot call (live 404 verified 2026-11-24). Our
    // builder must therefore ALWAYS set inCloudParams.model.
    const fallback = getGenerativeModel(ai, { mode: InferenceMode.PREFER_ON_DEVICE });
    expect(fallback.model).toContain("gemini-2.5-flash-lite");
    const explicit = getGenerativeModel(ai, buildHybridParams(realFirebase, { systemPrompt: "S" }));
    expect(explicit.model).toContain("gemini-3.5-flash-lite");
    const bare = getGenerativeModel(ai, buildHybridParams(realFirebase, {}));
    expect(bare.model).toContain("gemini-3.5-flash-lite");
  });
});

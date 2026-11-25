// esbuild entry point for the vendored Firebase AI Logic bundle.
// Only what hybrid-session.js uses, so the bundle stays small.
export { initializeApp } from "firebase/app";
export { initializeAppCheck } from "firebase/app-check";
export { getAI, getGenerativeModel, GoogleAIBackend, InferenceMode } from "firebase/ai";

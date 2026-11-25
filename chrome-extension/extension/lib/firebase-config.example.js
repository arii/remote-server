// ============================================================================
// LocalForm AI - Firebase AI Logic configuration template (#BuildHybridAI)
// ============================================================================
// The extension runs hybrid inference through Firebase AI Logic: on-device
// Gemini Nano (Chrome Prompt API) first, with seamless cloud fallback through
// the Gemini Developer API (free tier, no billing required).
//
// SETUP (about 5 minutes):
//   1. Copy this file to `firebase-config.js` in this same directory.
//      That file is gitignored and must never be committed.
//   2. In the Firebase console (https://console.firebase.google.com), create
//      a project, then go to AI Services > AI Logic > Get started and choose
//      the "Gemini Developer API" provider (no-cost).
//   3. Register a Web app in the project and paste its config values below.
//   4. App Check is enforced automatically for AI Logic. For development and
//      hackathon judging (debug tokens are accepted by the rules), set
//      appCheckDebugToken below to a debug token registered in the Firebase
//      console under Security > App Check > Apps > Manage debug tokens.
//      To mint one, run the extension once with appCheckDebugToken: "true"
//      and copy the token Chrome logs to the console, then register it.
//      Debug tokens stay local to your machine and are never shared.
//   5. Re-run `npm run build` and reload the unpacked extension.
//
// Without this file the extension still works fully on-device through
// Chrome's built-in LanguageModel/Summarizer APIs; only the cloud fallback
// leg of hybrid inference is disabled. That no-config mode is also what a
// judge gets from the submission zip: the hackathon rules forbid production
// API keys, live Google Cloud project secrets, and debug tokens in the repo,
// the submission form, AND the testing instructions, so credentials are never
// shared. A judge who wants to exercise the cloud fallback can paste config
// values from their own free Firebase project into their local copy of this file.
//
// Never commit real config values or debug tokens.

export const firebaseConfig = {
  apiKey: "",
  authDomain: "",
  projectId: "",
  appId: ""
};

// Debug token string, or the literal string "true" to mint a new one on the
// next run (printed to the side panel's DevTools console). Empty disables
// App Check debug mode; on-device inference still works without it.
export const appCheckDebugToken = "";

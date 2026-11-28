// ============================================================================
// LocalForm AI - side panel view state (pure logic)
// Decides what the AI Model and Observer tabs show. Kept out of sidepanel.js
// (no test harness) so the show/hide rules are unit-testable.
// ============================================================================

const NAMES = { languageModel: "Prompt", summarizer: "Summarizer", writer: "Writer" };
const KEYS = Object.keys(NAMES);

// report: {languageModel, summarizer, writer} availability strings, or null
// while checking. Strings: "available", "downloadable", "downloading",
// "unavailable".
export function modelPanelView(report) {
  if (!report) {
    return { phase: "checking", allReady: false, promptReady: false, showProgress: false, showDownloadButton: false, summary: "Checking on-device models...", ctaText: "Checking on-device models..." };
  }
  const states = KEYS.map((k) => report[k]);
  const allReady = states.every((s) => s === "available");
  const promptReady = report.languageModel === "available";
  const downloading = states.includes("downloading");
  const downloadable = states.includes("downloadable");
  const showDownloadButton = downloading || downloadable;
  const notReady = KEYS.filter((k) => report[k] !== "available");
  let phase = "ready";
  if (showDownloadButton) phase = "download";
  else if (!allReady) phase = "blocked";

  let summary = "All 3 on-device models are ready.";
  if (!allReady) summary = `Not ready: ${notReady.map((k) => `${NAMES[k]} (${report[k] || "unknown"})`).join(", ")}.`;

  let ctaText = "";
  if (!promptReady) {
    if (report.languageModel === "downloading") ctaText = "Downloading... Ask and prompt tools unlock when the Prompt model is ready.";
    else if (report.languageModel === "downloadable") ctaText = "Download the built-in models above to use Ask and prompt tools.";
    else ctaText = "The Prompt model is unavailable in this Chrome. See Model details.";
  }
  return { phase, allReady, promptReady, showProgress: !allReady, showDownloadButton, summary, ctaText };
}

// Hostname of a regular web page, or "" for anything else. chrome-extension://
// pages parse with the extension ID as their hostname, so only http(s) counts.
export function webHostname(url) {
  try {
    const u = new URL(url || "");
    return u.protocol === "http:" || u.protocol === "https:" ? u.hostname : "";
  } catch {
    return "";
  }
}

// Start/stop button state for the Observer tab.
export function observeView({ hostname, enabled } = {}) {
  if (!hostname) {
    return { label: "Open a website to start observing", disabled: true, pressed: false, hint: "Observer works on regular websites, not browser pages." };
  }
  if (enabled) {
    return { label: `Stop observing ${hostname}`, disabled: false, pressed: true, hint: "Edits you make in forms on this site are being recorded for review." };
  }
  return { label: `Start observing ${hostname}`, disabled: false, pressed: false, hint: "Record your form edits on this site so you can review them for your vault." };
}

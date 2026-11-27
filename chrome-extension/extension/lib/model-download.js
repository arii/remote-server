// Pure state logic for built-in model download progress (Prompt, Summarizer, Writer).

export const MODEL_APIS = [
  { key: "languageModel", label: "Prompt", global: "LanguageModel" },
  { key: "summarizer", label: "Summarizer", global: "Summarizer" },
  { key: "writer", label: "Writer", global: "Writer" }
];

/**
 * Map Chrome availability() plus an optional live percent to a display state.
 * pct is 100 only when availability is "available"; a live download is capped at 99.
 */
export function modelProgressState(availability, pct = null) {
  if (availability === "available") return { state: "ready", pct: 100, label: "Ready" };
  if (availability === "downloadable") return { state: "downloadable", pct: 0, label: "Not downloaded" };
  if (availability === "downloading") {
    const known = Number.isFinite(pct) ? Math.max(0, Math.min(99, Math.round(pct))) : null;
    return { state: "downloading", pct: known, label: known === null ? "Downloading..." : `Downloading ${known}%` };
  }
  return { state: "unavailable", pct: null, label: "Unavailable" };
}

/** Returns a Chrome `monitor` callback that reports integer percent (0..100) from downloadprogress events. */
export function makeDownloadMonitor(onPct) {
  return (m) => {
    m.addEventListener("downloadprogress", (e) => {
      const loaded = Number(e.loaded);
      const pct = Number.isFinite(loaded) ? Math.round(Math.max(0, Math.min(1, loaded)) * 100) : 0;
      if (onPct) onPct(pct);
    });
  };
}

export const DISK_NOTE = "Chrome needs about 22 GB free on the profile volume for Gemini Nano, and removes the model if free space drops below 10 GB after download. Free space and retry.";
export const INTERNALS_URL = "chrome://on-device-internals";

/**
 * Turn a failed create() error into a displayable detail. A cause is only
 * named when the error name/message supports it; otherwise cause is "unknown"
 * and the raw name and message are shown. Chrome documents no disk-space
 * specific error, so disk is only claimed when the error itself says so.
 * availability is the re-checked value after the failure (optional).
 */
export function describeModelError(err, { availability } = {}) {
  const name = String(err?.name || "Error");
  const message = String(err?.message ?? err ?? "");
  const text = `${name} ${message}`;
  let cause = "unknown";
  let hint = "Cause not identified from the error. Open chrome://on-device-internals for the model status and logs.";
  const steps = [];
  if (name === "NotAllowedError") {
    cause = "permission";
    hint = "Chrome blocked the download: it needs a user gesture (click the button again) or the feature is not permitted.";
  } else if (name === "QuotaExceededError" || /disk|space|storage|quota/i.test(text)) {
    cause = "disk";
    hint = "The error points to insufficient disk space.";
    steps.push(DISK_NOTE);
  } else if (name === "NotSupportedError") {
    cause = "unsupported";
    hint = "The device or requested options are not supported by this Chrome build.";
  } else if (name === "NetworkError" || /network|fetch|offline/i.test(text)) {
    cause = "network";
    hint = "The download looks like a network failure. Check the connection and retry.";
  } else if (availability === "unavailable") {
    cause = "unavailable";
    hint = "Chrome reports the model unavailable: the device or options are unsupported, possibly insufficient disk space or hardware. Chrome does not say which.";
    steps.push(DISK_NOTE);
  }
  steps.push("Check model status and logs at chrome://on-device-internals. Stable extension Prompt API needs no experimental flags; check hardware, requested options and model download status. Other APIs, including Writer, have separate availability.");
  return { name, message, cause, hint, steps };
}

/** Plain-text rendering of describeModelError output. */
export function formatModelError(d) {
  return `${d.name}: ${d.message}\n${d.hint}\n${d.steps.map((s) => `- ${s}`).join("\n")}`;
}

/**
 * Collapses repeated error texts into a single full message followed by shorthand notes.
 * @param {Array<{key: string, label: string, text: string}>} items - Display‑ordered items.
 * @returns {Array<{key: string, text: string}>} Collapsed list; empty text entries are kept empty.
 */
export function collapseRepeatedErrors(items) {
  const seen = new Map();
  return items.map(({ key, label, text }) => {
    if (!text) return { key, text: "" };
    if (seen.has(text)) {
      return { key, text: `Same as ${seen.get(text)} above.` };
    }
    seen.set(text, label);
    return { key, text };
  });
}

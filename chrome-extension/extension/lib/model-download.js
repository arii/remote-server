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

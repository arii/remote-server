import { redactSecrets } from "./model-download.js";

export function buildTraceEntry({ type, title, durationMs, details }, now = new Date()) {
  const id = now.getTime() + Math.random();
  const ts = now.toLocaleTimeString();
  const duration = Math.round(durationMs || 0);
  const titleText = redactSecrets(String(title ?? ""));
  const detailsText =
    typeof details === "string"
      ? redactSecrets(details)
      : redactSecrets(JSON.stringify(details, null, 2) ?? "");

  return {
    id,
    ts,
    type,
    title: titleText,
    durationMs: duration,
    details: detailsText,
  };
}

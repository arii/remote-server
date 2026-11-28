// ============================================================================
// LocalForm AI - Drive appDataFolder sync (pure logic, #1709)
// One JSON file, localform-sync.json, in the user's Drive appDataFolder holds
// the learned state and vault so a second browser can pick it up. No server.
// Pull, merge, write local, then push. Merge is per entry: newest updatedAt
// wins, deletions carry a tombstone, mapping hit counts take the max.
//
// Pure: token source and fetch are injected (same shape as drive-import.js),
// and local storage access is supplied by the caller. No chrome.* APIs here.
// ============================================================================

import { DRIVE_API, DRIVE_UPLOAD_API, driveGet } from "./drive-import.js";
import { MAX_LEARNED_LOG } from "./learning.js";
import { MAX_MAPPINGS } from "./mappings.js";
import { MAX_FILL_OUTCOMES } from "./outcomes.js";
import { normalizeGuidance } from "./prompt-guidance.js";
import { PROMPT_IDS } from "./prompts.js";

export const DRIVE_APPDATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata";
export const SYNC_FILE_NAME = "localform-sync.json";
export const SYNC_SCHEMA_VERSION = 1;
export const MAX_TOMBSTONES = 500;
// chrome.storage.local keys the sync reads and writes.
export const SYNC_STORAGE_KEYS = [
  "userContextVault",
  "pendingDeltas",
  "learnedMappings",
  "learnedAliases",
  "promptGuidance",
  "fillOutcomes",
  "syncTombstones"
];

const MESSAGES = {
  "not-signed-in": "Sign in to Google to sync, then press Sync with Google Drive again.",
  "scope-denied": "Sync needs permission to use the app data folder in your Drive. Allow it when Google asks, then try again.",
  "api-disabled": "The Google Drive API is not turned on for this extension's Google Cloud project.",
  quota: "Your Google Drive storage is full.",
  "rate-limited": "Google Drive is limiting requests right now.",
  "drive-unavailable": "Google Drive had a server problem.",
  forbidden: "Google Drive refused the request.",
  network: "Could not reach Google Drive. Check your connection and try again.",
  "schema-mismatch": "The synced file in Drive was written by a different version of LocalForm AI. Update the extension, then try again. Nothing was changed.",
  failed: "Google Drive sync failed."
};

// One plain next step per failure class, shown under the summary.
const NEXT = {
  "not-signed-in": "Press Sync with Google Drive again and finish the Google sign-in.",
  "scope-denied": "Press Sync again and allow access to the app data folder when Google asks.",
  "api-disabled": "In Google Cloud console, enable the Google Drive API for the project that owns the OAuth client ID, wait a minute, then sync again.",
  quota: "Free up space in your Google Drive (or Google One storage), then sync again.",
  "rate-limited": "Wait a minute, then press Sync again.",
  "drive-unavailable": "Wait a few minutes, then press Sync again.",
  forbidden: "Check the Google message above and the OAuth consent screen setup (test users, scopes), then sync again.",
  network: "Check your connection or any VPN or firewall, then press Sync again.",
  "schema-mismatch": "Update the extension on this browser, then sync again. Nothing was changed.",
  failed: "Press Sync again. If it keeps failing, open the Observer tab and copy the ERROR trace."
};

const cap = (v, n) => (typeof v === "string" ? v.slice(0, n) : "");

export class DriveSyncError extends Error {
  // info: {status, reason, googleMessage, cause, step}. Only strings and the
  // HTTP status are kept, never tokens, headers or file contents.
  constructor(code, detail, info = {}) {
    super(detail ? `${MESSAGES[code]} (${detail})` : MESSAGES[code]);
    this.name = "DriveSyncError";
    this.code = code;
    this.detail = detail || "";
    this.status = Number.isInteger(info.status) ? info.status : 0;
    this.reason = cap(info.reason, 80);
    this.googleMessage = cap(info.googleMessage, 300);
    this.cause = cap(info.cause, 80);
    this.step = info.step || "";
    this.next = NEXT[code];
  }
}

const REASONS = {
  scope: /^(insufficientPermissions|ACCESS_TOKEN_SCOPE_INSUFFICIENT)$/,
  api: /^(accessNotConfigured|SERVICE_DISABLED|API_KEY_SERVICE_BLOCKED)$/,
  quota: /^(storageQuotaExceeded|quotaExceeded)$/,
  rate: /^(rateLimitExceeded|userRateLimitExceeded|dailyLimitExceeded|RATE_LIMIT_EXCEEDED)$/
};

function httpCode(status, reason, googleMessage) {
  if (status === 401) return "not-signed-in";
  if (status === 429) return "rate-limited";
  if (status >= 500) return "drive-unavailable";
  if (status === 403) {
    if (REASONS.scope.test(reason)) return "scope-denied";
    if (REASONS.api.test(reason) || /has not been used in project|SERVICE_DISABLED/i.test(googleMessage)) return "api-disabled";
    if (REASONS.quota.test(reason)) return "quota";
    if (REASONS.rate.test(reason)) return "rate-limited";
    return "forbidden";
  }
  return "failed";
}

function classify(err, step = "") {
  if (err instanceof DriveSyncError) {
    if (!err.step) err.step = step;
    return err;
  }
  const status = Number.isInteger(err?.status) ? err.status : 0;
  const info = { status, reason: err?.reason, googleMessage: err?.googleMessage, cause: err?.name || "Error", step };
  if (status) {
    const code = httpCode(status, info.reason || "", info.googleMessage || "");
    return new DriveSyncError(code, `HTTP ${status}${info.reason ? `, ${info.reason}` : ""}`, info);
  }
  return new DriveSyncError("failed", `${info.cause}: ${cap(err?.message, 200) || "no message"}`, info);
}

// Wraps injected deps so every failure maps to one explicit DriveSyncError.
// ctx.step is kept current by syncWithDrive so HTTP and network failures say
// which stage they happened in.
function guardDeps({ getToken, removeToken, fetchFn }, ctx) {
  return {
    removeToken,
    getToken: async () => {
      try {
        return await getToken();
      } catch (err) {
        const message = cap(err?.message, 200);
        const code = /scope|not granted|revoked|denied|did not approve/i.test(message) ? "scope-denied" : "not-signed-in";
        throw new DriveSyncError(code, message ? `${err?.name || "Error"}: ${message}` : "", {
          cause: err?.name || "Error",
          step: "sign-in"
        });
      }
    },
    fetchFn: async (url, init) => {
      try {
        return await fetchFn(url, init);
      } catch (err) {
        const name = err?.name || "Error";
        throw new DriveSyncError("network", `${name}: ${cap(err?.message, 200) || "no message"}`, { cause: name, step: ctx.step });
      }
    }
  };
}

// Plain, JSON-safe description of any sync failure for the panel and traces.
export function errorInfo(err) {
  const e = classify(err);
  return {
    code: e.code,
    name: e.cause || e.name,
    status: e.status,
    reason: e.reason,
    googleMessage: e.googleMessage,
    detail: e.detail,
    step: e.step,
    summary: MESSAGES[e.code],
    next: e.next
  };
}

export function formatSyncFailure(info) {
  const google = [info.reason, info.googleMessage].filter(Boolean).join(": ");
  const parts = [
    `${info.name || "Error"} (${info.code})`,
    info.step ? `during ${info.step}` : "",
    info.status ? `HTTP ${info.status}` : "",
    google || info.detail
  ].filter(Boolean);
  return [`Sync failed: ${info.summary}`, `Details: ${parts.join(", ")}`, `Next: ${info.next}`].join("\n");
}

// --- Merge -------------------------------------------------------------------

function toMs(v) {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  const t = typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? t : 0;
}

const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const list = (v) => (Array.isArray(v) ? v.filter(isObj) : []);
const str = (v) => (typeof v === "string" ? v : "");
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);

// Whitelists: only these fields ever leave the device. Vault entries are the
// one place values are allowed; everything else holds ids, keys and counters.
const cleanMapping = (m) => ({
  sig: str(m.sig),
  entryId: str(m.entryId),
  entryKey: str(m.entryKey),
  hosts: Array.isArray(m.hosts) ? m.hosts.filter((h) => typeof h === "string") : [],
  hits: num(m.hits),
  cleanUses: num(m.cleanUses),
  status: m.status === "confirmed" ? "confirmed" : "quarantined",
  lastSeen: num(m.lastSeen)
});
const cleanAlias = (a) => ({
  label: str(a.label),
  alias: str(a.alias),
  entryId: str(a.entryId),
  entryKey: str(a.entryKey),
  ts: num(a.ts)
});
const cleanOutcome = (o) => ({
  fillId: str(o.fillId),
  ts: num(o.ts),
  host: str(o.host),
  sig: {
    label: str(o.sig?.label),
    name: str(o.sig?.name),
    autocomplete: str(o.sig?.autocomplete),
    type: str(o.sig?.type)
  },
  entryKey: typeof o.entryKey === "string" ? o.entryKey : null,
  pass: str(o.pass),
  confidence: typeof o.confidence === "number" ? o.confidence : null,
  outcome: str(o.outcome),
  remappedEntryId: typeof o.remappedEntryId === "string" ? o.remappedEntryId : null
});
const cleanTombstone = (t) => ({ kind: str(t.kind), id: str(t.id), deletedAt: toMs(t.deletedAt) });

export const outcomeId = (o) => `${o.fillId}|${o.sig.label}|${o.sig.name}|${o.sig.autocomplete}|${o.sig.type}`;
export const aliasId = (a) => `${a.entryId}|${a.alias}`;

// Guidance text per target ("global" or a prompt id) with the time of its last
// edit, taken from the edit history. Targets that were never set are omitted.
function guidanceEntries(raw) {
  const g = normalizeGuidance(raw);
  const out = [];
  for (const id of ["global", ...PROMPT_IDS]) {
    const text = id === "global" ? g.global : g.perPrompt[id] || "";
    let updatedAt = 0;
    for (const h of g.history) {
      const before = id === "global" ? h?.before?.global : h?.before?.perPrompt?.[id];
      const after = id === "global" ? h?.after?.global : h?.after?.perPrompt?.[id];
      if ((before || "") !== (after || "")) updatedAt = Math.max(updatedAt, toMs(h.at));
    }
    if (text || updatedAt) out.push({ id, text, updatedAt });
  }
  return out;
}

// Newest entry per id wins; ties keep local. A tombstone drops an entry unless
// the entry was changed after the deletion. Returns the survivors in local
// order (then new remote entries) and the ids that survived.
function mergeEntries(kind, localList, remoteList, tombs, { id, ts }) {
  const byId = new Map();
  for (const e of [...localList, ...remoteList]) {
    const key = id(e);
    if (!key) continue;
    const cur = byId.get(key);
    if (!cur) byId.set(key, { winner: e, versions: [e] });
    else {
      cur.versions.push(e);
      if (ts(e) > ts(cur.winner)) cur.winner = e;
    }
  }
  const dead = new Map(tombs.filter((t) => t.kind === kind).map((t) => [t.id, t.deletedAt]));
  const merged = [];
  const survivors = new Set();
  for (const [key, { winner, versions }] of byId) {
    if (dead.has(key) && dead.get(key) >= ts(winner)) continue;
    survivors.add(key);
    merged.push({ winner, versions });
  }
  return { merged, survivors };
}

function dedupeTombstones(all) {
  const best = new Map();
  for (const t of all) {
    if (!t.kind || !t.id) continue;
    const k = `${t.kind}|${t.id}`;
    if (!best.has(k) || t.deletedAt > best.get(k).deletedAt) best.set(k, t);
  }
  return [...best.values()].sort((a, b) => b.deletedAt - a.deletedAt).slice(0, MAX_TOMBSTONES);
}

// local: chrome.storage.local values. remote: validated payload or null.
// Returns the merged state in storage shape (guidance as the stored object).
export function mergeStates(local, remote) {
  const r = remote || {};
  const tombs = dedupeTombstones([...list(local.syncTombstones).map(cleanTombstone), ...list(r.tombstones).map(cleanTombstone)]);

  const vault = mergeEntries("vault", list(local.userContextVault), list(r.userContextVault), tombs, {
    id: (e) => str(e.id),
    ts: (e) => toMs(e.updatedAt)
  });

  const mappings = mergeEntries("mapping", list(local.learnedMappings).map(cleanMapping), list(r.learnedMappings).map(cleanMapping), tombs, {
    id: (m) => m.sig,
    ts: (m) => m.lastSeen
  });
  const learnedMappings = mappings.merged
    .map(({ winner, versions }) => ({
      ...winner,
      status: (() => {
        const tomb = tombs.find((t) => t.kind === "mapping" && t.id === winner.sig);
        return versions.some((v) => v.status === "confirmed" && (!tomb || v.lastSeen > tomb.deletedAt))
          ? "confirmed"
          : winner.status;
      })(),
      hits: Math.max(...versions.map((v) => v.hits)),
      cleanUses: Math.max(...versions.map((v) => v.cleanUses))
    }))
    .sort((a, b) => b.lastSeen - a.lastSeen)
    .slice(0, MAX_MAPPINGS);

  const aliases = mergeEntries("alias", list(local.learnedAliases).map(cleanAlias), list(r.learnedAliases).map(cleanAlias), tombs, {
    id: aliasId,
    ts: (a) => a.ts
  });
  const learnedAliases = aliases.merged
    .map((m) => m.winner)
    .sort((a, b) => b.ts - a.ts)
    .slice(0, MAX_LEARNED_LOG);

  const guidance = mergeEntries("guidance", guidanceEntries(local.promptGuidance), list(r.promptGuidance), [], {
    id: (e) => str(e.id),
    ts: (e) => toMs(e.updatedAt)
  }).merged.map((m) => m.winner);
  const base = normalizeGuidance(local.promptGuidance);
  const text = (id) => str(guidance.find((g) => g.id === id)?.text);
  const perPrompt = {};
  for (const id of PROMPT_IDS) if (text(id)) perPrompt[id] = text(id);
  const promptGuidance = normalizeGuidance({ global: text("global"), perPrompt, history: base.history });

  const seen = new Map();
  for (const o of [...list(local.fillOutcomes), ...list(r.fillOutcomes)].map(cleanOutcome)) {
    if (o.fillId && !seen.has(outcomeId(o))) seen.set(outcomeId(o), o);
  }
  const fillOutcomes = [...seen.values()].sort((a, b) => a.ts - b.ts).slice(-MAX_FILL_OUTCOMES);

  // A tombstone is only kept while it still hides something or the entry is
  // gone everywhere; one beaten by a newer entry is dropped.
  const live = {
    vault: vault.survivors,
    mapping: mappings.survivors,
    alias: aliases.survivors
  };
  const syncTombstones = tombs.filter((t) => !live[t.kind]?.has(t.id));

  return {
    userContextVault: vault.merged.map((m) => ({ ...m.winner })),
    learnedMappings,
    learnedAliases,
    promptGuidance,
    fillOutcomes,
    syncTombstones
  };
}

// Tombstones for ids present in `before` but gone from `after`.
export function addTombstones(existing, kind, beforeList, afterList, idOf, now = Date.now()) {
  const kept = new Set((afterList || []).map(idOf));
  const gone = (beforeList || []).map(idOf).filter((id) => id && !kept.has(id));
  if (gone.length === 0) return Array.isArray(existing) ? existing : [];
  return dedupeTombstones([...list(existing).map(cleanTombstone), ...gone.map((id) => ({ kind, id, deletedAt: now }))]);
}

// The file body. Built from a whitelist so nothing but the vault carries values.
export function buildPayload(state, now = Date.now()) {
  return {
    schemaVersion: SYNC_SCHEMA_VERSION,
    updatedAt: new Date(now).toISOString(),
    userContextVault: list(state.userContextVault),
    learnedMappings: list(state.learnedMappings).map(cleanMapping),
    learnedAliases: list(state.learnedAliases).map(cleanAlias),
    promptGuidance: guidanceEntries(state.promptGuidance),
    fillOutcomes: list(state.fillOutcomes).map(cleanOutcome),
    tombstones: list(state.syncTombstones).map(cleanTombstone)
  };
}

export function parsePayload(text) {
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw new DriveSyncError("schema-mismatch");
  }
  if (!isObj(body) || body.schemaVersion !== SYNC_SCHEMA_VERSION) throw new DriveSyncError("schema-mismatch");
  return body;
}

// --- Drive I/O ---------------------------------------------------------------

export function buildFindUrl() {
  const params = new URLSearchParams({
    spaces: "appDataFolder",
    q: `name='${SYNC_FILE_NAME}' and trashed=false`,
    fields: "files(id,name)",
    pageSize: "1"
  });
  return `${DRIVE_API}/files?${params}`;
}

export async function pullRemote(deps) {
  const found = await (await driveGet(buildFindUrl(), deps)).json();
  const file = (found.files || [])[0];
  if (!file?.id) return { fileId: "", payload: null };
  const res = await driveGet(`${DRIVE_API}/files/${encodeURIComponent(file.id)}?alt=media`, deps);
  return { fileId: file.id, payload: parsePayload(await res.text()) };
}

const BOUNDARY = "localform_sync_boundary";

export async function pushRemote(deps, fileId, payload) {
  const body = JSON.stringify(payload);
  if (fileId) {
    await driveGet(`${DRIVE_UPLOAD_API}/files/${encodeURIComponent(fileId)}?uploadType=media`, deps, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body
    });
    return fileId;
  }
  const meta = JSON.stringify({ name: SYNC_FILE_NAME, parents: ["appDataFolder"], mimeType: "application/json" });
  const multipart =
    `--${BOUNDARY}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${meta}\r\n` +
    `--${BOUNDARY}\r\nContent-Type: application/json\r\n\r\n${body}\r\n--${BOUNDARY}--`;
  const res = await driveGet(`${DRIVE_UPLOAD_API}/files?uploadType=multipart&fields=id`, deps, {
    method: "POST",
    headers: { "Content-Type": `multipart/related; boundary=${BOUNDARY}` },
    body: multipart
  });
  return (await res.json()).id || "";
}

// Pull, merge into local (applyRemote runs inside the worker's mutation queue
// and returns the payload to upload), then push. Every failure is a
// DriveSyncError with a plain message; nothing is swallowed.
export async function syncWithDrive(rawDeps, { applyRemote, now = Date.now }) {
  const ctx = { step: "pull" };
  const deps = guardDeps(rawDeps, ctx);
  try {
    const { fileId, payload } = await pullRemote(deps);
    ctx.step = "merge";
    const merged = await applyRemote(payload);
    ctx.step = "push";
    await pushRemote(deps, fileId, merged);
    return { syncedAt: now() };
  } catch (err) {
    throw classify(err, ctx.step);
  }
}

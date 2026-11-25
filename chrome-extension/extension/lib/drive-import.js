// Read-only Google Drive import helpers. Pure: the token source and fetch are
// injected so the service worker (chrome.identity) and tests (mocks) share one
// code path. Nothing here touches the vault; text goes to the ingest pipeline.

import { htmlToText } from "./profile-ingest.js";

export const DRIVE_API = "https://www.googleapis.com/drive/v3";
export const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly";
export const PLACEHOLDER_CLIENT_ID = "YOUR_CLIENT_ID.apps.googleusercontent.com";
export const DRIVE_MAX_BYTES = 500 * 1024;
export const DRIVE_PAGE_SIZE = 50;
export const MAX_DRIVE_FILES_PER_RUN = 5;
const GOOGLE_DOC = "application/vnd.google-apps.document";
const TEXT_EXT = /\.(txt|md|markdown|html?|json)$/i;
const TEXT_MIMES = new Set(["text/plain", "text/markdown", "text/x-markdown", "text/html", "application/json"]);
const FILE_ID = /^[A-Za-z0-9_-]{5,128}$/;

export function isClientIdConfigured(clientId) {
  const id = String(clientId || "").trim();
  return /^[^\s]+\.apps\.googleusercontent\.com$/.test(id) && id !== PLACEHOLDER_CLIENT_ID && !id.startsWith("YOUR_");
}

export function isValidFileId(id) {
  return typeof id === "string" && FILE_ID.test(id);
}

// "export" for Google Docs (files.export as text/plain), "download" for plain
// text-like files, null for everything else (folders, PDFs, Sheets, images).
export function classifyDriveFile(file) {
  if (!file || typeof file !== "object") return null;
  if (file.mimeType === GOOGLE_DOC) return "export";
  if (/^application\/vnd\.google-apps\./.test(file.mimeType || "")) return null;
  return TEXT_MIMES.has(file.mimeType) || TEXT_EXT.test(file.name || "") ? "download" : null;
}

export function buildListUrl({ pageToken, search } = {}) {
  const clauses = ["trashed=false", "mimeType!='application/vnd.google-apps.folder'"];
  const term = String(search || "").trim().replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  if (term) clauses.push(`name contains '${term}'`);
  const params = new URLSearchParams({
    q: clauses.join(" and "),
    pageSize: String(DRIVE_PAGE_SIZE),
    orderBy: "modifiedTime desc",
    fields: "nextPageToken,files(id,name,mimeType,size,modifiedTime)"
  });
  if (pageToken) params.set("pageToken", pageToken);
  return `${DRIVE_API}/files?${params}`;
}

export function buildFileUrl(file) {
  const id = encodeURIComponent(file.id);
  return classifyDriveFile(file) === "export"
    ? `${DRIVE_API}/files/${id}/export?mimeType=${encodeURIComponent("text/plain")}`
    : `${DRIVE_API}/files/${id}?alt=media`;
}

export const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3";

// Pulls Google's own explanation out of an error response: error.status,
// error.errors[0].reason (or error.details[].reason) and error.message. The
// body is capped and only these three strings are kept, so no token, header or
// file content is carried into the thrown error.
export async function readDriveError(res) {
  let body;
  try {
    const text = (await res.text()).slice(0, 2000);
    body = JSON.parse(text);
  } catch {
    return {};
  }
  const e = body?.error;
  if (!e || typeof e !== "object") return {};
  const reason = e.errors?.find((x) => typeof x?.reason === "string")?.reason || e.details?.find((x) => typeof x?.reason === "string")?.reason;
  const out = {};
  if (typeof e.status === "string") out.googleStatus = e.status.slice(0, 80);
  if (typeof reason === "string") out.reason = reason.slice(0, 80);
  if (typeof e.message === "string") out.googleMessage = e.message.slice(0, 300);
  return out;
}

// One authorised request (GET unless init says otherwise). A 401 means the
// cached token is stale: drop it and retry once with a fresh one. Thrown errors
// carry the HTTP status as `status` so callers can classify them.
export async function driveGet(url, { getToken, removeToken, fetchFn }, init = {}) {
  if (!url.startsWith(`${DRIVE_API}/`) && !url.startsWith(`${DRIVE_UPLOAD_API}/`)) {
    throw new Error("Only Google Drive API URLs are allowed");
  }
  const fail = async (message, status, res) =>
    Object.assign(new Error(message), { status }, await readDriveError(res));
  for (let attempt = 0; attempt < 2; attempt++) {
    const token = await getToken();
    const res = await fetchFn(url, {
      ...init,
      headers: { ...init.headers, Authorization: `Bearer ${token}` },
      credentials: "omit",
      redirect: "manual"
    });
    if (res.status === 401 && attempt === 0) {
      await removeToken?.(token);
      continue;
    }
    if (res.status === 401) throw await fail("Google sign-in expired; try again", 401, res);
    if (res.status === 403) throw await fail("Drive access denied (check the Drive API and consent screen setup)", 403, res);
    if (!res.ok) throw await fail(`Drive request failed with status ${res.status}`, res.status, res);
    return res;
  }
  throw new Error("Drive request failed");
}

export async function listDriveFiles(deps, opts = {}) {
  const res = await driveGet(buildListUrl(opts), deps);
  const body = await res.json();
  const files = (body.files || [])
    .filter((f) => isValidFileId(f.id) && classifyDriveFile(f))
    .filter((f) => !(Number(f.size) > DRIVE_MAX_BYTES))
    .map((f) => ({ id: f.id, name: String(f.name || f.id), mimeType: f.mimeType, modifiedTime: f.modifiedTime || "" }));
  return { files, nextPageToken: body.nextPageToken || "" };
}

async function readCapped(res, max) {
  if (Number(res.headers.get("content-length") || 0) > max) throw new Error("File too large (limit 500KB)");
  const reader = res.body.getReader();
  const parts = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      throw new Error("File too large (limit 500KB)");
    }
    parts.push(value);
  }
  const decoder = new TextDecoder();
  return parts.map((p) => decoder.decode(p, { stream: true })).join("") + decoder.decode();
}

// Fetches metadata, then content, and returns plain text ready for chunking.
export async function fetchDriveFileText(fileId, deps) {
  if (!isValidFileId(fileId)) throw new Error("Invalid Drive file id");
  const metaRes = await driveGet(`${DRIVE_API}/files/${encodeURIComponent(fileId)}?fields=id,name,mimeType,size`, deps);
  const meta = await metaRes.json();
  const mode = classifyDriveFile(meta);
  if (!mode) throw new Error("Unsupported file type (use Google Docs, .txt, .md, .html or .json)");
  if (Number(meta.size) > DRIVE_MAX_BYTES) throw new Error("File too large (limit 500KB)");
  const res = await driveGet(buildFileUrl({ ...meta, id: fileId }), deps);
  const raw = await readCapped(res, DRIVE_MAX_BYTES);
  const name = String(meta.name || fileId);
  let text = raw;
  if (mode === "download" && (/\.html?$/i.test(name) || meta.mimeType === "text/html")) text = htmlToText(raw);
  text = text.trim();
  if (!text) throw new Error("File has no readable text");
  return { id: fileId, title: name, url: `https://drive.google.com/file/d/${fileId}/view`, text };
}

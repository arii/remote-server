// ============================================================================
// LocalForm AI - Profile ingest (pure logic)
// Turns long text (resume, portfolio, fetched web pages) into profile fact
// candidates for the Review and Merge Queue. The side panel supplies the
// on-device model call; nothing here touches chrome.* or the vault.
// ============================================================================

import { getPrompt } from "./prompts.js";

export const PROFILE_CATEGORIES = ["identity", "contact", "location", "professional", "social", "preferences", "custom"];
export const MAX_CHUNK_CHARS = 1800;
export const CHUNK_OVERLAP = 150;
export const MAX_FOLLOW_PAGES = 5;
export const MAX_CHUNKS_PER_PAGE = 20;
export const MAX_CHUNKS_PER_RUN = 60;

export const PROFILE_FACTS_SCHEMA = {
  type: "array",
  items: {
    type: "object",
    properties: {
      key: { type: "string" },
      proposedValue: { type: "string" },
      category: { type: "string", enum: PROFILE_CATEGORIES }
    },
    required: ["key", "proposedValue", "category"]
  }
};

// Split text into chunks of at most maxChars, preferring whitespace breaks,
// with a small overlap so a fact on a boundary appears whole in one chunk.
export function chunkText(text, maxChars = MAX_CHUNK_CHARS, overlap = CHUNK_OVERLAP) {
  const src = String(text || "").trim();
  if (!src) return [];
  if (src.length <= maxChars) return [src];
  const step = Math.max(1, maxChars - Math.min(overlap, maxChars - 1));
  const chunks = [];
  let start = 0;
  while (start < src.length) {
    let end = Math.min(start + maxChars, src.length);
    if (end < src.length) {
      const breakAt = src.lastIndexOf(" ", end);
      if (breakAt > start + step) end = breakAt;
    }
    chunks.push(src.slice(start, end).trim());
    if (end >= src.length) break;
    let next = Math.max(end - overlap, start + 1);
    const space = src.indexOf(" ", next);
    if (space !== -1 && space < end) next = space + 1; // begin on a word boundary
    start = next;
  }
  return chunks.filter(Boolean);
}

export function buildProfileExtractionPrompt(chunk, { existingKeys = [], guidance } = {}) {
  const known = existingKeys.length ? `\nAlready saved (skip unless the value differs): ${existingKeys.slice(0, 60).join(", ")}.` : "";
  // Page text must not be able to close the triple-quote block.
  const safeChunk = String(chunk).replace(/"{3,}/g, "''");
  return getPrompt("profile_extract", { guidance, vars: { categories: PROFILE_CATEGORIES.join(", "), known, chunk: safeChunk } });
}

// Tolerant of code fences and surrounding prose; drops malformed entries and
// entries with an unknown category. Returns [] for unparseable output.
export function parseProfileFacts(raw) {
  const text = String(raw || "");
  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start === -1 || end <= start) return [];
  let parsed;
  try {
    parsed = JSON.parse(text.slice(start, end + 1));
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const facts = [];
  for (const f of parsed) {
    if (!f || typeof f !== "object") continue;
    const key = typeof f.key === "string" ? f.key.trim() : "";
    const proposedValue = typeof f.proposedValue === "string" ? f.proposedValue.trim() : "";
    if (!key || !proposedValue) continue;
    if (!PROFILE_CATEGORIES.includes(f.category)) continue;
    facts.push({ key, proposedValue, category: f.category });
  }
  return facts;
}

const norm = (t) =>
  String(t || "")
    .toLowerCase()
    .replace(/[^a-z0-9@.+#/:_-]+/g, " ")
    .replace(/[.,;:!?]+(?=\s|$)/g, "")
    .replace(/\s+/g, " ")
    .trim();
const SHORT_ANSWER = /^(yes|no|true|false|n\/a|none)$/i;

// A model-proposed fact counts as supported only when the source text itself
// says it: a sentence of the chunk must contain the whole proposed value
// (whole words). For yes/no style values or values of 3 characters or fewer,
// that same sentence must also contain a significant word of the key, so an
// absent statement is never turned into "No". Returns the sentence or null.
export function factSupport(fact, chunk) {
  const value = norm(fact.proposedValue);
  if (!value) return null;
  const keyWords = norm(fact.key).split(" ").filter((w) => w.length >= 4);
  const needsKey = SHORT_ANSWER.test(value) || value.length <= 3;
  const sentences = String(chunk || "").split(/(?<=[.!?])\s+|\n+/);
  for (const sentence of sentences) {
    const ns = ` ${norm(sentence)} `;
    if (!ns.includes(` ${value} `)) continue;
    if (needsKey && !keyWords.some((w) => ns.includes(w))) continue;
    return sentence.trim().slice(0, 240);
  }
  return null;
}

// Run the model over every chunk and aggregate. runPrompt(prompt) returns the
// raw model string; the caller owns session lifetime (one fresh session each).
export async function extractFactsFromText(text, runPrompt, { existingKeys = [], guidance, signal, onProgress, maxChunks = MAX_CHUNKS_PER_PAGE } = {}) {
  const allChunks = chunkText(text);
  const chunks = allChunks.slice(0, Math.max(0, maxChunks));
  const seen = new Set();
  const facts = [];
  let unsupported = 0;
  for (let i = 0; i < chunks.length; i++) {
    if (signal?.aborted) throw new Error("Extraction aborted");
    if (onProgress) onProgress(i + 1, chunks.length);
    const raw = await runPrompt(buildProfileExtractionPrompt(chunks[i], { existingKeys, guidance }));
    for (const f of parseProfileFacts(raw)) {
      const id = `${f.key.toLowerCase()}|${f.proposedValue.toLowerCase()}`;
      if (seen.has(id)) continue;
      seen.add(id);
      const excerpt = factSupport(f, chunks[i]);
      if (!excerpt) {
        unsupported++;
        continue;
      }
      facts.push({ ...f, sourceExcerpt: excerpt });
    }
  }
  return { facts, unsupported, chunkCount: chunks.length, skippedChunks: allChunks.length - chunks.length };
}

const DROP_BLOCKS = /<(head|script|style|nav|noscript|svg|template|header|footer)\b[\s\S]*?<\/\1\s*>/gi;
const ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " " };

export function htmlToText(html) {
  return String(html || "")
    .replace(/<title\b[\s\S]*?<\/title\s*>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(DROP_BLOCKS, " ")
    .replace(/<\/(p|div|li|h[1-6]|tr|section|article|br)\s*>|<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&(amp|lt|gt|quot|nbsp|#39);/g, (m) => ENTITIES[m] ?? m)
    .replace(/[ \t\f\v]+/g, " ")
    .replace(/\s*\n\s*/g, "\n")
    .trim();
}

export function extractTitle(html) {
  const m = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(String(html || ""));
  return m ? htmlToText(m[1]).replace(/\s+/g, " ") : "";
}

const PROFILE_HOSTS = /(^|\.)(github\.com|linkedin\.com)$/i;

// Absolute http(s) links worth following one level: same origin as baseUrl, or
// a GitHub / LinkedIn profile. Deduped, base page excluded.
export function extractLinks(html, baseUrl) {
  let base;
  try {
    base = new URL(baseUrl);
  } catch {
    return [];
  }
  const out = [];
  const seen = new Set([base.href.split("#")[0]]);
  const re = /<a\b[^>]*?\shref\s*=\s*["']([^"']+)["']/gi;
  let m;
  while ((m = re.exec(String(html || "")))) {
    let u;
    try {
      u = new URL(m[1], base);
    } catch {
      continue;
    }
    if (u.protocol !== "https:" && u.protocol !== "http:") continue;
    u.hash = "";
    const sameSite = u.origin === base.origin;
    if (!sameSite && !PROFILE_HOSTS.test(u.hostname)) continue;
    if (seen.has(u.href)) continue;
    seen.add(u.href);
    out.push(u.href);
  }
  return out;
}

// Hosts the service worker must never fetch: localhost, .local/.internal names,
// IPv6 literals, and IPv4 loopback, private, link-local and CGNAT ranges.
// URL parsing already normalises decimal and hex IPv4 forms to dotted quads.
export function isPrivateHost(hostname) {
  const h = String(hostname || "").toLowerCase().replace(/\.$/, "");
  if (!h) return true;
  if (h === "localhost" || h.endsWith(".localhost") || h.endsWith(".local") || h.endsWith(".internal")) return true;
  if (h.startsWith("[") || h.includes(":")) return true;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  if (!m) return false;
  const [a, b] = [Number(m[1]), Number(m[2])];
  return (
    a === 0 || a === 10 || a === 127 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    a >= 224
  );
}

// Fetch the user's source pages and, optionally, a bounded number of links.
// fetchPage(url) returns {title, text, links} or throws. Followed pages are not
// crawled further. Returns {pages, failures}.
export async function collectSourcePages(sources, fetchPage, { follow = false, maxFollow = MAX_FOLLOW_PAGES } = {}) {
  const pages = [];
  const failures = [];
  const queue = [...sources];
  const visited = new Set();
  let followed = 0;
  while (queue.length) {
    const url = queue.shift();
    if (visited.has(url)) continue;
    visited.add(url);
    try {
      const page = await fetchPage(url);
      pages.push({ title: page.title || url, url, text: page.text });
      if (!follow) continue;
      for (const link of page.links || []) {
        if (followed >= maxFollow) break;
        if (visited.has(link) || queue.includes(link)) continue;
        followed++;
        visited.add(link);
        try {
          const sub = await fetchPage(link);
          pages.push({ title: sub.title || link, url: link, text: sub.text });
        } catch (err) {
          failures.push(`${link}: ${err.message}`);
        }
      }
    } catch (err) {
      failures.push(`${url}: ${err.message}`);
    }
  }
  return { pages, failures };
}

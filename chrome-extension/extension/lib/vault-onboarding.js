// ============================================================================
// LocalForm AI - Vault onboarding (pure logic)
// A prompt the user gives their own chatbot (ChatGPT, Claude, Gemini, ...) and
// a strict validator for the JSON it returns. Accepted facts go to the Review
// and Merge Queue through the panel's existing delta:queue path; nothing here
// touches chrome.* or writes the vault.
//
// Wire format is [{category, key, value}] because that is all the review queue
// keeps: approval builds the vault entry (id, aliases, tags, updatedAt) in
// lib/storage-mutations.js. Anything else in the pasted JSON is rejected.
// ============================================================================

import { PROFILE_CATEGORIES } from "./profile-ingest.js";
import { isSensitiveLabel } from "./sensitive.js";
import { MAX_PENDING_DELTAS } from "./storage-mutations.js";

export const MAX_IMPORT_FACTS = MAX_PENDING_DELTAS;
export const MAX_KEY_CHARS = 80;
export const MAX_VALUE_CHARS = 1000;
export const MAX_IMPORT_CHARS = 200000;
const FACT_FIELDS = ["category", "key", "value"];

export const MAX_EXISTING_KEYS = 100;

// Field names already in the vault, safe to show a chatbot so it skips them.
// Values are never read. Sensitive-looking names are left out.
export function existingKeysForPrompt(vault) {
  const seen = new Set();
  const out = [];
  for (const e of Array.isArray(vault) ? vault : []) {
    const key = String(e?.key || "").trim().slice(0, MAX_KEY_CHARS);
    const category = String(e?.category || "");
    if (!key || isSensitiveLabel(key) || sensitiveReason(key, "")) continue;
    const id = `${category}|${key.toLowerCase()}`;
    if (seen.has(id)) continue;
    seen.add(id);
    out.push({ category, key });
    if (out.length >= MAX_EXISTING_KEYS) break;
  }
  return out;
}

export function buildVaultPrompt({ existing = [] } = {}) {
  const example = JSON.stringify(
    [
      { category: "identity", key: "Full Name", value: "Jane Q. Example" },
      { category: "professional", key: "Current Job Title", value: "Staff Engineer" }
    ],
    null,
    2
  );
  return [
    "I am setting up a private autofill profile in a browser extension. Using what you already know about me from our conversations, list facts that are useful for filling in forms and job applications.",
    "",
    "Reply with ONLY a JSON array, no commentary. Each item is an object with exactly these three string fields and nothing else:",
    `- "category": one of ${PROFILE_CATEGORIES.map((c) => `"${c}"`).join(", ")}`,
    `- "key": a short label such as "Full Name" or "LinkedIn URL" (max ${MAX_KEY_CHARS} characters)`,
    `- "value": the fact itself (max ${MAX_VALUE_CHARS} characters)`,
    "",
    "Example:",
    example,
    "",
    "Rules:",
    "- Only include facts you actually know. Do not guess or invent anything.",
    "- NEVER include passwords, passcodes, card numbers, CVV, bank details, social security or government ID numbers, API keys, or any other secret.",
    `- At most ${MAX_IMPORT_FACTS} items. One fact per item.`,
    ...(existing.length
      ? [
          "",
          "Already in my profile (field names only, values omitted):",
          ...existing.map((e) => `- ${e.category}: ${e.key}`),
          "",
          "Do not repeat these. Only add facts not listed. If you know nothing new, reply with an empty JSON array []."
        ]
      : [])
  ].join("\n");
}

// Pull the JSON text out of a pasted reply: a single fenced block when the
// chatbot wrapped its answer, otherwise the whole text.
function unwrapJson(text) {
  const t = String(text || "").trim();
  const fence = /^```[a-z]*\s*\n([\s\S]*?)\n?```$/i.exec(t);
  return fence ? fence[1].trim() : t;
}

function luhnValid(digits) {
  let sum = 0;
  for (let i = 0; i < digits.length; i++) {
    let d = Number(digits[digits.length - 1 - i]);
    if (i % 2 === 1) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    sum += d;
  }
  return sum % 10 === 0;
}

const SECRET_WORDS = /\b(password|passcode|passphrase|cvv|cvc|secret|api[ _-]?key|private[ _-]?key|bearer)\b/i;
const SSN_VALUE = /\b\d{3}-\d{2}-\d{4}\b/;
const CARD_VALUE = /\b\d(?:[ -]?\d){12,18}\b/g;

// Why a value looks like a secret, or "" when it does not. Values are not run
// through isSensitiveLabel: its "ssn" substring match hits names like "Lessner".
export function sensitiveReason(key, value) {
  if (isSensitiveLabel(key)) return "key names a sensitive field";
  if (SECRET_WORDS.test(key) || SECRET_WORDS.test(value)) return "mentions a secret";
  if (SSN_VALUE.test(value)) return "looks like a social security number";
  for (const m of value.match(CARD_VALUE) || []) {
    if (luhnValid(m.replace(/\D/g, ""))) return "looks like a card number";
  }
  return "";
}

// Validate pasted/uploaded text. All-or-nothing: any problem returns
// {ok:false, errors} and nothing is queued, so the user fixes the reply and
// pastes again. Returns {ok:true, facts:[{key, proposedValue, category}]}, the
// shape queueVerifiedDeltas takes.
export function parseVaultImport(text) {
  const raw = String(text || "");
  if (raw.length > MAX_IMPORT_CHARS) return { ok: false, errors: [`Input is larger than ${MAX_IMPORT_CHARS} characters.`] };
  const body = unwrapJson(raw);
  if (!body) return { ok: false, errors: ["Nothing to import. Paste your chatbot's JSON reply."] };
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    return { ok: false, errors: ["Not valid JSON. Paste only the JSON array the chatbot returned."] };
  }
  if (!Array.isArray(parsed)) return { ok: false, errors: ["Expected a JSON array of facts."] };
  if (parsed.length === 0) return { ok: false, errors: ["The chatbot found nothing new to add."] };
  if (parsed.length > MAX_IMPORT_FACTS) return { ok: false, errors: [`Too many facts: ${parsed.length} (max ${MAX_IMPORT_FACTS}).`] };

  const errors = [];
  const facts = [];
  parsed.forEach((item, i) => {
    const at = `Item ${i + 1}`;
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      errors.push(`${at}: not an object.`);
      return;
    }
    const unknown = Object.keys(item).filter((k) => !FACT_FIELDS.includes(k));
    if (unknown.length) errors.push(`${at}: unknown field(s) ${unknown.join(", ")}.`);
    const missing = FACT_FIELDS.filter((k) => typeof item[k] !== "string");
    if (missing.length) {
      errors.push(`${at}: ${missing.join(", ")} must be a string.`);
      return;
    }
    const key = item.key.trim();
    const value = item.value.trim();
    if (!key || !value) errors.push(`${at}: key and value must not be empty.`);
    if (key.length > MAX_KEY_CHARS) errors.push(`${at}: key is longer than ${MAX_KEY_CHARS} characters.`);
    if (value.length > MAX_VALUE_CHARS) errors.push(`${at}: value is longer than ${MAX_VALUE_CHARS} characters.`);
    if (!PROFILE_CATEGORIES.includes(item.category)) errors.push(`${at}: category must be one of ${PROFILE_CATEGORIES.join(", ")}.`);
    const why = sensitiveReason(key, value);
    if (why) errors.push(`${at} ("${key.slice(0, 40)}"): rejected, ${why}.`);
    facts.push({ key, proposedValue: value, category: item.category });
  });
  return errors.length ? { ok: false, errors } : { ok: true, facts };
}

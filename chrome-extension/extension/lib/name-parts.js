// ============================================================================
// LocalForm AI - Name parts (pure logic)
// Forms ask for "Legal first name" / "Legal last name" while a vault usually
// stores one full-name entry. Derive the part from that entry, but only when
// the split is unambiguous (exactly two words). Values come from the user's own
// entry; nothing is guessed. The result is NOT tied to the vault entry for
// learning, so a derived part can never be learned as the full-name value.
// ============================================================================
import { normalizeLabel } from "./matcher.js";

const FULL_NAME_KEYS = new Set(["full name", "legal name", "legal full name"]);
const FIRST_RE = /^(?:legal |given |your )?(?:first|given) name\s*\*?$/;
const LAST_RE = /^(?:legal |your )?(?:last|family|sur) ?name\s*\*?$/;

export function namePartKind(label) {
  const l = normalizeLabel(label);
  if (FIRST_RE.test(l)) return "first";
  if (LAST_RE.test(l)) return "last";
  return null;
}

export function deriveNamePart(label, vault) {
  const kind = namePartKind(label);
  if (!kind) return null;
  const entry = (vault || []).find(
    (e) => e && typeof e.value === "string" && (e.id === "identity.full_name" || FULL_NAME_KEYS.has(normalizeLabel(e.key)))
  );
  if (!entry) return null;
  const words = entry.value.trim().split(/\s+/).filter(Boolean);
  if (words.length !== 2) return null;
  return { value: kind === "first" ? words[0] : words[1], fromKey: entry.key };
}

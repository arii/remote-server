// ============================================================================
// LocalForm AI - Learned Field Mappings (pure logic, #1540)
// When the outcome log shows a re-map (the user edited a filled field to a
// value that equals a DIFFERENT vault entry), we learn
//   field signature -> vault entry.
// A new mapping starts "quarantined": it is never used to fill. It becomes
// "confirmed" after QUARANTINE_CLEAN_USES clean uses or when the user clicks
// Confirm. The first user overwrite on a field where the mapping applied (or
// would have applied) deletes it. Stored records hold ids, keys and counters
// only, never a field value. The service worker is the only writer.
// No chrome.* APIs here.
// ============================================================================

import { fieldSignature, lookupMapping, normalizeLabel } from "./matcher.js";
import { isSensitiveLabel } from "./sensitive.js";

// Open question for Ari. A clean use is one more submission that agrees with
// the mapping: the user kept a fill of the mapped entry, or corrected the field
// to the mapped entry's value again. A quarantined mapping is never applied, so
// the second form is the one that happens in practice: the user keeps
// re-entering the same answer. 3 such agreeing submissions confirm it.
export const QUARANTINE_CLEAN_USES = 3;
export const MAX_MAPPINGS = 100;
export const MAX_MAPPING_HOSTS = 10;
export const MAPPING_STATUSES = ["quarantined", "confirmed"];

export { lookupMapping };

// One more agreeing submission. Promotes a quarantined record at the threshold;
// a confirmed record only counts the hit.
function withAgreement(m, host, ts, addHost) {
  const hosts = addHost ? withHost(m.hosts, host) : m.hosts;
  if (m.status === "confirmed") return { ...m, hits: m.hits + 1, hosts, lastSeen: ts };
  const cleanUses = m.cleanUses + 1;
  return {
    ...m,
    cleanUses,
    hits: m.hits + 1,
    hosts,
    lastSeen: ts,
    status: cleanUses >= QUARANTINE_CLEAN_USES ? "confirmed" : "quarantined"
  };
}

function isSensitiveSig(sig) {
  return (
    !sig ||
    sig.type === "password" ||
    [sig.label, sig.name, sig.autocomplete, sig.type].some((s) => isSensitiveLabel(s))
  );
}

function capMappings(list, max = MAX_MAPPINGS) {
  if (list.length <= max) return list;
  return [...list].sort((a, b) => b.lastSeen - a.lastSeen).slice(0, max);
}

function withHost(hosts, host) {
  const list = Array.isArray(hosts) ? hosts : [];
  if (!host || list.includes(host)) return list;
  return [...list, host].slice(-MAX_MAPPING_HOSTS);
}

// Apply one outcome record (from buildOutcomeRecords) to the mapping list.
// Returns a new list. `vault` resolves the remap target to its key and is used
// to refuse sensitive targets.
export function applyOutcomeToMappings(mappings, rec, vault, ts = Date.now()) {
  const list = Array.isArray(mappings) ? mappings : [];
  if (!rec || isSensitiveSig(rec.sig) || isSensitiveLabel(rec.entryKey)) return list;
  const sig = fieldSignature(rec.sig);
  if (!sig.replace(/\|/g, "")) return list;
  // Lookup against the state BEFORE this record: what would have applied.
  const existing = lookupMapping(list, sig, rec.host, { status: "any" });

  if (rec.outcome === "kept") {
    if (!existing) return list;
    // Only a kept fill of the mapped entry agrees. Nothing is applied while
    // quarantined, so this is an alias or autocomplete fill that happens to
    // land on the same entry.
    if (existing.entryKey !== rec.entryKey) return list;
    return list.map((m) => (m === existing ? withAgreement(m, rec.host, ts, false) : m));
  }

  // edited or cleared: a user overwrite.
  const target = rec.outcome === "edited" ? (vault || []).find((e) => e.id === rec.remappedEntryId) : null;
  const bySig = list.find((m) => m.sig === sig);
  // The same correction again, possibly on another site: not an overwrite of
  // the mapping. Record the site (this is how a mapping reaches 2 sites), and
  // count it as an agreeing submission (this is how a quarantined mapping
  // reaches confirmation without Confirm).
  if (target && bySig && bySig.entryId === target.id) {
    return list.map((m) => (m === bySig ? withAgreement(m, rec.host, ts, true) : m));
  }
  // A record for this signature that did not apply on this host (it was
  // learned elsewhere) is not touched by a different correction here, whatever
  // its status. Only a mapping that applied, or would have, is demoted.
  if (!existing && bySig) return list;
  // First overwrite of a mapping that applied (confirmed) or would have
  // applied (quarantined) demotes it: the record is deleted.
  let next = existing ? list.filter((m) => m !== existing) : list;
  if (!target || isSensitiveLabel(target.key) || isSensitiveLabel(target.id)) return next;
  // Re-learn from this correction, always starting in quarantine.
  next.push({
    sig,
    entryId: target.id,
    entryKey: target.key,
    hosts: withHost([], rec.host),
    hits: 1,
    cleanUses: 0,
    status: "quarantined",
    lastSeen: ts
  });
  return capMappings(next);
}

export function learnFromOutcomes(mappings, records, vault, ts = Date.now()) {
  let next = Array.isArray(mappings) ? mappings : [];
  for (const rec of records || []) next = applyOutcomeToMappings(next, rec, vault, ts);
  return next;
}

export function confirmMapping(mappings, sig) {
  return (mappings || []).map((m) => (m.sig === sig ? { ...m, status: "confirmed" } : m));
}

export function removeMapping(mappings, sig) {
  return (mappings || []).filter((m) => m.sig !== sig);
}

// Row shape for the Learned list: the field signature reduced to its label.
export function mappingLabel(m) {
  return normalizeLabel(String(m?.sig || "").split("|")[0]) || String(m?.sig || "");
}

// The one flat Learned list: aliases the user added (#1529, already confirmed,
// never quarantined) and learnedMappings (#1540). Rows hold labels and ids
// only. `Confirm` is offered where `unconfirmed` is true.
export function learnedRows(learnedAliases, mappings) {
  return [
    ...(learnedAliases || []).map((l) => ({
      kind: "alias",
      from: l.alias,
      to: l.entryKey,
      origin: "added by you",
      unconfirmed: false,
      entryId: l.entryId
    })),
    ...(mappings || []).map((m) => ({
      kind: "mapping",
      from: mappingLabel(m),
      to: m.entryKey,
      origin: m.status === "confirmed" ? "learned from your edit" : "learned, unconfirmed",
      unconfirmed: m.status !== "confirmed",
      sig: m.sig
    }))
  ];
}

// ============================================================================
// LocalForm AI - Vault/Delta State Mutations (pure single-writer logic)
// The service worker is the ONLY writer of userContextVault and pendingDeltas
// (OD-19 D3). These pure functions take the current state and return the next
// state, so the worker's message handler and the tests exercise the same
// rules. No chrome.* APIs appear here.
// ============================================================================

import { deduplicateVault } from "./vault.js";

const MAX_PENDING_DELTAS = 100;

function nowIso() {
  return new Date().toISOString();
}

// IDs derive from Date.now(); a batch approve creates N entries in one
// synchronous loop, so plain Date.now() ids can collide and later
// delete/update lookups by id would hit the wrong entry. Disambiguate
// against the entries already in the vault under construction.
function uniqueVaultId(base, vault) {
  let id = base;
  for (let n = 0; vault.some((e) => e.id === id); n++) id = `${base}_${n}`;
  return id;
}

// Replace-by-id or prepend. The panel's "Add New Fact" builds the entry and
// sends vault:upsert (new id -> unshift); the inline value edit sends the same
// message for an existing id, which updates in place and keeps its position.
export function applyVaultUpsert(state, entry) {
  if (!entry || typeof entry !== "object" || !entry.id) return state;
  const vault = [...(state.userContextVault || [])];
  const idx = vault.findIndex((e) => e.id === entry.id);
  if (idx >= 0) vault[idx] = { ...vault[idx], ...entry, updatedAt: entry.updatedAt || nowIso() };
  else vault.unshift({ ...entry, updatedAt: entry.updatedAt || nowIso() });
  return { ...state, userContextVault: vault };
}

export function applyVaultDelete(state, id) {
  return { ...state, userContextVault: (state.userContextVault || []).filter((e) => e.id !== id) };
}

// Import flow: replace the whole vault with the deduplicated import.
export function applyVaultReplace(state, entries) {
  if (!Array.isArray(entries)) return state;
  return { ...state, userContextVault: deduplicateVault(entries) };
}

// Approve one delta: apply it to the vault (update in place, or create by
// unshift) and remove it from the queue. Rules moved verbatim from the side
// panel's approveDelta — including the vault dedupe that used to run there.
export function applyDeltaApprove(state, deltaId) {
  const vault = [...(state.userContextVault || [])];
  const pendingDeltas = [...(state.pendingDeltas || [])];
  const targetDelta = pendingDeltas.find((d) => d.deltaId === deltaId);
  if (!targetDelta) return state;

  if (targetDelta.action === "update" && targetDelta.targetEntryId) {
    const existing = vault.find((e) => e.id === targetDelta.targetEntryId);
    if (existing) {
      existing.value = targetDelta.proposedValue;
      existing.updatedAt = nowIso();
    }
  } else {
    vault.unshift({
      id: uniqueVaultId(`${targetDelta.category || "custom"}.${Date.now()}`, vault),
      category: targetDelta.category || "custom",
      key: targetDelta.key,
      aliases: [targetDelta.key.toLowerCase()],
      value: targetDelta.proposedValue,
      tags: ["observed", targetDelta.category || "custom"],
      updatedAt: nowIso()
    });
  }

  return {
    userContextVault: deduplicateVault(vault),
    pendingDeltas: pendingDeltas.filter((d) => d.deltaId !== deltaId)
  };
}

export function applyDeltaDiscard(state, deltaId) {
  return { ...state, pendingDeltas: (state.pendingDeltas || []).filter((d) => d.deltaId !== deltaId) };
}

// Approve every pending delta in queue order; rules moved verbatim from the
// panel's "Approve All" handler.
export function applyDeltaApproveAll(state) {
  const vault = [...(state.userContextVault || [])];
  const pendingDeltas = [...(state.pendingDeltas || [])];

  for (const delta of pendingDeltas) {
    if (delta.action === "update" && delta.targetEntryId) {
      const existing = vault.find((e) => e.id === delta.targetEntryId);
      // updatedAt stamped here exactly as applyDeltaApprove does, so single
      // and bulk approval leave the same shape behind.
      if (existing) {
        existing.value = delta.proposedValue;
        existing.updatedAt = nowIso();
      }
    } else {
      vault.unshift({
        id: uniqueVaultId(`${delta.category || "custom"}.${Date.now()}`, vault),
        category: delta.category || "custom",
        key: delta.key,
        aliases: [delta.key.toLowerCase()],
        value: delta.proposedValue,
        tags: ["observed"],
        updatedAt: nowIso()
      });
    }
  }

  return { userContextVault: deduplicateVault(vault), pendingDeltas: [] };
}

export function applyDeltaDiscardAll(state) {
  return { ...state, pendingDeltas: [] };
}

// Merge already-built deltas (panel fact extraction) ahead of the queue,
// capping the queue exactly as the observer merge does. The write itself is
// serialized by the worker's mutation queue, so this cannot clobber a
// concurrent observer merge.
export function applyDeltaQueue(state, deltas) {
  if (!Array.isArray(deltas) || deltas.length === 0) return state;
  return { ...state, pendingDeltas: [...deltas, ...(state.pendingDeltas || [])].slice(0, MAX_PENDING_DELTAS) };
}

// Serialize all state mutations through one queue: each task re-reads state,
// applies its pure transition, and writes, so no two mutations can interleave
// their read-modify-write windows. A failing task is surfaced to its caller
// but does not poison the tasks queued behind it.
export function createMutationQueue() {
  let tail = Promise.resolve();
  return function enqueue(task) {
    const run = tail.then(task);
    tail = run.catch((err) => console.error("Vault/delta mutation failed:", err));
    return run;
  };
}

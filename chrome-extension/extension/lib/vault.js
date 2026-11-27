// ============================================================================
// LocalForm AI - Vault Deduplication (pure merge rules)
// Merges duplicate vault entries instead of storing copies: entries with
// the same normalized value that also share a key, a category, or the
// "observed" tag collapse into one entry whose aliases union. Canonical
// (non-timestamp) IDs sort first so they win the merge.
// ============================================================================

export function sanitizeVaultEntries(raw) {
  if (!Array.isArray(raw)) return { entries: [], dropped: 0 };
  const entries = [];
  let dropped = 0;
  for (const item of raw) {
    const isPlainObject = item !== null && typeof item === "object" && !Array.isArray(item);
    const validValue = typeof item?.value === "string" || (typeof item?.value === "number" && Number.isFinite(item.value));
    if (!isPlainObject || !validValue) {
      dropped++;
      continue;
    }
    const clean = { ...item };
    clean.key = typeof item.key === "string" ? item.key : (item.key == null ? "" : String(item.key));
    clean.value = String(item.value);
    if (item.aliases !== undefined) {
      clean.aliases = Array.isArray(item.aliases) ? item.aliases.filter((a) => typeof a === "string") : [];
    }
    entries.push(clean);
  }
  return { entries, dropped };
}

export function deduplicateVault(entries) {
  if (!Array.isArray(entries)) return [];
  // Sort so canonical IDs (like "identity.full_name" without random timestamps) come FIRST
  const sorted = sanitizeVaultEntries(entries).entries.sort((a, b) => {
    const aIsTimestamp = /\.\d{10,}$/.test(a.id || "");
    const bIsTimestamp = /\.\d{10,}$/.test(b.id || "");
    return Number(aIsTimestamp) - Number(bIsTimestamp);
  });

  const cleaned = [];
  // Normalize each entry once (memoized by identity) so the merge search
  // below stays O(n) instead of re-running String() per comparison.
  const normMemo = new Map();
  const pairFor = (entry) => {
    if (!normMemo.has(entry)) {
      normMemo.set(entry, {
        val: String(entry.value || "").toLowerCase().replace(/\s+/g, " ").trim(),
        key: String(entry.key || "").toLowerCase().trim()
      });
    }
    return normMemo.get(entry);
  };
  for (const item of sorted) {
    const normKey = pairFor(item).key;
    const normVal = pairFor(item).val;
    if (!normVal) continue;

    // If another entry already has the exact same normalized value and overlapping key/category, merge aliases instead of duplicating
    const existing = cleaned.find((c) => {
      const cPair = pairFor(c);
      return cPair.val === normVal && (cPair.key === normKey || c.category === item.category || item.tags?.includes("observed"));
    });

    if (existing) {
      const mergedAliases = new Set([...(existing.aliases || []), ...(item.aliases || []), normKey]);
      existing.aliases = Array.from(mergedAliases);
      continue;
    }
    cleaned.push(item);
  }
  return cleaned;
}

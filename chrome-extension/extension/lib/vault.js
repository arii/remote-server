// ============================================================================
// LocalForm AI - Vault Deduplication (pure merge rules)
// Merges duplicate vault entries instead of storing copies: entries with
// the same normalized value that also share a key, a category, or the
// "observed" tag collapse into one entry whose aliases union. Canonical
// (non-timestamp) IDs sort first so they win the merge.
// ============================================================================

export function deduplicateVault(entries) {
  if (!Array.isArray(entries)) return [];
  // Sort so canonical IDs (like "identity.full_name" without random timestamps) come FIRST
  const sorted = [...entries].sort((a, b) => {
    const aIsTimestamp = /\.\d{10,}$/.test(a.id || "");
    const bIsTimestamp = /\.\d{10,}$/.test(b.id || "");
    return Number(aIsTimestamp) - Number(bIsTimestamp);
  });

  const cleaned = [];
  for (const item of sorted) {
    const normKey = String(item.key || "").toLowerCase().trim();
    const normVal = String(item.value || "").toLowerCase().replace(/\s+/g, " ").trim();
    if (!normVal) continue;

    // If another entry already has the exact same normalized value and overlapping key/category, merge aliases instead of duplicating
    const existing = cleaned.find((c) => {
      const cVal = String(c.value || "").toLowerCase().replace(/\s+/g, " ").trim();
      const cKey = String(c.key || "").toLowerCase().trim();
      return cVal === normVal && (cKey === normKey || c.category === item.category || item.tags?.includes("observed"));
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

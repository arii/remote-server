import { describe, expect, it, vi } from "vitest";
import { deduplicateVault, sanitizeVaultEntries } from "../extension/lib/vault.js";
import { applyVaultReplace } from "../extension/lib/storage-mutations.js";

describe("sanitizeVaultEntries", () => {
  it("returns empty for non-array", () => {
    const result = sanitizeVaultEntries("notarray");
    expect(result).toEqual({ entries: [], dropped: 0 });
  });

  it("drops invalid items and counts them", () => {
    const raw = [
      null,
      42,
      "string",
      [1, 2],
      {},
      { value: { nested: 1 } },
      { value: true },
      { value: 3, key: "num", tags: ["a"] },
      { value: "valid", key: "key", aliases: ["ok"], category: "cat" }
    ];
    const result = sanitizeVaultEntries(raw);
    expect(result.dropped).toBe(7);
    expect(result.entries).toHaveLength(2);
    expect(result.entries[0]).toEqual({ value: "3", key: "num", tags: ["a"] });
    expect(result.entries[1]).toEqual({ value: "valid", key: "key", aliases: ["ok"], category: "cat" });
  });

  it("coerces non-array aliases to [] when aliases is present", () => {
    const raw = [{ value: "x", aliases: "notarray" }];
    const result = sanitizeVaultEntries(raw);
    expect(result.entries[0].aliases).toEqual([]);
  });

  it("filters non-string aliases", () => {
    const raw = [{ value: "x", aliases: ["ok", 42, null, "also"] }];
    const result = sanitizeVaultEntries(raw);
    expect(result.entries[0].aliases).toEqual(["ok", "also"]);
  });

  it("leaves other fields intact", () => {
    const raw = [{ value: "x", key: "k", tags: ["t"], category: "c", custom: { a: 1 } }];
    const result = sanitizeVaultEntries(raw);
    expect(result.entries[0]).toEqual({ value: "x", key: "k", tags: ["t"], category: "c", custom: { a: 1 } });
  });

  it("does not add aliases field if aliases is undefined", () => {
    const raw = [{ value: "x" }];
    const result = sanitizeVaultEntries(raw);
    expect(result.entries[0]).not.toHaveProperty("aliases");
  });
});

describe("deduplicateVault", () => {
  it("does not throw on null and returns the valid entry", () => {
    const entries = [null, { value: "v", key: "k" }];
    const result = deduplicateVault(entries);
    expect(result).toEqual([{ value: "v", key: "k" }]);
  });

  it("does not mutate input alias arrays when merging duplicates", () => {
    const a = { value: "same", key: "k", aliases: ["a"] };
    const b = { value: "same", key: "k", aliases: ["b"] };
    const input = [a, b];
    const result = deduplicateVault(input);
    expect(a.aliases).toEqual(["a"]);
    expect(b.aliases).toEqual(["b"]);
    expect(input).toEqual([a, b]);
    expect(result).toHaveLength(1);
    expect(result[0].aliases).toContain("a");
    expect(result[0].aliases).toContain("b");
  });

  it("round-trips distinct valid entries unchanged", () => {
    const entries = [
      { id: "id1", value: "v1", key: "k1" },
      { id: "id2", value: "v2", key: "k2" }
    ];
    const result = deduplicateVault(entries);
    expect(result).toEqual(entries);
  });
});

describe("matchField integration", () => {
  it("stores [] for non-array aliases via applyVaultReplace", () => {
    const entry = { id: "k.1", value: "v", key: "k", aliases: "notarray" };
    const next = applyVaultReplace({ userContextVault: [], pendingDeltas: [] }, [entry, null]);
    expect(next.userContextVault).toHaveLength(1);
    expect(next.userContextVault[0].aliases).toEqual([]);
  });
});

import { describe, expect, it } from "vitest";
import { sanitizeVaultEntries, deduplicateVault } from "../extension/lib/vault.js";
import { applyVaultReplace } from "../extension/lib/storage-mutations.js";

describe("sanitizeVaultEntries follow-ups", () => {
  it("normalizes non-array tags to [] and keeps valid string arrays", () => {
    const result = sanitizeVaultEntries([
      { id: "num", value: "1", key: "num", tags: 5 },
      { id: "obj", value: "2", key: "obj", tags: { observed: 1 } },
      { id: "str", value: "3", key: "str", tags: "observed" },
      { id: "valid", value: "3", key: "num", tags: ["a"] },
    ]);
    expect(result.dropped).toBe(0);
    expect(result.entries[0].tags).toEqual([]);
    expect(result.entries[1].tags).toEqual([]);
    expect(result.entries[2].tags).toEqual(["observed"]);
    expect(result.entries[3].tags).toEqual(["a"]);
  });

  it("drops whitespace-only string values but keeps numeric 0", () => {
    const result = sanitizeVaultEntries([
      { id: "ws", value: "   ", key: "ws" },
      { id: "zero", value: 0, key: "zero" },
      null,
      "plain-string",
    ]);
    expect(result.dropped).toBe(3);
    expect(result.entries).toHaveLength(1);
    expect(result.entries[0]).toMatchObject({ id: "zero", value: "0", key: "zero" });
  });
});

describe("deduplicateVault non-array tags", () => {
  it("does not throw on tags: 5 and treats a string observed tag as observed", () => {
    const entries = [
      { id: "a", value: "shared", key: "keyA", category: "catA", tags: 5 },
      { id: "b", value: "shared", key: "keyB", category: "catB", tags: "observed" },
    ];
    const result = deduplicateVault(entries);
    expect(Array.isArray(result)).toBe(true);
    expect(result).toHaveLength(1);
    expect(result[0].aliases).toContain("keyb");
  });
});

describe("applyVaultReplace import follow-ups", () => {
  it("replaces vault with sanitized/deduplicated entries and reports the right dropped count", () => {
    const mixed = [
      null,
      "plain-string",
      { id: "ws", value: "   ", key: "ws", category: "catWS" },
      { id: "a", value: "shared", key: "keyA", category: "catA", tags: 5 },
      { id: "b", value: "shared", key: "keyB", category: "catB", tags: "observed" },
      { id: "c", value: "unique", key: "keyC", category: "catC", aliases: ["keyC"], tags: ["observed"] },
      { id: "d", value: "unique", key: "keyD", category: "catD", tags: ["observed"] },
    ];

    let nextState;
    expect(() => {
      nextState = applyVaultReplace({}, mixed);
    }).not.toThrow();

    expect(nextState.userContextVault).toHaveLength(2);
    expect(nextState.userContextVault.map((e) => e.value).sort()).toEqual(["shared", "unique"]);

    const uniqueEntry = nextState.userContextVault.find((e) => e.value === "unique");
    expect(uniqueEntry.tags).toEqual(["observed"]);
    expect(uniqueEntry.aliases).toEqual(expect.arrayContaining(["keyC", "keyd"]));

    const sanitized = sanitizeVaultEntries(mixed);
    expect(sanitized.dropped).toBe(3);
  });
});

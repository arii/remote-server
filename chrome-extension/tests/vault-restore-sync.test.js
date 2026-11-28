import { describe, expect, it, vi, afterEach } from "vitest";
import { applyVaultReplace } from "../extension/lib/storage-mutations.js";
import { mergeStates } from "../extension/lib/drive-sync.js";

describe("applyVaultReplace stamps updatedAt", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stamps every imported entry with the current ISO time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-01-01T00:00:00Z"));
    const state = { userContextVault: [], syncTombstones: [] };
    const result = applyVaultReplace(state, [
      { id: "a", key: "a", value: "1", updatedAt: "2020-01-01T00:00:00Z" },
      { id: "b", key: "b", value: "2" },
    ]);
    expect(result.userContextVault).toHaveLength(2);
    for (const entry of result.userContextVault) {
      expect(entry.updatedAt).toBe("2025-01-01T00:00:00.000Z");
    }
    // Non-array input returns state unchanged
    expect(applyVaultReplace(state, "not-an-array")).toBe(state);
  });

  it("lets a restored entry beat an older tombstone during real mergeStates", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-01-01T00:00:00Z"));
    const now = Date.now();
    const state = {
      userContextVault: [],
      syncTombstones: [
        { kind: "vault", id: "identity.email", deletedAt: now - 86400000 },
      ],
    };
    const local = applyVaultReplace(state, [
      { id: "identity.email", key: "email", value: "a@b.com" },
    ]);
    const merged = mergeStates(local, null);
    // The restored entry survives and the obsolete tombstone is dropped.
    expect(merged.userContextVault).toContainEqual(
      expect.objectContaining({ id: "identity.email" })
    );
    expect(
      merged.syncTombstones.some(
        (t) => t.kind === "vault" && t.id === "identity.email"
      )
    ).toBe(false);
  });

  it("a newer tombstone still deletes a restored entry", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-01-01T00:00:00Z"));
    const now = Date.now();
    const state = {
      userContextVault: [],
      syncTombstones: [
        { kind: "vault", id: "identity.email", deletedAt: now + 60000 },
      ],
    };
    const local = applyVaultReplace(state, [
      { id: "identity.email", key: "email", value: "a@b.com" },
    ]);
    const merged = mergeStates(local, null);
    expect(
      merged.userContextVault.some((e) => e.id === "identity.email")
    ).toBe(false);
  });
});

// Golden tests for vault deduplication (lib/vault.js), extracted verbatim
// from the side panel's render/clean path.
import { describe, expect, it } from "vitest";
import { deduplicateVault } from "../extension/lib/vault.js";

describe("deduplicateVault", () => {
  it("returns an empty array for non-array input", () => {
    expect(deduplicateVault(null)).toEqual([]);
    expect(deduplicateVault(undefined)).toEqual([]);
  });

  it("drops entries with empty values", () => {
    expect(deduplicateVault([{ id: "a", key: "Name", value: "" }])).toEqual([]);
  });

  it("merges a timestamped duplicate into the canonical entry, unioning aliases", () => {
    const canonical = { id: "identity.full_name", key: "full name", value: "Ariel Anders", category: "identity", aliases: [] };
    const observed = {
      id: "identity.full_name.1690000000000",
      key: "Full Name",
      value: "Ariel Anders",
      category: "identity",
      aliases: ["name"],
      tags: []
    };
    const cleaned = deduplicateVault([observed, canonical]);
    expect(cleaned).toHaveLength(1);
    expect(cleaned[0].id).toBe("identity.full_name");
    expect(cleaned[0].aliases).toEqual(expect.arrayContaining(["name", "full name"]));
  });

  it("merges an observed-tagged entry across categories into the same value", () => {
    const bio = { id: "custom.bio", key: "Bio", value: "I build systems", category: "custom", aliases: [] };
    const observed = {
      id: "custom.bio.1690000000000",
      key: "About",
      value: "I build systems",
      category: "professional",
      aliases: [],
      tags: ["observed"]
    };
    const cleaned = deduplicateVault([bio, observed]);
    expect(cleaned).toHaveLength(1);
    expect(cleaned[0].id).toBe("custom.bio");
    expect(cleaned[0].aliases).toContain("about");
  });

  it("keeps same-key entries with different values as separate facts", () => {
    const cleaned = deduplicateVault([
      { id: "custom.1", key: "Bio", value: "First bio", category: "custom" },
      { id: "custom.2", key: "Bio", value: "Second bio", category: "custom" }
    ]);
    expect(cleaned).toHaveLength(2);
  });

  it("keeps entries whose values differ only in punctuation spacing apart (key and category both differ)", () => {
    const cleaned = deduplicateVault([
      { id: "a", key: "Bio", value: "Same value", category: "custom", tags: [] },
      { id: "b", key: "Other", value: "Same value", category: "professional", tags: [] }
    ]);
    expect(cleaned).toHaveLength(2);
  });
});

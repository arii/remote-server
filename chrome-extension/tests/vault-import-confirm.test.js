import { describe, it, expect } from "vitest";
import { describeVaultImport } from "../extension/lib/vault.js";

describe("describeVaultImport", () => {
  it("counts current and imported entries without dropped", () => {
    const current = [{ id: "a", key: "name", value: "Alice" }];
    const imported = [{ id: "b", key: "email", value: "alice@example.com" }];
    const result = describeVaultImport(current, imported);
    expect(result.currentCount).toBe(1);
    expect(result.importCount).toBe(1);
    expect(result.dropped).toBe(0);
    expect(result.message).toBe(
      "Replace your vault with this file?\n\nCurrent vault: 1 entry\nIn the file: 1 valid entry\n\nEntries that are not in the file are removed from this vault. If you press Sync with Google Drive, those removals are applied on your other devices too, and the imported values can overwrite newer edits made there."
    );
  });

  it("uses plural forms for counts > 1", () => {
    const current = [
      { id: "a", key: "name", value: "Alice" },
      { id: "b", key: "email", value: "alice@example.com" },
    ];
    const imported = [
      { id: "c", key: "phone", value: "555" },
      { id: "d", key: "city", value: "Paris" },
    ];
    const result = describeVaultImport(current, imported);
    expect(result.currentCount).toBe(2);
    expect(result.importCount).toBe(2);
    expect(result.dropped).toBe(0);
    expect(result.message).toContain("Current vault: 2 entries");
    expect(result.message).toContain("In the file: 2 valid entries");
    expect(result.message).not.toContain("invalid skipped");
  });

  it("appends dropped suffix when invalid entries are present", () => {
    const imported = [
      { id: "ok", key: "name", value: "Alice" },
      { value: "   " },
      null,
    ];
    const result = describeVaultImport([], imported);
    expect(result.currentCount).toBe(0);
    expect(result.importCount).toBe(1);
    expect(result.dropped).toBe(2);
    expect(result.message).toContain("In the file: 1 valid entry (2 invalid skipped)");
  });

  it("accepts non-array inputs as empty", () => {
    const result = describeVaultImport(undefined, "not-array");
    expect(result.currentCount).toBe(0);
    expect(result.importCount).toBe(0);
    expect(result.dropped).toBe(0);
    expect(result.message).toContain("Current vault: 0 entries");
    expect(result.message).toContain("In the file: 0 valid entries");
  });

  it("counts duplicate entries in the file once", () => {
    const imported = [
      { id: "a", key: "name", value: "Alice" },
      { id: "b", key: "name", value: "Alice" },
      { id: "c", key: "email", value: "alice@example.com" },
    ];
    const result = describeVaultImport([], imported);
    expect(result.importCount).toBe(2);
    expect(result.dropped).toBe(0);
  });

  it("skips whitespace-only and null values as dropped", () => {
    const imported = [
      { id: "valid", key: "name", value: "Bob" },
      { id: "blank", key: "x", value: "   " },
      null,
      { id: "n", key: "y", value: [] },
    ];
    const result = describeVaultImport([], imported);
    expect(result.importCount).toBe(1);
    expect(result.dropped).toBe(3);
    expect(result.message).toContain("In the file: 1 valid entry (3 invalid skipped)");
  });
});

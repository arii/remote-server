import { describe, expect, it } from "vitest";
import { mergeStates } from "../extension/lib/drive-sync.js";

describe("mergeStates confirmed status stickiness", () => {
  const baseMapping = {
    sig: "s1",
    entryId: "a",
    entryKey: "a",
    hosts: [],
    hits: 2,
    cleanUses: 0,
    status: "quarantined",
    lastSeen: 10,
  };

  it("keeps confirmed when remote has newer quarantined", () => {
    const local = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 100 }],
    };
    const remote = {
      learnedMappings: [
        { ...baseMapping, status: "quarantined", lastSeen: 200 },
      ],
    };
    const result = mergeStates(local, remote);
    const mapping = result.learnedMappings.find((m) => m.sig === "s1");
    expect(mapping.status).toBe("confirmed");
    expect(mapping.lastSeen).toBe(200);
    expect(mapping.hits).toBe(2);
  });

  it("keeps confirmed when local newer quarantined and remote confirmed older", () => {
    const local = {
      learnedMappings: [
        { ...baseMapping, status: "quarantined", lastSeen: 200 },
      ],
    };
    const remote = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 100 }],
    };
    const result = mergeStates(local, remote);
    const mapping = result.learnedMappings.find((m) => m.sig === "s1");
    expect(mapping.status).toBe("confirmed");
    expect(mapping.lastSeen).toBe(200);
  });

  it("stays quarantined when both quarantined", () => {
    const local = { learnedMappings: [{ ...baseMapping, lastSeen: 100 }] };
    const remote = { learnedMappings: [{ ...baseMapping, lastSeen: 200 }] };
    const result = mergeStates(local, remote);
    const mapping = result.learnedMappings.find((m) => m.sig === "s1");
    expect(mapping.status).toBe("quarantined");
  });

  it("stays confirmed when both confirmed", () => {
    const local = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 100 }],
    };
    const remote = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 200 }],
    };
    const result = mergeStates(local, remote);
    const mapping = result.learnedMappings.find((m) => m.sig === "s1");
    expect(mapping.status).toBe("confirmed");
  });

  it("removes mapping when tombstone newer than confirmed", () => {
    const local = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 100 }],
    };
    const remote = {
      learnedMappings: [],
      tombstones: [{ kind: "mapping", id: "s1", deletedAt: 300 }],
    };
    const result = mergeStates(local, remote);
    expect(result.learnedMappings.find((m) => m.sig === "s1")).toBeUndefined();
  });

  it("keeps confirmed when tombstone older than newest version", () => {
    const local = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 200 }],
    };
    const remote = {
      learnedMappings: [],
      tombstones: [{ kind: "mapping", id: "s1", deletedAt: 150 }],
    };
    const result = mergeStates(local, remote);
    const mapping = result.learnedMappings.find((m) => m.sig === "s1");
    expect(mapping).toBeDefined();
    expect(mapping.status).toBe("confirmed");
  });

  it("invalidates confirmed older than tombstone even when relearned newer mapping exists", () => {
    const local = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 100 }],
    };
    const remote = {
      learnedMappings: [
        { ...baseMapping, status: "quarantined", lastSeen: 200 },
      ],
      tombstones: [{ kind: "mapping", id: "s1", deletedAt: 150 }],
    };
    const result = mergeStates(local, remote);
    const mapping = result.learnedMappings.find((m) => m.sig === "s1");
    expect(mapping).toBeDefined();
    expect(mapping.status).toBe("quarantined");
  });

  it("keeps confirmed when tombstone is older than all versions", () => {
    const local = {
      learnedMappings: [{ ...baseMapping, status: "confirmed", lastSeen: 100 }],
    };
    const remote = {
      learnedMappings: [
        { ...baseMapping, status: "quarantined", lastSeen: 200 },
      ],
      tombstones: [{ kind: "mapping", id: "s1", deletedAt: 90 }],
    };
    const result = mergeStates(local, remote);
    const mapping = result.learnedMappings.find((m) => m.sig === "s1");
    expect(mapping).toBeDefined();
    expect(mapping.status).toBe("confirmed");
  });
});

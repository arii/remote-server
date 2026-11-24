import { describe, expect, it, vi } from "vitest";
import {
  DriveSyncError,
  SYNC_FILE_NAME,
  addTombstones,
  buildPayload,
  errorInfo,
  formatSyncFailure,
  mergeStates,
  syncWithDrive
} from "../extension/lib/drive-sync.js";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

// In-memory Drive appDataFolder behind a mocked fetch.
function fakeDrive({ file = null, failWith } = {}) {
  const drive = { file, urls: [], calls: [], tokens: 0, removed: [] };
  const fetchFn = vi.fn(async (url, init = {}) => {
    drive.urls.push(url);
    drive.calls.push({ url, init });
    if (failWith) return failWith(url, init, drive);
    const u = new URL(url);
    if (u.pathname === "/drive/v3/files" && u.searchParams.get("spaces") === "appDataFolder") {
      return json({ files: drive.file ? [{ id: "file1234", name: SYNC_FILE_NAME }] : [] });
    }
    if (u.pathname === "/drive/v3/files/file1234") return new Response(drive.file);
    if (u.pathname === "/upload/drive/v3/files" && init.method === "POST") {
      drive.file = init.body.split("\r\n\r\n")[2].split("\r\n--")[0];
      return json({ id: "file1234" });
    }
    if (u.pathname === "/upload/drive/v3/files/file1234" && init.method === "PATCH") {
      drive.file = init.body;
      return json({ id: "file1234" });
    }
    return json({}, 404);
  });
  const deps = {
    fetchFn,
    getToken: vi.fn(async () => `tok${++drive.tokens}`),
    removeToken: vi.fn(async (t) => drive.removed.push(t))
  };
  return { drive, deps };
}

// One simulated device: local storage plus the same apply step the worker runs.
function device(initial = {}) {
  const store = { userContextVault: [], pendingDeltas: [], ...initial };
  const sync = async (deps) => {
    await syncWithDrive(deps, {
      applyRemote: async (remote) => {
        const merged = mergeStates(store, remote);
        Object.assign(store, merged);
        return buildPayload(merged);
      }
    });
    return store;
  };
  return { store, sync };
}

const entry = (id, value, updatedAt) => ({ id, category: "identity", key: id, aliases: [], value, tags: [], updatedAt });

describe("drive sync", () => {
  it("first sync creates localform-sync.json in appDataFolder with the local state", async () => {
    const { drive, deps } = fakeDrive();
    const a = device({ userContextVault: [entry("identity.name", "Ada Lovelace", "2026-01-01T00:00:00.000Z")] });
    await a.sync(deps);
    const post = drive.calls.find((c) => c.init.method === "POST");
    expect(post.url).toBe("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id");
    expect(post.init.body).toContain('"parents":["appDataFolder"]');
    expect(post.init.body).toContain(`"name":"${SYNC_FILE_NAME}"`);
    const body = JSON.parse(drive.file);
    expect(body.schemaVersion).toBe(1);
    expect(typeof body.updatedAt).toBe("string");
    expect(body.userContextVault.map((e) => e.id)).toEqual(["identity.name"]);
  });

  it("a second device merges what the first uploaded with its own entries", async () => {
    const { drive, deps } = fakeDrive();
    const a = device({
      userContextVault: [entry("a", "from A", "2026-01-01T00:00:00.000Z")],
      learnedMappings: [{ sig: "s1", entryId: "a", entryKey: "a", hosts: [], hits: 2, cleanUses: 0, status: "quarantined", lastSeen: 10 }]
    });
    await a.sync(deps);
    const b = device({
      userContextVault: [entry("b", "from B", "2026-01-02T00:00:00.000Z")],
      learnedMappings: [{ sig: "s1", entryId: "a", entryKey: "a", hosts: [], hits: 5, cleanUses: 1, status: "quarantined", lastSeen: 5 }]
    });
    await b.sync(deps);
    expect(b.store.userContextVault.map((e) => e.id).sort()).toEqual(["a", "b"]);
    expect(b.store.learnedMappings[0].hits).toBe(5);
    await a.sync(deps);
    expect(a.store.userContextVault.map((e) => e.id).sort()).toEqual(["a", "b"]);
    expect(a.store.learnedMappings[0].hits).toBe(5);
  });

  it("takes the max hit count even when the other side's entry is newer", () => {
    const m = (hits, lastSeen) => ({ sig: "s", entryId: "e", entryKey: "k", hosts: [], hits, cleanUses: 0, status: "confirmed", lastSeen });
    const merged = mergeStates({ learnedMappings: [m(9, 1)] }, { learnedMappings: [m(3, 100)] });
    expect(merged.learnedMappings[0]).toMatchObject({ hits: 9, lastSeen: 100 });
  });

  it("the newer entry wins per id, either direction", () => {
    const old = entry("x", "old", "2026-01-01T00:00:00.000Z");
    const fresh = entry("x", "fresh", "2026-02-01T00:00:00.000Z");
    expect(mergeStates({ userContextVault: [old] }, { userContextVault: [fresh] }).userContextVault[0].value).toBe("fresh");
    expect(mergeStates({ userContextVault: [fresh] }, { userContextVault: [old] }).userContextVault[0].value).toBe("fresh");
  });

  it("a tombstone beats a stale entry on the other device, so a deleted fact stays deleted", async () => {
    const { drive, deps } = fakeDrive();
    const stale = entry("x", "secret", "2026-01-01T00:00:00.000Z");
    const a = device({ userContextVault: [stale] });
    await a.sync(deps);
    const b = device();
    await b.sync(deps);
    expect(b.store.userContextVault).toHaveLength(1);
    // B deletes the entry and records the tombstone, as the worker does.
    b.store.syncTombstones = addTombstones([], "vault", b.store.userContextVault, [], (e) => e.id, Date.parse("2026-03-01"));
    b.store.userContextVault = [];
    await b.sync(deps);
    expect(JSON.parse(drive.file).tombstones).toHaveLength(1);
    await a.sync(deps);
    expect(a.store.userContextVault).toEqual([]);
  });

  it("an entry edited after the deletion survives the tombstone", () => {
    const tomb = { kind: "vault", id: "x", deletedAt: Date.parse("2026-03-01") };
    const merged = mergeStates({ userContextVault: [entry("x", "back", "2026-04-01T00:00:00.000Z")] }, { tombstones: [tomb] });
    expect(merged.userContextVault).toHaveLength(1);
    expect(merged.syncTombstones).toEqual([]);
  });

  it("merges accepted prompt guidance by edit time and unions outcomes", () => {
    const g = (global, at) => ({ global, perPrompt: {}, history: [{ at, before: { global: "", perPrompt: {} }, after: { global, perPrompt: {} }, source: "user" }] });
    const out = (fillId, ts) => ({ fillId, ts, host: "h", sig: { label: "l", name: "", autocomplete: "", type: "" }, entryKey: "k", pass: "alias", confidence: 1, outcome: "kept", remappedEntryId: null });
    const merged = mergeStates(
      { promptGuidance: g("older", 1000), fillOutcomes: [out("f1", 1)] },
      { promptGuidance: [{ id: "global", text: "newer", updatedAt: 2000 }], fillOutcomes: [out("f1", 1), out("f2", 2)] }
    );
    expect(merged.promptGuidance.global).toBe("newer");
    expect(merged.fillOutcomes.map((o) => o.fillId)).toEqual(["f1", "f2"]);
  });

  it("retries once with a fresh token after a 401", async () => {
    let first = true;
    const { drive, deps } = fakeDrive({
      failWith: (url, init) => {
        if (first) {
          first = false;
          return new Response("", { status: 401 });
        }
        return json({ files: [] });
      }
    });
    const payload = await syncWithDrive(deps, { applyRemote: async () => buildPayload({}) }).catch((e) => e);
    // The retry succeeded; the later upload gets the same stub reply and is ok.
    expect(payload).not.toBeInstanceOf(Error);
    expect(drive.removed).toEqual(["tok1"]);
    expect(drive.calls[1].init.headers.Authorization).toBe("Bearer tok2");
  });

  it("payload holds no vault values outside userContextVault", async () => {
    const { drive, deps } = fakeDrive();
    const a = device({
      userContextVault: [entry("identity.name", "SECRET-VALUE-123", "2026-01-01T00:00:00.000Z")],
      learnedMappings: [{ sig: "s", entryId: "identity.name", entryKey: "name", hosts: ["h.com"], hits: 1, cleanUses: 0, status: "quarantined", lastSeen: 1, value: "SECRET-VALUE-123" }],
      learnedAliases: [{ label: "full name", alias: "full name", entryId: "identity.name", entryKey: "name", ts: 1, value: "SECRET-VALUE-123" }],
      fillOutcomes: [{ fillId: "f", ts: 1, host: "h.com", sig: { label: "l", name: "", autocomplete: "", type: "" }, entryKey: "name", pass: "alias", confidence: 1, outcome: "kept", remappedEntryId: null, filled: "SECRET-VALUE-123", submitted: "SECRET-VALUE-123" }]
    });
    await a.sync(deps);
    const { userContextVault, ...rest } = JSON.parse(drive.file);
    expect(userContextVault[0].value).toBe("SECRET-VALUE-123");
    expect(JSON.stringify(rest)).not.toContain("SECRET-VALUE-123");
  });

  it("only talks to the Drive v3 and upload v3 endpoints", async () => {
    const { drive, deps } = fakeDrive();
    const a = device({ userContextVault: [entry("x", "v", "2026-01-01T00:00:00.000Z")] });
    await a.sync(deps);
    await a.sync(deps);
    expect(drive.urls.length).toBeGreaterThan(3);
    for (const url of drive.urls) {
      expect(url.startsWith("https://www.googleapis.com/drive/v3/") || url.startsWith("https://www.googleapis.com/upload/drive/v3/")).toBe(true);
    }
  });
});

describe("drive sync errors", () => {
  const run = (deps) => syncWithDrive(deps, { applyRemote: async () => buildPayload({}) }).catch((e) => e);

  it("not signed in", async () => {
    const { deps } = fakeDrive();
    deps.getToken = async () => {
      throw new Error("Google sign-in was cancelled");
    };
    const err = await run(deps);
    expect(err).toBeInstanceOf(DriveSyncError);
    expect(err.code).toBe("not-signed-in");
    expect(err.message).toMatch(/Sign in to Google/);
  });

  it("repeated 401 is reported as not signed in", async () => {
    const { deps } = fakeDrive({ failWith: () => new Response("", { status: 401 }) });
    expect((await run(deps)).code).toBe("not-signed-in");
  });

  const gerr = (status, error) => () => json({ error }, status);

  it("bare 403 with no body is forbidden, not scope denied", async () => {
    const { deps } = fakeDrive({ failWith: () => new Response("", { status: 403 }) });
    const err = await run(deps);
    expect(err.code).toBe("forbidden");
    expect(err.status).toBe(403);
    expect(err.step).toBe("pull");
  });

  it("403 insufficientPermissions is scope denied", async () => {
    const { deps } = fakeDrive({ failWith: gerr(403, { message: "Insufficient Permission", errors: [{ reason: "insufficientPermissions" }] }) });
    const err = await run(deps);
    expect(err.code).toBe("scope-denied");
    expect(err.message).toMatch(/app data folder/);
    expect(err.reason).toBe("insufficientPermissions");
  });

  it("403 accessNotConfigured names the disabled API and the fix", async () => {
    const { deps } = fakeDrive({
      failWith: gerr(403, { status: "PERMISSION_DENIED", message: "Google Drive API has not been used in project 123 before or it is disabled.", errors: [{ reason: "accessNotConfigured" }] })
    });
    const info = errorInfo(await run(deps));
    expect(info).toMatchObject({ code: "api-disabled", status: 403, reason: "accessNotConfigured", step: "pull" });
    expect(info.googleMessage).toMatch(/has not been used in project 123/);
    expect(info.next).toMatch(/enable the Google Drive API/);
    const text = formatSyncFailure(info);
    expect(text).toMatch(/^Sync failed: The Google Drive API is not turned on/);
    expect(text).toMatch(/HTTP 403, accessNotConfigured: Google Drive API has not been used/);
    expect(text).toMatch(/Next: In Google Cloud console/);
  });

  it("403 SERVICE_DISABLED in details is api-disabled", async () => {
    const { deps } = fakeDrive({ failWith: gerr(403, { status: "PERMISSION_DENIED", message: "API disabled", details: [{ reason: "SERVICE_DISABLED" }] }) });
    const err = await run(deps);
    expect(err.code).toBe("api-disabled");
    expect(err.reason).toBe("SERVICE_DISABLED");
  });

  it("403 storageQuotaExceeded is quota", async () => {
    const { deps } = fakeDrive({ failWith: gerr(403, { message: "quota", errors: [{ reason: "storageQuotaExceeded" }] }) });
    expect((await run(deps)).code).toBe("quota");
  });

  it("403 userRateLimitExceeded and 429 are rate-limited", async () => {
    const a = fakeDrive({ failWith: gerr(403, { message: "slow", errors: [{ reason: "userRateLimitExceeded" }] }) });
    expect((await run(a.deps)).code).toBe("rate-limited");
    const b = fakeDrive({ failWith: gerr(429, { message: "Too many requests" }) });
    const err = await run(b.deps);
    expect(err.code).toBe("rate-limited");
    expect(err.status).toBe(429);
  });

  it("401 twice keeps the status", async () => {
    const { deps } = fakeDrive({ failWith: gerr(401, { message: "Invalid Credentials" }) });
    const err = await run(deps);
    expect(err.code).toBe("not-signed-in");
    expect(err.status).toBe(401);
    expect(err.googleMessage).toBe("Invalid Credentials");
  });

  it("500 is drive-unavailable and a non-JSON body still reports the status", async () => {
    const a = fakeDrive({ failWith: gerr(500, { message: "Backend Error" }) });
    const err = await run(a.deps);
    expect(err.code).toBe("drive-unavailable");
    expect(err.googleMessage).toBe("Backend Error");
    const b = fakeDrive({ failWith: () => new Response("<html>Bad gateway</html>", { status: 502 }) });
    const e2 = await run(b.deps);
    expect(e2.code).toBe("drive-unavailable");
    expect(e2.status).toBe(502);
    expect(e2.googleMessage).toBe("");
  });

  it("other statuses stay failed but carry the status", async () => {
    const { deps } = fakeDrive({ failWith: gerr(404, { message: "File not found" }) });
    const err = await run(deps);
    expect(err.code).toBe("failed");
    expect(err.message).toMatch(/HTTP 404/);
  });

  it("fetch timeout keeps the error name and message", async () => {
    const { deps } = fakeDrive({
      failWith: () => {
        throw Object.assign(new Error("The operation timed out."), { name: "TimeoutError" });
      }
    });
    const info = errorInfo(await run(deps));
    expect(info.code).toBe("network");
    expect(info.name).toBe("TimeoutError");
    expect(info.detail).toMatch(/TimeoutError: The operation timed out/);
    expect(formatSyncFailure(info)).toMatch(/TimeoutError \(network\), during pull/);
  });

  it("getToken failures keep the chrome.identity message", async () => {
    const a = fakeDrive();
    a.deps.getToken = async () => {
      throw new Error("OAuth2 not granted or revoked.");
    };
    const e1 = await run(a.deps);
    expect(e1.code).toBe("scope-denied");
    expect(e1.message).toMatch(/OAuth2 not granted or revoked/);
    expect(e1.step).toBe("sign-in");
    const b = fakeDrive();
    b.deps.getToken = async () => {
      throw new Error("The user did not approve access.");
    };
    const e2 = await run(b.deps);
    expect(e2.code).toBe("scope-denied");
    expect(e2.detail).toMatch(/did not approve access/);
  });

  it("an error while merging reports the merge step and its name", async () => {
    const { deps } = fakeDrive();
    const err = await syncWithDrive(deps, {
      applyRemote: async () => {
        throw new RangeError("storage write failed");
      }
    }).catch((e) => e);
    const info = errorInfo(err);
    expect(info).toMatchObject({ code: "failed", name: "RangeError", step: "merge" });
    expect(info.summary).toBe("Google Drive sync failed.");
    expect(formatSyncFailure(info)).toMatch(/RangeError: storage write failed/);
  });

  it("schema mismatch has a next step", async () => {
    const { deps } = fakeDrive({ file: JSON.stringify({ schemaVersion: 99 }) });
    const info = errorInfo(await syncWithDrive(deps, { applyRemote: vi.fn() }).catch((e) => e));
    expect(info.code).toBe("schema-mismatch");
    expect(info.next).toMatch(/Update the extension/);
  });

  it("errorInfo handles plain errors and never leaks the token or file body", async () => {
    const info = errorInfo(new Error("boom"));
    expect(info).toMatchObject({ code: "failed", name: "Error" });
    expect(info.summary && info.next).toBeTruthy();
    const { deps } = fakeDrive({
      failWith: (url, init) => json({ error: { message: "denied", errors: [{ reason: "forbidden" }] }, echo: init.headers.Authorization }, 403)
    });
    const err = await syncWithDrive(deps, { applyRemote: async () => ({ userContextVault: [{ value: "SECRET-VALUE" }] }) }).catch((e) => e);
    const dump = JSON.stringify(errorInfo(err)) + err.message + formatSyncFailure(errorInfo(err));
    expect(dump).not.toMatch(/tok\d|Bearer|SECRET-VALUE/);
  });

  it("network failure", async () => {
    const { deps } = fakeDrive({
      failWith: () => {
        throw new TypeError("Failed to fetch");
      }
    });
    const err = await run(deps);
    expect(err.code).toBe("network");
    expect(err.message).toMatch(/Could not reach Google Drive/);
  });

  it("schema mismatch leaves local untouched and does not upload", async () => {
    const { drive, deps } = fakeDrive({ file: JSON.stringify({ schemaVersion: 99 }) });
    const applyRemote = vi.fn();
    const err = await syncWithDrive(deps, { applyRemote }).catch((e) => e);
    expect(err.code).toBe("schema-mismatch");
    expect(applyRemote).not.toHaveBeenCalled();
    expect(drive.calls.some((c) => c.init.method === "PATCH")).toBe(false);
  });

  it("unreadable remote file is a schema mismatch too", async () => {
    const { deps } = fakeDrive({ file: "not json" });
    expect((await run(deps)).code).toBe("schema-mismatch");
  });
});

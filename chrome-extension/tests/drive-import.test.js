import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import {
  DRIVE_MAX_BYTES,
  buildFileUrl,
  buildListUrl,
  classifyDriveFile,
  fetchDriveFileText,
  isClientIdConfigured,
  listDriveFiles
} from "../extension/lib/drive-import.js";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

function deps(handler) {
  const fetchFn = vi.fn(async (url, init) => handler(url, init));
  const removeToken = vi.fn(async () => {});
  let n = 0;
  const getToken = vi.fn(async () => `tok${++n}`);
  return { fetchFn, getToken, removeToken };
}

describe("isClientIdConfigured", () => {
  it("treats the placeholder and junk as unset", () => {
    expect(isClientIdConfigured("YOUR_CLIENT_ID.apps.googleusercontent.com")).toBe(false);
    expect(isClientIdConfigured("")).toBe(false);
    expect(isClientIdConfigured(undefined)).toBe(false);
    expect(isClientIdConfigured("123-abc.apps.googleusercontent.com")).toBe(true);
  });
  it("ships the manifest with the placeholder, identity permission and read-only scope", () => {
    const m = JSON.parse(readFileSync("extension/manifest.json", "utf8"));
    expect(m.permissions).toContain("identity");
    expect(isClientIdConfigured(m.oauth2.client_id)).toBe(false);
    expect(m.oauth2.scopes).toEqual(["https://www.googleapis.com/auth/drive.readonly"]);
  });
});

describe("classifyDriveFile and URLs", () => {
  it("exports Google Docs and downloads text-like files only", () => {
    expect(classifyDriveFile({ mimeType: "application/vnd.google-apps.document", name: "cv" })).toBe("export");
    expect(classifyDriveFile({ mimeType: "application/octet-stream", name: "notes.md" })).toBe("download");
    expect(classifyDriveFile({ mimeType: "application/json", name: "x" })).toBe("download");
    expect(classifyDriveFile({ mimeType: "application/pdf", name: "cv.pdf" })).toBeNull();
    expect(classifyDriveFile({ mimeType: "application/vnd.google-apps.spreadsheet", name: "a.txt" })).toBeNull();
  });
  it("builds export vs media URLs and escapes the search term", () => {
    expect(buildFileUrl({ id: "abc12345", mimeType: "application/vnd.google-apps.document" })).toContain("/export?mimeType=text%2Fplain");
    expect(buildFileUrl({ id: "abc12345", mimeType: "text/plain" })).toContain("?alt=media");
    const q = new URL(buildListUrl({ search: "o'brien" })).searchParams.get("q");
    expect(q).toContain("name contains 'o\\'brien'");
  });
});

describe("listDriveFiles", () => {
  it("sends the bearer token, drops unsupported and oversized files", async () => {
    const d = deps(() =>
      json({
        nextPageToken: "next",
        files: [
          { id: "doc00001", name: "Resume", mimeType: "application/vnd.google-apps.document" },
          { id: "pdf00001", name: "cv.pdf", mimeType: "application/pdf" },
          { id: "big00001", name: "big.txt", mimeType: "text/plain", size: String(DRIVE_MAX_BYTES + 1) },
          { id: "txt00001", name: "bio.txt", mimeType: "text/plain", size: "10" }
        ]
      })
    );
    const out = await listDriveFiles(d);
    expect(out.files.map((f) => f.id)).toEqual(["doc00001", "txt00001"]);
    expect(out.nextPageToken).toBe("next");
    expect(d.fetchFn.mock.calls[0][1].headers.Authorization).toBe("Bearer tok1");
  });
  it("removes the cached token on 401 and retries once", async () => {
    let calls = 0;
    const d = deps(() => (++calls === 1 ? new Response("", { status: 401 }) : json({ files: [] })));
    await listDriveFiles(d);
    expect(d.removeToken).toHaveBeenCalledWith("tok1");
    expect(d.fetchFn.mock.calls[1][1].headers.Authorization).toBe("Bearer tok2");
  });
  it("fails after a second 401", async () => {
    const d = deps(() => new Response("", { status: 401 }));
    await expect(listDriveFiles(d)).rejects.toThrow(/expired/);
  });
});

describe("fetchDriveFileText", () => {
  const meta = (m) => json({ id: "file0001", ...m });
  it("exports a Google Doc as text/plain", async () => {
    const d = deps((url) =>
      url.includes("/export") ? new Response("Jane Doe\njane@example.com") : meta({ name: "CV", mimeType: "application/vnd.google-apps.document" })
    );
    const r = await fetchDriveFileText("file0001", d);
    expect(r.text).toBe("Jane Doe\njane@example.com");
    expect(d.fetchFn.mock.calls[1][0]).toContain("/export?mimeType=text%2Fplain");
  });
  it("downloads html and converts it to text", async () => {
    const d = deps((url) =>
      url.includes("alt=media") ? new Response("<html><body><p>Hello Jane</p><script>x()</script></body></html>") : meta({ name: "p.html", mimeType: "text/html" })
    );
    const r = await fetchDriveFileText("file0001", d);
    expect(r.text).toContain("Hello Jane");
    expect(r.text).not.toContain("<p>");
    expect(r.text).not.toContain("x()");
  });
  it("rejects unsupported types, oversize metadata, oversize bodies and bad ids", async () => {
    await expect(fetchDriveFileText("file0001", deps(() => meta({ name: "a.pdf", mimeType: "application/pdf" })))).rejects.toThrow(/Unsupported/);
    await expect(
      fetchDriveFileText("file0001", deps(() => meta({ name: "a.txt", mimeType: "text/plain", size: String(DRIVE_MAX_BYTES + 1) })))
    ).rejects.toThrow(/too large/);
    const big = deps((url) => (url.includes("alt=media") ? new Response("a".repeat(DRIVE_MAX_BYTES + 5)) : meta({ name: "a.txt", mimeType: "text/plain" })));
    await expect(fetchDriveFileText("file0001", big)).rejects.toThrow(/too large/);
    await expect(fetchDriveFileText("../etc", deps(() => meta({})))).rejects.toThrow(/Invalid/);
  });
  it("never calls anything but the Drive API", async () => {
    const d = deps((url) => (url.includes("alt=media") ? new Response("hi") : meta({ name: "a.txt", mimeType: "text/plain" })));
    await fetchDriveFileText("file0001", d);
    for (const [url] of d.fetchFn.mock.calls) expect(url.startsWith("https://www.googleapis.com/drive/v3/")).toBe(true);
  });
});

describe("single-writer boundary", () => {
  it("drive code never writes the vault or storage", () => {
    const src = readFileSync("extension/lib/drive-import.js", "utf8");
    expect(src).not.toMatch(/chrome\.storage|userContextVault/);
  });
});

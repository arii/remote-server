import { describe, it, expect, vi } from "vitest";
import { MODEL_APIS, describeModelError, formatModelError, makeDownloadMonitor, modelProgressState } from "../extension/lib/model-download.js";

describe("modelProgressState", () => {
  it("maps availability to states", () => {
    expect(modelProgressState("available")).toMatchObject({ state: "ready", pct: 100 });
    expect(modelProgressState("downloadable")).toMatchObject({ state: "downloadable", pct: 0, label: "Not downloaded" });
    expect(modelProgressState("unavailable")).toMatchObject({ state: "unavailable", pct: null });
    expect(modelProgressState(undefined).state).toBe("unavailable");
  });

  it("downloading is indeterminate without a percent and never reports 100", () => {
    expect(modelProgressState("downloading")).toMatchObject({ state: "downloading", pct: null });
    expect(modelProgressState("downloading", 42)).toMatchObject({ pct: 42, label: "Downloading 42%" });
    expect(modelProgressState("downloading", 100).pct).toBe(99);
    expect(modelProgressState("downloading", -5).pct).toBe(0);
  });

  it("covers Prompt, Summarizer and Writer", () => {
    expect(MODEL_APIS.map((a) => a.global)).toEqual(["LanguageModel", "Summarizer", "Writer"]);
  });
});

function mockApi(events, availability = "downloadable") {
  return {
    availability: async () => availability,
    create: vi.fn(async ({ monitor }) => {
      const m = new EventTarget();
      monitor(m);
      for (const loaded of events) {
        const e = new Event("downloadprogress");
        e.loaded = loaded;
        m.dispatchEvent(e);
      }
      return { destroy: vi.fn() };
    })
  };
}

describe("makeDownloadMonitor", () => {
  it.each(["LanguageModel", "Summarizer", "Writer"])("reports clamped percent from %s create()", async () => {
    const api = mockApi([0, 0.256, 0.5, 1.7, -1, "x"]);
    const seen = [];
    await api.create({ monitor: makeDownloadMonitor((p) => seen.push(p)) });
    expect(seen).toEqual([0, 26, 50, 100, 0, 0]);
  });

  it("tolerates a missing callback", async () => {
    const api = mockApi([0.5]);
    await expect(api.create({ monitor: makeDownloadMonitor() })).resolves.toBeTruthy();
  });

  it("does not turn a live event into ready state", () => {
    const seen = [];
    const monitor = makeDownloadMonitor((p) => seen.push(modelProgressState("downloading", p)));
    const m = new EventTarget();
    monitor(m);
    const e = new Event("downloadprogress");
    e.loaded = 1;
    m.dispatchEvent(e);
    expect(seen[0]).toMatchObject({ state: "downloading", pct: 99 });
  });
});

describe("describeModelError", () => {
  const err = (name, message) => Object.assign(new Error(message), { name });

  it("classifies by error name or message", () => {
    expect(describeModelError(err("NotAllowedError", "x")).cause).toBe("permission");
    expect(describeModelError(err("QuotaExceededError", "x")).cause).toBe("disk");
    expect(describeModelError(err("Error", "Not enough disk space")).cause).toBe("disk");
    expect(describeModelError(err("NotSupportedError", "x")).cause).toBe("unsupported");
    expect(describeModelError(err("NetworkError", "x")).cause).toBe("network");
    expect(describeModelError(err("Error", "Failed to fetch")).cause).toBe("network");
  });

  it("never labels unknown errors as disk and keeps raw name and message", () => {
    const d = describeModelError(err("UnknownError", "model blew up"));
    expect(d).toMatchObject({ cause: "unknown", name: "UnknownError", message: "model blew up" });
    expect(d.steps.join(" ")).not.toMatch(/22 GB/);
    expect(formatModelError(d)).toContain("UnknownError: model blew up");
  });

  it("unavailable after failure is stated as ambiguous", () => {
    const d = describeModelError(err("Error", "boom"), { availability: "unavailable" });
    expect(d.cause).toBe("unavailable");
    expect(d.hint).toMatch(/does not say which/);
    expect(d.steps.join(" ")).toMatch(/22 GB/);
  });

  it("handles non-Error rejections", () => {
    expect(describeModelError("nope")).toMatchObject({ name: "Error", message: "nope", cause: "unknown" });
  });
});

import { describe, it, expect } from "vitest";
import { modelPanelView, observeView } from "../extension/lib/panel-state.js";

const r = (languageModel, summarizer, writer) => ({ languageModel, summarizer, writer });

describe("modelPanelView", () => {
  it("checking when report is null", () => {
    const v = modelPanelView(null);
    expect(v.phase).toBe("checking");
    expect(v.promptReady).toBe(false);
    expect(v.showProgress).toBe(false);
    expect(v.ctaText).toMatch(/Checking/);
  });
  it("ready hides progress and download", () => {
    const v = modelPanelView(r("available", "available", "available"));
    expect(v).toMatchObject({ phase: "ready", allReady: true, promptReady: true, showProgress: false, showDownloadButton: false, ctaText: "" });
    expect(v.summary).toBe("All 3 on-device models are ready.");
  });
  it("downloadable shows button and CTA", () => {
    const v = modelPanelView(r("downloadable", "downloadable", "downloadable"));
    expect(v).toMatchObject({ phase: "download", showDownloadButton: true, showProgress: true, promptReady: false });
    expect(v.ctaText).toMatch(/Download the built-in models/);
  });
  it("downloading shows unlock CTA", () => {
    const v = modelPanelView(r("downloading", "available", "available"));
    expect(v.phase).toBe("download");
    expect(v.ctaText).toMatch(/Downloading/);
  });
  it("prompt ready while another model downloads keeps tools and progress", () => {
    const v = modelPanelView(r("available", "downloading", "available"));
    expect(v).toMatchObject({ promptReady: true, showProgress: true, showDownloadButton: true, ctaText: "" });
  });
  it("writer unavailable: no download button, progress still shown", () => {
    const v = modelPanelView(r("available", "available", "unavailable"));
    expect(v).toMatchObject({ phase: "blocked", showDownloadButton: false, showProgress: true, promptReady: true });
    expect(v.summary).toContain("Writer (unavailable)");
  });
  it("prompt unavailable gives details CTA", () => {
    const v = modelPanelView(r("unavailable", "available", "available"));
    expect(v.phase).toBe("blocked");
    expect(v.ctaText).toMatch(/unavailable in this Chrome/);
  });
});

describe("observeView", () => {
  it("no hostname disables", () => {
    expect(observeView({})).toMatchObject({ label: "Open a website to start observing", disabled: true, pressed: false });
    expect(observeView()).toMatchObject({ disabled: true });
  });
  it("start when off", () => {
    expect(observeView({ hostname: "a.com", enabled: false })).toMatchObject({ label: "Start observing a.com", disabled: false, pressed: false });
  });
  it("stop when on", () => {
    expect(observeView({ hostname: "a.com", enabled: true })).toMatchObject({ label: "Stop observing a.com", pressed: true });
  });
});

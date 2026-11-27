import { describe, it, expect } from "vitest";
import { collapseRepeatedErrors } from "../extension/lib/model-download.js";

describe("collapseRepeatedErrors", () => {
  it("collapses identical texts after the first", () => {
    const items = [
      { key: "languageModel", label: "Prompt API", text: "Error message" },
      { key: "summarizer", label: "Summarizer", text: "Error message" },
      { key: "writer", label: "Writer", text: "Error message" },
    ];
    expect(collapseRepeatedErrors(items)).toEqual([
      { key: "languageModel", text: "Error message" },
      { key: "summarizer", text: "Same as Prompt API above." },
      { key: "writer", text: "Same as Prompt API above." },
    ]);
  });

  it("keeps distinct texts full", () => {
    const items = [
      { key: "languageModel", label: "Prompt API", text: "First" },
      { key: "summarizer", label: "Summarizer", text: "Second" },
      { key: "writer", label: "Writer", text: "Third" },
    ];
    expect(collapseRepeatedErrors(items)).toEqual([
      { key: "languageModel", text: "First" },
      { key: "summarizer", text: "Second" },
      { key: "writer", text: "Third" },
    ]);
  });

  it("keeps empty texts empty", () => {
    const items = [
      { key: "languageModel", label: "Prompt API", text: "" },
      { key: "summarizer", label: "Summarizer", text: "" },
    ];
    expect(collapseRepeatedErrors(items)).toEqual([
      { key: "languageModel", text: "" },
      { key: "summarizer", text: "" },
    ]);
  });

  it("collapses only the third when second differs", () => {
    const items = [
      { key: "languageModel", label: "Prompt API", text: "Same" },
      { key: "summarizer", label: "Summarizer", text: "Different" },
      { key: "writer", label: "Writer", text: "Same" },
    ];
    expect(collapseRepeatedErrors(items)).toEqual([
      { key: "languageModel", text: "Same" },
      { key: "summarizer", text: "Different" },
      { key: "writer", text: "Same as Prompt API above." },
    ]);
  });
});

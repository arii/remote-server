import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";

it("side panel re-renders the observer queue when the worker writes pendingDeltas", () => {
  const src = readFileSync(join(import.meta.dirname, "../extension/sidepanel.js"), "utf8");
  expect(src).toMatch(/changes\.pendingDeltas\) renderObserverQueue\(\)/);
});

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => vi.useRealTimers());

it("observes only trusted input, sends kind only and injects once; worker debounces", () => {
  vi.useFakeTimers();
  const listeners = {};
  const document = {addEventListener: vi.fn((type, listener) => { listeners[type] = listener; })};
  const chrome = {runtime: {sendMessage: vi.fn()}};
  const globals = {};
  const source = readFileSync(join(import.meta.dirname, "../takeover.js"), "utf8");
  const inject = () => new Function("document", "chrome", "globalThis", "Date", source)(document, chrome, globals, Date);
  inject(); inject();
  expect(document.addEventListener).toHaveBeenCalledTimes(3);
  listeners.keydown({isTrusted: false, key: "not-trusted"});
  expect(chrome.runtime.sendMessage).not.toHaveBeenCalled();
  listeners.keydown({isTrusted: true, key: "password", code: "KeyP"});
  listeners.pointerdown({isTrusted: true});
  vi.advanceTimersByTime(601);
  listeners.touchstart({isTrusted: true});
  expect(chrome.runtime.sendMessage.mock.calls).toEqual([[{type: "human_input", kind: "key"}], [{type: "human_input", kind: "pointer"}], [{type: "human_input", kind: "touch"}]]);
});

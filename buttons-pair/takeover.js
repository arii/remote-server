(() => {
  if (globalThis.__buttonsPairTakeover) return;
  globalThis.__buttonsPairTakeover = true;
  let lastInput = -Infinity;
  for (const [event, kind] of [["pointerdown", "pointer"], ["keydown", "key"], ["touchstart", "touch"]]) {
    document.addEventListener(event, (input) => {
      if (!input.isTrusted) return;
      const now = Date.now();
      const quiet = now - lastInput >= 600;
      lastInput = now;
      if (quiet) void chrome.runtime.sendMessage({type: "human_input", kind});
    }, {capture: true, passive: true});
  }
})();

(() => {
  if (globalThis.__buttonsPairTakeover) return;
  globalThis.__buttonsPairTakeover = true;
  for (const [event, kind] of [["pointerdown", "pointer"], ["keydown", "key"], ["touchstart", "touch"]]) {
    document.addEventListener(event, (input) => {
      if (!input.isTrusted) return;
      void chrome.runtime.sendMessage({type: "human_input", kind});
    }, {capture: true, passive: true});
  }
})();

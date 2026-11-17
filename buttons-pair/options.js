import { DEFAULT_ORIGIN, serverOrigin } from "./config.js";

const sites = ["http://*/*", "https://*/*"];
async function load() {
  const config = await chrome.storage.local.get({serverOrigin: DEFAULT_ORIGIN});
  document.querySelector("#origin").value = config.serverOrigin;
  document.querySelector("#sites").checked = await chrome.permissions.contains({origins: sites});
}

document.querySelector("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = document.querySelector("#message");
  try {
    const origin = serverOrigin(document.querySelector("#origin").value);
    const allSites = document.querySelector("#sites").checked;
    const origins = allSites ? sites : [`${origin}/*`];
    if (!await chrome.permissions.request({origins})) throw new Error("Site access was not granted. Settings were not changed.");
    await chrome.runtime.sendMessage({type: "disconnect"});
    await chrome.storage.local.set({serverOrigin: origin});
    message.textContent = "Saved. Click Connect in the popup when ready.";
  } catch (cause) {
    message.textContent = cause.message;
  }
});

void load().catch((cause) => { document.querySelector("#message").textContent = cause.message; });

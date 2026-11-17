import { DEFAULT_ORIGIN, serverOrigin } from "./config.js";

async function refresh(type = "status") {
  try {
    const state = await chrome.runtime.sendMessage({type});
    document.querySelector("#status").textContent = state.status;
    document.querySelector("#error").textContent = state.error;
    document.querySelector("#signin").href = `${serverOrigin(state.serverOrigin || DEFAULT_ORIGIN)}/buttons`;
    document.querySelector("#connect").disabled = state.optedIn && state.status === "Paired";
    document.querySelector("#disconnect").disabled = !state.optedIn;
  } catch (cause) {
    document.querySelector("#error").textContent = cause.message;
  }
}

document.querySelector("#connect").addEventListener("click", () => void refresh("connect"));
document.querySelector("#disconnect").addEventListener("click", () => void refresh("disconnect"));
document.querySelector("#options").addEventListener("click", () => void chrome.runtime.openOptionsPage());
void refresh();
setInterval(() => void refresh(), 1000);

# Buttons pair

Requires Chrome 116+. This is separate from LocalForm; nothing under
`chrome-extension/` is used or changed.

1. Open `chrome://extensions`, enable Developer mode, click **Load unpacked**,
   and select this `buttons-pair/` directory. No build is needed.
2. Sign in once to https://dev-onasafari.ddns.net:8443/buttons **in that Chrome
   profile**. The extension reuses the browser's OAuth cookie, not a password.
3. Click **Buttons pair** and **Connect**. Installation alone never pairs.
   The popup shows **Paired**, **Not paired**, or **sign in to Buttons first**.
4. Select the backend's `paired` target. The first operation creates a tab in
   the **Buttons agent** group. Chrome displays its **"is debugging this
   browser"** infobar. Closing the tab or cancelling debugging notifies Buttons.
5. Use **Disconnect** to stop pairing and automatic reconnection. It detaches
   debugging but leaves your tab open. Pairing opt-in survives browser restarts;
   a replacement pairing from another Chrome disables the old one's reconnect.

Options can change the HTTPS server origin; saving disconnects. The socket is
`wss://<server host>/buttons/api/browser/ext`. Default host access is only the
server above. To drive other sites, explicitly enable optional site access in
Options. Without access, takeover injection fails closed: agent input is refused.
Chrome internal pages cannot be driven. The `scripting` permission is necessary
for `chrome.scripting.executeScript`; other permissions are debugger, tabs,
tabGroups, storage and alarms. Optional host access is requested only by Save.

Only tabs created by this extension are attached. Trusted pointer/key/touch
input in that tab signals takeover without transmitting key values. Agent CDP
input is suppressed while in flight and for 300 ms afterward; bursts are
debounced for 600 ms. Human input during that suppression window is not reported.
Navigation reinjects the observer. Restrict commands to the agent tab; browser-
wide Target/Browser CDP commands and alternate tab/session identifiers are refused.

WebSocket traffic every 20 seconds keeps the MV3 worker active. The heartbeat
is `{}`, ignored by the existing backend protocol. Drops reconnect through one
alarm path with persisted backoff of 1, 2, 4, then 5 minutes (capped). A restart
detaches only the previously recorded agent tab before creating a fresh one.
No LLM or paid service is used by the extension.

Development: `pnpm --filter buttons-pair lint` and
`pnpm --filter buttons-pair test`. Root lint and extension tests include these;
the existing extension CI job runs them alongside LocalForm.

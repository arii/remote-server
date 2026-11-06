# Co-Browsing Architecture Guide: Container-Free Native Co-Browsing & Decommissioning Browser Containers

## Executive Summary

Following live evaluation of the `/browser/` route on the `buttons` console, the containerized browser approach (`browserless/chromium` with `DEMO_MODE=true`) demonstrated critical limitations:
1. **Not a Co-Browsing Interface**: Browserless rendered a Monaco code editor for running Node.js Puppeteer scripts ("Click play to run your code") instead of an interactive web browser.
2. **Resource Bloat**: Browser containers require reserving 2GB of shared memory (`shm_size: 2gb`) and persistent RAM/CPU on the TRIGKEY S5 home server.
3. **Virtual Desktop Redundancy**: Alternative desktop containers (`selenium/standalone-chromium`, `neko`, `kasmweb`) attempt to stream an entire X11 OS over VNC/WebRTC into an iframe, resulting in a laggy "browser-inside-a-browser" with awkward input translation, no shared session cookies, and zero password manager integration.

**The Solution**: Decommission all browser Docker containers from the S5 server stack and standardize on **Container-Free Native Co-Browsing via BrowserMCP Extension Pairing**. 

The AI agent on the S5 connects directly to the operator's **real native Chrome browser** on their machine over a secure tunnel. This delivers true, zero-latency visual co-browsing and instant human takeover with **zero Docker browser containers running on the server**.

---

## 1. Why We Are Getting Rid of Browser Docker Containers

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 The Container Problem                  │
                  │                                                        │
                  │   Docker on S5 (2GB RAM, X11, VNC/WebRTC, Chromium)   │
                  │                           ▼                            │
                  │   Streamed over WebSocket to Caddy /buttons iframe     │
                  │                           ▼                            │
                  │   Laggy video, no cookies, no password manager,        │
                  │   ephemeral sessions, "browser inside a browser"       │
                  └───────────────────────────┬────────────────────────────┘
                                              │
                                              ▼
                                       GET RID OF IT
                                              │
                                              ▼
                  ┌────────────────────────────────────────────────────────┐
                  │           Container-Free Native Co-Browsing            │
                  │                                                        │
                  │   Operator's Real Native Chrome Browser (on Laptop)    │
                  │                           ▲                            │
                  │                           │ BrowserMCP Extension       │
                  │                           │ + SSH/Tailscale Tunnel     │
                  │                           ▼                            │
                  │   S5 Agent (Pi, Muse, boomtick) Controls Tabs Directly │
                  │                                                        │
                  │   • Real tabs on your screen (0ms display latency)     │
                  │   • Native click/keyboard takeover (just click tab)    │
                  │   • Real cookies, saved logins, and passkeys           │
                  │   • 0MB Docker overhead on the S5 server               │
                  └────────────────────────────────────────────────────────┘
```

### 1.1 The Pitfalls of Browser Containers on the S5
* **`browserless/chromium`**: Strictly a headless scraping API. Its UI is an in-browser code editor to debug scripts, not a navigable browser. Sessions are ephemeral and self-destruct when an agent disconnects.
* **`selenium/standalone-chromium` & `m1k1o/neko`**: Run a full virtual X11 desktop inside Docker. This requires streaming high-framerate video across HTTP/WebSockets or UDP, introducing cursor lag, rendering artifacts, and eating valuable memory/CPU on the S5 mini PC.
* **Lack of Context**: A containerized browser starts cold with no cookies, no active logins, no stored credentials, and no browser extensions. Every session requires re-authenticating through 2FA/CAPTCHAs inside an awkward virtual desktop canvas.

---

## 2. The Native Co-Browsing Architecture: BrowserMCP Pairing

The repository already includes the optimal, container-free co-browsing infrastructure in [`docs/browser-mcp-pairing.md`](file:///home/ari/remote-server/docs/browser-mcp-pairing.md) and the `browser-relay` service in [`docker-compose.yml`](file:///home/ari/remote-server/docker-compose.yml).

### 2.1 How Native Co-Browsing Works
1. **Operator's Chrome**: You run your standard Chrome browser on your laptop or workstation.
2. **BrowserMCP Extension**: A lightweight Chrome extension connects to loopback port `9010` on your laptop.
3. **Secure Tunnel**: An SSH or Tailscale tunnel forwards port `9010` from your laptop to the S5 server (`terminal:19010` -> `browser-relay` -> `daemon 127.0.0.1:9010`).
4. **Agent Control**: When an agent on the S5 needs to browse, inspect UI, or test a web application, it sends CDP commands to the daemon.
5. **Real-Time Visual Co-Browsing**:
   * New tabs open directly in your native Chrome browser window.
   * You watch the agent navigate, fill forms, and click elements in real time on your actual screen.
   * You can immediately click into the tab to pause, solve a CAPTCHA, log in with your native password manager, or take over navigation.
   * When you are done, the agent seamlessly resumes with the authenticated session.

### 2.2 Benefits of Native Pairing Over Docker Containers
* **Zero Container Footprint**: No 2GB Chromium containers, no Xvfb, no VNC daemons running on the S5.
* **Zero Latency**: You interact with local hardware-accelerated Chrome, not a compressed video stream over a network socket.
* **Shared Authentication**: The agent operates within your authenticated browser environment, leveraging your existing session cookies and credentials without credential sharing.

---

## 3. Plan to Decommission `browserless` from the Stack

To cleanly remove `browserless` and revert the stack to a clean, container-free browser configuration:

### 3.1 Update [`docker-compose.yml`](file:///home/ari/remote-server/docker-compose.yml)

1. **Delete the `browserless` service block**:
   Remove lines 370–389:
   ```yaml
   # REMOVE THIS BLOCK:
   browserless:
     image: ghcr.io/browserless/chromium:latest
     container_name: remote-browserless-1
     shm_size: "2gb"
     restart: unless-stopped
     expose:
       - "3000"
     ports:
       - "127.0.0.1:3000:3000"
     environment:
       - DEMO_MODE=true
       - PREBOOT_CHROME=true
       - MAX_CONCURRENT_SESSIONS=5
       - CONNECTION_TIMEOUT=300000
       - TOKEN=${BROWSERLESS_TOKEN:-}
     logging: *default-logging
   ```

2. **Remove `BROWSERLESS_ENDPOINT` from the `terminal` service**:
   Remove from `terminal.environment`:
   ```yaml
   # REMOVE THIS LINE:
   BROWSERLESS_ENDPOINT: ${BROWSERLESS_ENDPOINT:-ws://browserless:3000}
   ```

3. **Retain `browser-relay`**:
   Keep the lightweight `browser-relay` Caddy container that bridges the laptop extension tunnel to the terminal's BrowserMCP daemon.

### 3.2 Update [`caddy/Caddyfile`](file:///home/ari/remote-server/caddy/Caddyfile)

Remove the `/browser*` routing blocks:

```caddy
# REMOVE THESE BLOCKS:
handle /browser {
    redir * /browser/debugger/
}

handle /browser/ {
    redir * /browser/debugger/
}

handle /browser* {
    uri strip_prefix /browser
    header -X-Frame-Options
    reverse_proxy browserless:3000 {
        transport http {
            dial_timeout 10s
        }
    }
}
```

### 3.3 Clean Up Environment & Documentation

1. **`.env.example`**: Remove `BROWSERLESS_TOKEN`.
2. **`docs/browser-setup.md`**: Deprecate this document and redirect readers to [`docs/browser-mcp-pairing.md`](file:///home/ari/remote-server/docs/browser-mcp-pairing.md).

---

## 4. Handling Automated Visual Audits (`td audit-impact`)

In [`boomtick/cli/dev_tools/services/browser_cdp.py`](file:///home/ari/boomtick/cli/dev_tools/services/browser_cdp.py), `td audit-impact --json` captures DOM and visual snapshots when UI files change.

Without a dedicated browser container running on the S5:
1. **Paired Mode**: When the operator's laptop Chrome is paired via BrowserMCP, `td audit-impact` can connect to the local relay (`http://127.0.0.1:9010`) to capture snapshots directly from the paired Chrome.
2. **Standalone / Unpaired Mode**: If no browser is connected, `capture_dom_and_visual_snapshot` already handles connection failures gracefully by logging to stderr and returning `VisualDiffResult(reachable=False, error="BROWSER_UNREACHABLE")`. The static AST impact analysis, import dependent graph, and syntax gates continue to execute and validate code deterministically without blocking on a visual diff.

---

## 5. Summary of Architecture Changes

| Component | With Docker Browser Containers | With Native BrowserMCP Pairing (New Baseline) |
| :--- | :--- | :--- |
| **Containers on S5** | `browserless` (2GB RAM) + `terminal` + `buttons` | `terminal` + `buttons` + `browser-relay` (0MB extra browser RAM) |
| **Browser Interface** | Monaco code editor iframe at `/browser/` | Real native Chrome browser tabs on operator's laptop |
| **Streaming Protocol** | WebSocket canvas streaming / noVNC | Direct Chrome DevTools Protocol over local SSH/Tailscale tunnel |
| **Human Takeover** | Click inside an iframe; no cookies or saved passwords | Native click into Chrome tab; full password manager & session auth |
| **Host Maintenance** | Managing container lifecycle, memory crashes, image pulls | Zero container maintenance; uses the browser you already run |

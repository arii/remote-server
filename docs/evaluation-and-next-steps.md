# Evaluation, Button Audit, and Next Steps — 2026-10-29

## 1. Executive Summary & Critical Issue Resolvings

### A. Repos Dropdown Only Showing `remote-server` (Resolved)
- **Root Cause:** In `_discover_repos()`, the server ran `git -C <host_path> config --get remote.origin.url` without `-c safe.directory=<host_path>`. Because `buttons` runs as `root` while `/home/ari/repos` is owned by `1000:1000` (`ari`), git flagged dubious ownership and exited with code 1. Discovery logged `skipping boomtick/tech-dancer (no parseable GitHub origin)` and fell back to the hardcoded `remote-server` entry.
- **Resolution (`b66a676`):** Added `safe.directory={host_path}` to `git config` in `_discover_repos()`.
- **Live Status:** All three repositories now discover dynamically and populate the dropdown: `boomtick`, `remote-server`, and `tech-dancer`.

### B. BoomTick Out-of-Sync, Missing Webhook & 0.15.0 Upgrade (Resolved)
- **Root Cause of BoomTick Errors:**
  1. **20 Commits Behind:** Local `/srv/repos/boomtick` was stuck on `0.14.3` (`f19f28e`), while GitHub was on `0.15.0` (`15dc98a`).
  2. **Dirty Schema Blocked Pulls:** An earlier `td context-warm` modified `cli/dev_tools/cli-schema.json`, preventing automated git pulls.
  3. **No Webhook Registered:** `scripts/setup-webhooks.sh` only registered `remote-server` and `tech-dancer`. GitHub never sent push events for `boomtick`.
  4. **Missing Node Modules:** `pnpm install` had never been run in `boomtick`, causing `preflight` / `type-check` to fail with `tsx: not found` and `run-p: not found`.
  5. **Stale Installed CLI:** `/opt/venvs/td` had `boomtick-cli 0.14.3` installed in non-editable mode.
- **Fixes Applied & Verified:**
  - Discarded unstaged schema changes and fast-forwarded `boomtick` to latest `origin/main` (`15dc98a`, tagged `cli-v0.15.0`, `mcp-v0.7.0`).
  - Ran `pnpm install` in `boomtick` (installed dependencies for `@arii/boomtick-mcp` and dev tooling).
  - Installed `boomtick-cli 0.15.0` in editable mode (`pip install -e /srv/repos/boomtick/cli`) inside `/opt/venvs/td`, so updates in `boomtick/cli` take effect immediately.
  - Added `arii/boomtick` to `scripts/setup-webhooks.sh` and created webhook `704572541` on GitHub. Verified incoming ping and pull (`ok: 16 files changed, 778 insertions(+), 50 deletions(-)`).
  - Added `boomtick` to `scripts/setup-hooks.sh` and installed the `post-merge` hook.

### C. Access URL & Navigation Clarification (`buttons` is not a valid URL)
- Entering plain `buttons` or `http://buttons` into a mobile browser address bar fails or triggers a web search because `buttons` is not a top-level domain or hostname.
- **Correct Full URL:**
  ```text
  https://dev-onasafari.ddns.net:8443/buttons
  ```
  (or `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>/buttons`). Both `/buttons` and `/buttons/` route through oauth2-proxy (GitHub OAuth gate) and Caddy to the buttons service on port 8000. Navigating to `/` opens the interactive web terminal (`ttyd` + `zellij`).

### D. Agent Command Permissions Configured (No Manual Approval Clicking)
- Updated `/root/.gemini/antigravity-cli/settings.json` under `permissions.allow` to permit binary prefixes:
  `command(git)`, `command(docker)`, `command(gh)`, `command(npm)`, `command(pnpm)`, `command(td)`, `command(python3)`, `command(curl)`, `command(bash)`, `command(sh)`, `command(/opt/venvs/td/bin/pip)`.
- Eliminates the repetitive approval dialogs for git, docker, and development commands.

---

## 2. Button UX Evaluation: Why Buttons Fail & How to Improve Mobile Usability

User feedback highlighted that multiple buttons fail with errors and are difficult to use on mobile:

### 1. `Sync & Nudge Agents` Hard-Fails when `JULES_API_KEY` is Unset
- **Behavior:** The button runs `td --json agent list-sessions` followed by `td --json agent run-feedback-check`.
- **Failure:** If `JULES_API_KEY` is not defined in `.env`, `td agent list-sessions` exits with code 1 (`ValueError: JULES_API_KEY is not set or empty`). The button aborts immediately, turns the status bar red, and shows `failed (exit 2) — step 1/2`.
- **UX Problem:** The user sees a red failure banner with no immediate explanation unless they scroll through the raw output.
- **Remedy:** Gracefully check if `JULES_API_KEY` is set and output an actionable notice (`"Jules not configured: set JULES_API_KEY in .env to use agent sync"`) rather than an aborting exit-code failure.

### 2. `Preflight Check` Dumps Megabytes of Raw JSON and Fails on Linter Warnings
- **Behavior:** `td --json preflight` runs `doctor`, `typecheck`, `lint`, `audit`, and `merge-simulation`.
- **Failure:** In `tech-dancer`, Python linter (`pylint`) fails with exit code 1 because optional ETL packages (`pandas`, `playwright`, `bs4`) are not installed in the global python environment.
- **UX Problem:** Preflight returns a 30,000+ character JSON dump. On a phone, reading this inside a `<pre>` block is cumbersome.
- **Remedy:** Parse the preflight output to show a concise high-level checklist (e.g., `Doctor: ✓, Types: ✓, Lint: ✗ (pylint warnings)`) instead of raw JSON.

### 3. Error Feedback is Buried in Raw `<pre>` Logs
- **Behavior:** When an action fails, the top status banner only says:
  `failed (exit 1) — step 1/1 — 0s\nsee the “step failed” line at the end of the output`
- **UX Problem:** On mobile screens, the user must scroll past hundreds of lines to find the actual reason (e.g. missing API key, uncommitted change, or lint failure).
- **Remedy:** Extract the terminal error summary or top-level JSON `"message"` and display it directly in the status bar in plain English.

---

## 3. Current Button Inventory & Classification

| Button | Target Repo | Failure Modes & Usability Issues | Recommended Treatment |
|---|---|---|---|
| **Status** | Stack | Very stable (<1s). | Keep as-is. |
| **Deploy buttons** | Stack | Stable (runs via `terminal` container). | Keep as-is. |
| **Recreate terminal** | Stack | Stable (container recreate). | Keep as-is. |
| **Logs** | Stack | Renders terminal logs + Pi logs. | Keep as-is. |
| **Repo briefing** | Selected | Fast (~7s), reliable. Shows Jules warning if key missing. | Keep as-is. |
| **Sync & nudge agents** | Stack / Repos | **Fails red** if `JULES_API_KEY` missing. | Update step to check for key or exit 0 with reminder. |
| **Warm context** | Selected | Fast (~1s). | Keep as-is. |
| **Preflight check** | Selected | **Fails red** on linter warnings, dumps 30KB JSON. | Parse JSON to show concise checklist (Doctor ✓, Types ✓, Lint ✗). |
| **Agent suggests next actions** | Selected | Slow (Pi inference); requires clear spinner. | Keep allowlisted execution. |

---

## 4. Current Repository Status & Next Steps

All three repositories are verified up to date on `main`:
- **`remote-server`:** `e7c018f` (all webhook, discovery, and documentation updates pushed).
- **`tech-dancer`:** `b906042cae` (tagged `tech-dancer-v0.10.0`, all recent PRs merged clean).
- **`boomtick`:** `15dc98a` (tagged `cli-v0.15.0` & `mcp-v0.7.0`, pnpm dependencies installed, CLI installed in editable mode).

### Next Actions:
- [ ] Test the live briefing on `boomtick` by visiting `https://dev-onasafari.ddns.net:8443/buttons` and selecting `boomtick`.
- [ ] Implement error extraction in `buttons/server.py` so mobile failures display human-readable root causes rather than generic exit-code banners.
- [ ] Add graceful fallback in `Sync & nudge agents` when `JULES_API_KEY` is not present in `.env`.

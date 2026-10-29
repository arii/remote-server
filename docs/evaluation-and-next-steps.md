# Evaluation, Button Audit, and Next Steps — 2026-10-29

## 1. Executive Summary & Critical Issue Resolvings

### A. Repos Dropdown Only Showing `remote-server` (Resolved)
- **Root Cause:** In `_discover_repos()`, the server ran `git -C <host_path> config --get remote.origin.url` without `-c safe.directory=<host_path>`. Because `buttons` runs as `root` while `/home/ari/repos` is owned by `1000:1000` (`ari`), git flagged dubious ownership and exited with code 1. Discovery logged `skipping boomtick/tech-dancer (no parseable GitHub origin)` and fell back to the hardcoded `remote-server` entry.
- **Resolution (`b66a676`):** Added `safe.directory={host_path}` to `git config` in `_discover_repos()`.
- **Live Status:** All three repositories now discover dynamically and populate the dropdown: `boomtick`, `remote-server`, and `tech-dancer`.

### B. BoomTick Out-of-Sync & Missing Webhook (Resolved)
- **Root Cause of BoomTick Errors:**
  1. **20 Commits Behind:** The local checkout `/srv/repos/boomtick` was stuck on `0.14.3` (`f19f28e`), while GitHub was on `0.15.0` (`15dc98a`).
  2. **Dirty Schema Blocked Pulls:** An earlier `td context-warm` modified `cli/dev_tools/cli-schema.json`, preventing automated git pulls.
  3. **No Webhook Registered:** `scripts/setup-webhooks.sh` only registered `remote-server` and `tech-dancer`. GitHub never sent push events for `boomtick`.
  4. **Missing Node Modules:** `pnpm install` had never been run in `boomtick`, causing `preflight` / `type-check` to fail with `tsx: not found` and `run-p: not found`.
  5. **Stale Installed CLI:** `/opt/venvs/td` had `boomtick-cli 0.14.3` installed in non-editable mode.
- **Fixes Applied:**
  - Discarded unstaged schema changes and fast-forwarded `boomtick` to `0.15.0` (`15dc98a`).
  - Ran `pnpm install` in `boomtick` (installed 479 packages).
  - Reinstalled `boomtick-cli 0.15.0` in editable mode (`pip install -e /srv/repos/boomtick/cli`) inside `/opt/venvs/td`.
  - Added `arii/boomtick` to `scripts/setup-webhooks.sh` and created webhook `704572541` on GitHub. Verified incoming ping and pull (`ok: 16 files changed, 778 insertions(+), 50 deletions(-)`).
  - Added `boomtick` to `scripts/setup-hooks.sh` and installed `hooks/post-merge-tech-dancer` as `/srv/repos/boomtick/.git/hooks/post-merge`.

---

## 2. Button UX Evaluation: Why Buttons Are Failing & Difficult to Use on Mobile

User feedback flagged that multiple buttons fail with errors and the interface is difficult to use on a phone. An audit of button execution revealed clear causes:

### 1. `Sync & Nudge Agents` Hard-Fails when `JULES_API_KEY` is Unset
- **Behavior:** The button runs `td --json agent list-sessions` followed by `td --json agent run-feedback-check`.
- **Failure:** If `JULES_API_KEY` is not defined in `.env`, `td agent list-sessions` exits with code 1 (`ValueError: JULES_API_KEY is not set or empty`). The button aborts immediately, turns the status bar red, and shows `failed (exit 2) — step 1/2`.
- **UX Problem:** The user is left with a red failure box and no explanation unless they scroll through the raw terminal output. It should degrade gracefully with a clear message: `"Jules not configured: set JULES_API_KEY in .env to use agent sync"`.

### 2. `Preflight Check` Dumps Megabytes of Raw JSON and Fails on Linter Warnings
- **Behavior:** `td --json preflight` runs `doctor`, `typecheck`, `lint`, `audit`, and `merge-simulation`.
- **Failure:** In `tech-dancer`, Python linter (`pylint`) fails with exit code 1 because optional ETL packages (`pandas`, `playwright`, `bs4`) are not installed in the global python environment. In `boomtick`, preflight previously failed because `node_modules` was missing.
- **UX Problem:** Preflight returns a 30,000+ character JSON structure with nested stack traces. On a phone, reading this inside a `<pre>` block is nearly impossible.

### 3. Error Feedback is Buried in Raw `<pre>` Logs
- **Behavior:** When any action fails, the top status banner only says:
  `failed (exit 1) — step 1/1 — 0s\nsee the “step failed” line at the end of the output`
- **UX Problem:** On mobile screens, the user must scroll past hundreds of lines to find the actual reason (e.g. missing API key, uncommitted change, or lint failure).
- **Solution Needed:** The UI should extract the terminal error summary or top-level JSON `"message"` and display it directly in the status bar in plain English.

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

## 4. Prioritized Next Steps

1. **Improve Mobile UX for Error Reporting:**
   - [ ] Update `buttons/server.py` to extract the primary error message from stderr/JSON payloads so the top status bar displays the actual cause (e.g., `"Error: JULES_API_KEY is not set"` or `"Linting failed with exit 1"`) instead of generic `"see step failed line"`.
   - [ ] Make `Sync & nudge agents` check for `JULES_API_KEY` and print a friendly configuration prompt rather than turning the status pane red.

2. **Verify BoomTick End-to-End:**
   - [x] Fast-forward `boomtick` to `0.15.0` (`15dc98a`).
   - [x] Run `pnpm install` in `boomtick`.
   - [x] Install `boomtick-cli 0.15.0` editable in `td` venv.
   - [x] Register GitHub webhook `704572541` for `boomtick` and test ping/pull.
   - [ ] Test `Repo briefing (td, fast)` with `boomtick` selected in `/buttons`.

3. **Process `tech-dancer` PR Queue:**
   - [ ] Use the live one-tap **Merge** button on **PR #4827** (`chore/update-config-for-td`) and **PR #4826** (`dependabot/npm_and_yarn`).
   - [ ] Allow `release-please` to refresh and resolve conflicts on **PR #4814** for release `0.10.0`.

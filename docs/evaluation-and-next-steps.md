# Evaluation, Button Audit, and Next Steps — 2026-10-29

## 1. Executive Summary & Current Stack State

All three repositories are verified on their latest releases, fully synchronized with GitHub, and wired into the automated webhook / hook infrastructure:

| Repository | Current Release / Head | Open PRs | Auto-Pull Webhook | Tracked Post-Merge Hook |
|---|---|---|---|---|
| **`remote-server`** | `c5a0aa0` (latest `main`) | 0 | Registered (`704473987`) | `/srv/remote-server/.git/hooks/post-merge` |
| **`tech-dancer`** | `1fbc9c9` (`v0.10.0`) | **0** (all merged clean) | Registered (`704473991`) | `/srv/repos/tech-dancer/.git/hooks/post-merge` |
| **`boomtick`** | `37fd8da` (`cli-v0.15.1`, `mcp-v0.7.1`) | **0** (clean release) | Registered (`704572541`) | `/srv/repos/boomtick/.git/hooks/post-merge` |

- **Live Buttons Service:** Rebuilt and reloaded as `remote-buttons:local`. Both `boomtick` and `tech-dancer` return live briefings with zero open PRs and clean boards.
- **Auto-Approval Configured:** `/root/.gemini/antigravity-cli/settings.json` permits `git`, `docker`, `gh`, `npm`, `pnpm`, `td`, `python3`, `curl`, `bash`, `sh`, and `/opt/venvs/td/bin/pip` without manual prompt clicking.
- **Valid Access URL:** The phone UI is reached at:
  ```text
  https://dev-onasafari.ddns.net:8443/buttons
  ```
  (Entering plain `buttons` in a browser search bar fails because `buttons` is an internal route on port 8443, not a standalone URL.)

---

## 2. Issues Investigated & Resolved During Reload

### A. Repos Dropdown Multi-Repo Discovery (`b66a676`)
- **Issue:** Dropdown previously only listed `remote-server`.
- **Cause:** `_discover_repos()` ran `git -C <path> config --get remote.origin.url` without `-c safe.directory=<path>`. Git flagged dubious ownership across the container `root` and host `1000` (`ari`) boundary and returned exit 1.
- **Fix:** Added `-c safe.directory={host_path}` to `_discover_repos()`. Dropdown now dynamically lists `boomtick`, `remote-server`, and `tech-dancer`.

### B. BoomTick State Synchronization & Release Upgrade
- **Issue:** `boomtick` was out of sync on server, had an unstaged schema diff, lacked a GitHub webhook, and missed node dependencies.
- **Fixes Applied & Verified:**
  - Fast-forwarded `boomtick` to `37fd8da` (`cli-v0.15.1`, `mcp-v0.7.1`).
  - Ran `pnpm install --frozen-lockfile` (479 packages added for MCP and dev tooling).
  - Reinstalled `boomtick-cli 0.15.1` in editable mode (`pip install -e /srv/repos/boomtick/cli`) inside `/opt/venvs/td`.
  - Registered GitHub webhook `704572541` on `arii/boomtick` and verified incoming push deliveries.
  - Pre-warmed `.agent-context.json` via `td context-warm`.

### C. Tech-Dancer PR Queue Cleared
- **`chore(main): release tech-dancer 0.10.0` (#4814)** has been merged into `main`. Both #4827 and #4826 were incorporated.
- The `tech-dancer` board now reports 0 open PRs and clean statusCheckRollups. Context has been refreshed via `td context-warm`.

---

## 3. Button UX Evaluation: Mobile Pain Points & Proposed Polishing

User testing confirmed that button feedback on mobile needs targeted refinement:

1. **`Sync & Nudge Agents` Graceful Handling:**
   - Currently, if `JULES_API_KEY` is not in `.env`, `td agent list-sessions` exits with code 1 (`ValueError: JULES_API_KEY is not set or empty`), which turns the entire status banner red.
   - **Recommendation:** Wrap step 1 to check for `JULES_API_KEY` or catch exit 1 and report: `"Notice: JULES_API_KEY is unset; add to .env to enable active session syncing."` without turning the run status red.

2. **Error Extraction for Mobile Status Banner:**
   - The current banner (`failed (exit 1) — see "step failed" line at the end of output`) forces users to scroll through hundreds of lines in a mobile `<pre>` tag.
   - **Recommendation:** Extract the top-level error line or JSON `"message"` from the output and display it directly in the `#state` banner.

3. **Preflight JSON Digest:**
   - `td preflight` returns 30KB+ of nested JSON with oxlint/pylint/semgrep reports.
   - **Recommendation:** Render a compact checklist (`Doctor: OK, Typecheck: OK, Lint: Warnings`) instead of dumping raw JSON into the output pre-block.

---

## 4. Current Button Inventory

| Button | Target Repo | Role on Phone UI | Current Status |
|---|---|---|---|
| **Status** | Stack | Quick health check (git status, compose ps, terminal image). | Active & fast (<1s). |
| **Deploy buttons** | Stack | Rebuilds the buttons container via `terminal` container without self-kill. | Active. |
| **Recreate terminal** | Stack | Recreates `terminal` after `.env` changes or if `zellij` hangs. | Active escape hatch. |
| **Logs** | Stack | Tails 100 lines of `terminal` + `ollama` + Pi session audit. | Active. |
| **Repo briefing** | Selected | Fast (~7s) triage of open PRs, CI, conflicts, and reviews. | Active (shows 0 PRs on clean boards). |
| **Sync & nudge agents** | Stack / Repos | Syncs Jules sessions and runs automated CI feedback check. | Active (needs `JULES_API_KEY`). |
| **Warm context** | Selected | Regenerates `.agent-context.json` for agent tools. | Active (~1s). |
| **Preflight check** | Selected | Full-stack doctor, typecheck, lint, and merge simulation. | Active. |
| **Agent suggests next actions** | Selected | Pi analyzes briefing and renders allowlist-validated one-tap action buttons. | Active (`ef294e5`). |

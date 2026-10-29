# Evaluation, Button Audit, and Next Steps — 2026-10-29

## 1. Executive Summary & Root-Cause Analysis

### A. Root Cause: Repos Dropdown Only Showing `remote-server`
When multi-repo discovery landed (`93d9252`), the dropdown in `/buttons` displayed only `remote-server` and omitted `boomtick` and `tech-dancer`.

- **Root Cause:** In `_discover_repos()`, the server ran `git -C <host_path> config --get remote.origin.url` without `-c safe.directory=<host_path>`. Because the `buttons` container runs as `root` while the checkouts in `/home/ari/repos` are owned by `1000:1000` (`ari`), git flagged "dubious ownership" and exited with code 1. Discovery logged `skipping boomtick (no parseable GitHub origin)` and fell back to the hardcoded `remote-server` entry.
- **Resolution (`b66a676`):** Added `safe.directory={host_path}` to `git config` in `_discover_repos()`.
- **Live Status:** Verified live in `remote-buttons-1`. The `<select id="briefingRepo">` dropdown and repo strip cards now dynamically discover and present all three checkouts:
  - `boomtick` (`arii/boomtick`)
  - `remote-server` (`arii/remote-server`)
  - `tech-dancer` (`arii/tech-dancer`)

---

## 2. Evaluation of Recent Architectural Evolution (`6a97c2d..ef294e5`)

The buttons page has completed the transition from a **passive server/status dashboard** into an **active agentic control plane**:

1. **Dead Weight Trimmed (`b845501`):**
   - **Removed `Pull models`:** Multi-GB one-off downloads no longer occupy prime phone screen space; kept in `package.json` (`npm run models:pull`).
   - **Removed `Repo briefing (agent, slow)`:** Eliminated the slow (multi-minute) CPU 8b model paraphrase in favor of deterministic ~7s briefings.
   - **Replaced static Plans markdown checklist:** The old `.agents/plans/<date>-briefing-plan.md` checkboxes (where tapping a box on a phone only edited an untracked text file) were replaced with the live Briefing view.

2. **Actionable PR Cards with One-Tap Operations (`b845501`):**
   - Each open PR card in the live Briefing view now renders contextual, allowlisted action buttons validated strictly by integer `pr_number` and repo name:
     - **Merge (squash):** Enabled only when CI is passing and merge is clean (e.g. PR #4827 and PR #4826).
     - **Update branch:** Rebase/updates branch via GitHub API when behind base.
     - **Fix CI with agent:** Dispatches `td agent fix-ci --pr <N>` directly from the phone.

3. **Real Agentic Control Buttons (`b845501`, `fa16d41`):**
   - **Sync & Nudge Agents:** Runs `td agent sync` and `td agent run-feedback-check` to sync active Jules sessions and post CI/review feedback to unblock stalled agent PRs.
   - **Warm Repo Context:** Runs `td context-warm` to regenerate `.agent-context.json` so interactive agents (`agy`, `pi`, `td`) start with fresh semantic indexes.
   - **Preflight Check:** Runs `td preflight` (or `td doctor`) across the selected repo.

4. **Agent Suggests Next Actions (`ef294e5`):**
   - A dedicated **Agent suggests next actions** action invokes Pi with the live briefing payload. Pi recommends up to 3 concrete `td` commands, strictly filtered against an allowlist (`td gh`, `td agent`, `td context-warm`, `td preflight`).
   - Each suggested action renders with a one-tap **Approve & run** button on the phone.

5. **Operational Reliability & Resilience:**
   - **Deploy in Terminal (`d3ef069`):** Deploy buttons now executes inside the `terminal` container via `docker compose up -d --build buttons`, eliminating the container self-kill race condition.
   - **Durable Audit Ledger (`ecd6c55`, `d4cf925`):** Appends all PR actions and agent runs to a JSONL audit ledger stored on the persistent `buttons-data` volume (`/var/lib/buttons/ledger.jsonl`), complete with **Clear ledger** and **Clear Pi sessions** controls.
   - **Stale Page Detection (`2811065`):** Shows an amber banner if a webhook auto-pull advanced the checkout after the page was loaded.

---

## 3. Current Button Inventory & Classification

| Button | Scope | Classification | Role on Phone UI |
|---|---|---|---|
| **Status** | Stack | Server utility | Quick health check (git status, compose ps, terminal image). |
| **Deploy buttons** | Stack | Server utility | Rebuilds the buttons container via `terminal` container without self-kill. |
| **Recreate terminal** | Stack | Escape hatch | Recreates `terminal` after `.env` changes or if `zellij`/`ttyd` hangs. |
| **Logs** | Stack | Server utility | Tails 100 lines of `terminal` + `ollama`. |
| **Repo briefing (td, fast)** | Repo-scoped | Agentic triage | Runs `briefing_summary.py` on selected repo; feeds live Briefing view. |
| **Sync & nudge agents** | Stack / Multi-repo | Agentic workflow | Syncs Jules sessions and runs feedback loop to unblock stalled agent PRs. |
| **Warm context** | Repo-scoped | Agentic workflow | Updates `.agent-context.json` index in the selected repo. |
| **Preflight** | Repo-scoped | Agentic workflow | Validates environment, dependencies, and repo configuration. |
| **Agent suggests next actions** | Repo-scoped | Autonomous assistance | Pi analyzes briefing and renders allowlist-validated one-tap action buttons. |

---

## 4. Prioritized Next Steps

1. **Verify Multi-Repo Briefings on Phone:**
   - [x] Verify `tech-dancer` briefing and PR actions load properly.
   - [ ] Switch the **Repo:** dropdown on `/buttons` to `boomtick` and confirm briefing and Jules session listing load cleanly.
   - [ ] Confirm `remote-server` preflight and status operate cleanly under the new multi-repo layout.

2. **Process `tech-dancer` PR Queue via One-Tap Phone UI:**
   - [ ] On `/buttons` with `tech-dancer` selected:
     1. Tap **Merge** on **PR #4827** (`chore/update-config-for-td` — clean, CI passing).
     2. Tap **Merge** on **PR #4826** (`dependabot/npm_and_yarn` — clean, CI passing).
     3. Once #4826 merges, allow `release-please` to update **PR #4814** (clearing the `package.json` conflict), then merge #4814 for release `0.10.0`.

3. **Webhook Auto-Warming Enhancement:**
   - [ ] Wire automatic `td context-warm` execution in the terminal container when a push webhook arrives for `tech-dancer` on `main`, ensuring the repo context is warm without manual taps.

# Evaluation, Button Audit, and Next Steps — 2026-10-28

## 1. Summary of Recent Changes & Current State (`4207693..5f70072`)

1. **Buttons UI, PTY ANSI output, and Briefing Summarizer (`7aa4770..b9be1e3`):**
   - Grouped buttons under **Server** and **Briefing** with subtitles, PTY-backed ANSI colour output (`_run_step`), and plan excerpts.
   - Added `buttons/briefing_summary.py` combining `td --json` (`search-prs`, `status-board`, `detect-conflicts --all`, `workflow-runs`) with per-PR `gh pr view` enrichment (`statusCheckRollup`, `reviewDecision`, `mergeable`, `mergeStateStatus`).
   - Fixed root `td --json` flag placement, `"prs"` list conflict parsing (`{"prs": [4814, 4826], "files": ["package.json"]}`), and `head_branch`/`html_url` workflow run fields in `buttons/briefing_summary.py` (`b9be1e3`).
2. **GitHub Webhook Auto-Pull & Tracked Git Hooks (`ca0c21b..5f70072`):**
   - Installed tracked `post-merge` hooks into both `/srv/remote-server/.git/hooks/post-merge` and `/srv/repos/tech-dancer/.git/hooks/post-merge` via `npm run hooks:install`.
   - Configured `WEBHOOK_SECRET` in `.env`, upgraded `oauth2-proxy` to `v7.15.2` with `--skip-auth-route=POST=^/buttons/api/webhook$`, and rebuilt `buttons` with the `/home/ari/repos` bind mount.
   - Fixed `gh api` boolean field syntax (`-F active=true`) in `scripts/setup-webhooks.sh` and registered push webhooks on both `arii/remote-server` (`704473987`) and `arii/tech-dancer` (`704473991`), verified end-to-end via GitHub ping and push deliveries (`5f70072`).

---

## 2. Audit of Current Buttons: What Hooks Replace vs. What Is Useful

With **GitHub webhook auto-pull** (every push to `main` automatically fast-forwards `/home/ari/remote-server` and `/home/ari/repos/tech-dancer`) and **tracked `post-merge` hooks** (interactive pulls automatically rebuild `buttons` or run `pnpm install --frozen-lockfile`), several buttons on `/buttons` are now redundant or low-value clutter.

### A. Buttons That Are Unnecessary or Redundant Now

| Current Button | Why It Is No Longer Necessary | Recommendation |
|---|---|---|
| **Pull models** (`pull-models`) | Models (`qwen3:8b`, `qwen3:14b`) live in the persistent `ollama-data` volume and are only pulled on initial bring-up or when `.env` changes `OLLAMA_MODELS`. Keeping a permanent full-width phone button for a multi-GB one-off script adds clutter and accidental-tap risk. | **Remove from `/buttons`** (keep `scripts/pull-models.sh` as an `npm` script / shell command). |
| **Repo briefing (agent, slow)** (`briefing-agent`) | Runs `pi -p` against the local CPU-only 8b model, taking minutes to paraphrase the exact same data that `briefing_summary.py` (`Repo briefing (td, fast)`) computes deterministically in ~7 seconds. It takes no action and often hits tool/latency limits on CPU. | **Remove from `/buttons`**. |
| **Create plan from briefing** (`create-plan`) + **Plans checkboxes** | Duplicates `Repo briefing (td, fast)` by saving its output to `.agents/plans/<date>-briefing-plan.md` with static checkboxes. Ticking a checkbox on the phone only flips `[ ]` to `[x]` in an untracked file — nothing reads or executes those checkboxes. Meanwhile, the expanded plan card has two more buttons (`Re-run briefing`, `Create a new plan`) that duplicate the main buttons right above it. | **Replace or fold into a single live Briefing + Action view** (see Section 3). |
| `git pull` inside **Deploy buttons** (`deploy-buttons`) | Pushes to `main` on `arii/remote-server` are now auto-pulled within seconds by the webhook, and interactive host pulls already trigger `hooks/post-merge` to rebuild `buttons`. The only thing the webhook intentionally does *not* do is rebuild the `buttons` container (so it doesn't kill itself mid-pull). | **Keep only as a compact Server utility** (`Rebuild buttons`) for applying webhook-pulled changes to `buttons/server.py`, or trigger container recreation via a host-side watcher/systemd path unit so even that button disappears. |

### B. Buttons That Are Genuinely Useful in General (Ops & Escape Hatches)

Only three server actions earn their keep on a phone screen as **infrastructure escape hatches** — specifically for moments when you don't want to type in the terminal or when the terminal itself is the thing that needs restarting:

1. **Status (`status`)**: Fast (<1s) glance at `git status`, recent commits, `docker compose ps`, and the active terminal image.
2. **Logs (`logs`)**: Quick tail of `terminal`, `ollama`, and `buttons` logs without opening a zellij pane.
3. **Recreate terminal (`recreate`)**: Essential escape hatch when `.env` is updated or if `ttyd`/`zellij` inside `terminal` ever hangs (since you cannot use a wedged terminal to recreate itself).

---

## 3. Why the Current Page Doesn't Help Agentic Work (and What Would)

### The Core Problem: Read-Only Observation Disguised as Workflow

Right now, `/buttons` does two things:
1. **Server plumbing** (rebuilding/restarting containers), most of which is now automated by webhooks and hooks.
2. **Passive reporting** (`Repo briefing (td, fast)` + `Plans` checkboxes).

When you are on your phone, **reading a static markdown dump is not the bottleneck** — the GitHub mobile app already shows open PRs. The actual bottleneck on a phone is **triggering and unblocking agentic work without typing long CLI commands in a tiny terminal**:
- Seeing that PR #4827 is green and clean, and **merging it** with one tap.
- Seeing that PR #4826 and PR #4814 conflict on `package.json`, and **merging #4826 then rebasing/regenerating #4814** (or dispatching `td gh resolve-conflicts`).
- Seeing that a CI run or Jules session is stuck or failed, and **running the automated feedback/repair loop** (`td agent run-feedback-check`, `td agent fix-ci`, `td agent sync`).
- Keeping `.agent-context.json` warm in `tech-dancer` after webhook auto-pulls so interactive agents (`agy`, `pi`, `td`) start with fresh repo context.

### Proposed Redesign: From Static Checkboxes to Agentic Actions

#### 1. Automate Context Warming on Webhook Auto-Pull
When the `buttons` webhook auto-pulls `arii/tech-dancer` on `main`, it can optionally trigger a non-blocking `td context-warm` (or `pnpm install --frozen-lockfile` when `pnpm-lock.yaml` changes) inside `terminal` so the checkout is always warm for agents, not just `git fetch`ed.

#### 2. Replace the Static `Briefing` + `Plans` Section with `td` Agentic Workflows
Instead of three briefing/plan buttons that only write markdown files, expose the actual deterministic `td` agent operations that exist in `tech-dancer`:

| Proposed Button | Underlying Command (Allowlisted) | Why It Helps Agentic Work |
|---|---|---|
| **Triage & Briefing** (auto-runs on load or 1 tap) | `briefing_summary.py --briefing` + `td --json agent list-sessions` | Shows open PRs, CI/conflict status, **and active Jules sessions** in one view without needing a separate "Plans" file. |
| **Sync & Nudge Agents (Jules / CI feedback)** | `td agent sync && td agent run-feedback-check` | One-tap phone action to sync active Jules sessions and automatically post CI failure / review feedback to unblock stalled agent PRs. |
| **Warm Repo Context** | `td context-warm` | Pre-warms `.agent-context.json` in `tech-dancer` so `td`, `agy`, and `pi` have an up-to-date index after recent merges. |
| **Preflight Check (`tech-dancer`)** | `td preflight` (or `td doctor`) | Verifies full-stack runtime, credentials, typecheck, and baseline health in `tech-dancer`. |

#### 3. Per-PR Action Buttons (Instead of Inert Checkboxes)
Instead of rendering markdown checkboxes (`- [ ] #4827 ...`) that only edit a text file, each open PR returned by `/buttons/api/briefing` can render with allowlisted, parameterized action buttons (validated strictly by integer `pr_number`, just like `plans/toggle` validates `line_no`):
- **Merge PR** (`gh pr merge <N> --squash --delete-branch`) — enabled only when `CI == passing` and `merge == clean`.
- **Update / Rebase Branch** (`gh pr update-branch <N> --rebase` or `td gh resolve-conflicts --pr <N>`) — when `mergeStateStatus` is `BEHIND` or `DIRTY`.
- **Fix CI with Agent** (`td agent fix-ci --pr <N>` or `td agent trigger-feedback`) — when `CI == failing`.

---

## 4. Prioritized Next Steps

1. **Immediate `tech-dancer` PR Queue (Ready Now):**
   - [ ] Merge **PR #4827** (`chore/update-config-for-td` — CI passing, merge clean).
   - [ ] Merge **PR #4826** (`dependabot/npm_and_yarn` — CI passing, merge clean), which will let `release-please` automatically rebase and update **PR #4814** (`0.10.0` release) and clear the `package.json` conflict.
   - [ ] Inspect and close out the failing `main` push workflow run (`40739159188`) if not already superseded by PR #4827.
2. **Trim Redundant Buttons on `/buttons`:**
   - [ ] Remove **Pull models** and **Repo briefing (agent, slow)** from `ACTIONS` in `buttons/server.py` (move `pull-models` to `package.json` as `npm run models:pull`).
   - [ ] Collapse the redundant **Create plan from briefing** + static **Plans** file-checkbox UI in favor of rendering the live briefing directly.
   - [ ] Collapse the **Server** group into a compact row or `<details>` drawer (`Status`, `Logs`, `Recreate terminal`, `Rebuild buttons`) so the top of the phone screen is dedicated to repo & agent work, not server maintenance.
3. **Add Real Agentic Controls to `/buttons`:**
   - [ ] Add **Sync & Nudge Agents** (`td agent sync` + `td agent run-feedback-check`) and **Warm Context** (`td context-warm`).
   - [ ] Add strict integer-validated per-PR actions (**Merge**, **Update branch**, **Trigger CI feedback / Fix CI**) directly on open PR cards so you can actually clear the PR queue from your phone.

# Buttons page: functional review & areas for improvement

- **Page:** `https://dev-onasafari.ddns.net:8443/buttons` ("S5 Control Panel")
- **Date:** 2026-11-02
- **Method:** live walkthrough of every tab via browser-mcp (clicks, screenshots, console errors) plus source checks in `buttons/server.py`. No code changed.
- **Prior art:** `docs/buttons-ux-evaluation-and-next-steps.md` and `docs/ux-evaluation-and-redesign-guide.md` cover the 2026-10 restyle (tabs, navy palette — since shipped). This doc is a fresh *functional* review: what is broken, redundant, or noisy *now*.

## Verified bugs (fix first)

1. **Briefing "Needs attention" crashes the page script.** `renderAttention()` calls `shorten()`, but `shorten` is defined inside the *other* function `renderBriefing()` — out of scope. Console shows `ReferenceError: shorten is not defined` on every Briefing load with failing runs ([server.py](/srv/remote-server/buttons/server.py:3942) vs [server.py](/srv/remote-server/buttons/server.py:3863)). Fix: hoist `shorten` to top-level scope. The empty blank card on the Actions tab is the attention box failing to render.
2. **Repo switching does not refresh the Project tab.** The repo cards set the hidden `<select id="briefingRepo">` value programmatically, which does **not** fire the `change` listener that calls `refreshProject()` ([server.py](/srv/remote-server/buttons/server.py:3013)). Cards only call `refreshBriefing()`. Verified live: after tapping `arii/boomtick`, "Active repo" changed but Project headers and the Agent Chat repo label still said `tech-dancer` until the tab was re-clicked. Fix: dispatch `change` (or call `refreshProject()` + chat-label update) in the card click handler.
3. **Nothing persists across reload.** There is zero `localStorage`/cookie usage in `server.py`. Verified: reload resets repo to `tech-dancer`, tab to Actions, and collapses the Server section. Persist at minimum: selected repo, active tab, section collapse state.
4. **`tech-dancer` is hardcoded as the default in ~10 places** (JS picker fallbacks + Python `repo="tech-dancer"` defaults, e.g. [server.py](/srv/remote-server/buttons/server.py:2244)). Combined with bug 3, every fresh load lands on tech-dancer even if you only work in other repos. Fix: default to last-used repo (via bug 3), else `remote-server` (the stack you are actually operating).
5. **"Sync agent sessions" dumps ~20 KB of raw JSON into Output**, including full Jules session prompts and a whole git patch. Unreadable; leaks internal command lines and absolute paths. It should render the 2-line summary per session (title · state · repo · updated) that the Actions "Agent Status" card already produces — or just refresh that card.
6. **Jules session lists are not repo-scoped.** On the Project tab with `boomtick` selected, the list showed a `remote-server` session ("active and past jobs list…", PR 150). Either filter by repo or label the section "all repos".
7. **"Audit open issues" dumps raw JSON with server filesystem paths** (`/srv/repos/boomtick/.boomtick/logs/...`) into the page. Same class of bug as 5. Render links/counts, not JSON; never print server paths.
8. **"Auto-pull: not yet" is meaningless and ephemeral.** It resets on every reload (bug 3), and only `remote-server` ever showed "pulled ✓". Either make it real status (last webhook pull per repo, persisted) or delete it from all four cards.
9. **Test push dialog says "sent!" before confirming.** With Notifications off, "Test push" opens a modal whose body already claims "Test notification sent! Check ntfy/webhook." with Cancel/Confirm ([server.py](/srv/remote-server/buttons/server.py:6132)). The message must describe what *will* happen, not claim it happened. Also: with notifications off, these buttons should say where to enable them instead of dead-ending.

## Global / header (above the tabs)

| Area | Feedback |
|---|---|
| Repo cards (4×) | Take up the entire first viewport. Three of the four cards carry identical helper text ("Repo work — briefing, warm context, preflight, PR actions. Tap this card to select it."). Collapse to a one-line segmented control / dropdown + keep one shared caption. |
| Nested GitHub link | Each card embeds a GitHub link inside a tap-to-select card (tap target trap; the only reason the "outside its GitHub link" carve-out exists). Move the GitHub link to a small icon, or make selection explicit via radio/dropdown. |
| Hidden select | The real picker (`#briefingRepo`) is `display:none`; all state flows through an invisible control. Either show it (simplest, accessible) or replace cards + select with one visible control. |
| "Fixed actions only, no free-text commands." | Accurate but permanent boilerplate — demote to a tooltip/`How this works` line, not the page's second sentence on every load. |
| "Updated … / Notifications: off / Test push / Send digest" | Four unrelated things on one row. "Updated" timestamp meaning is unclear (page load? poll?). Notifications-off + two dead-end buttons is noise for the common case — fold into a settings corner. |
| "Active repo: X" + Chat button row | Fine, but it duplicates the selected card directly above it. Once cards collapse (above), this row becomes the single source of truth — good. |
| Terminal / VS Code links | Fine. Consider `target=_blank` + `rel=noopener` so they don't navigate away from the panel. |
| "Stack code updated … tap Deploy buttons" banner | Good idea; but it names a button in a *collapsed* section on another scroll position. Make the banner itself a button that runs the deploy (with confirm). |
| 84 buttons on one page | Symptomatic. Most boxes below repeat Refresh + action + expanders; see per-tab notes. |

## Chat + Output drawers (global, all tabs)

- **Three entries to the same chat:** header "Chat / Ask Agent" button, Pi-agent tab "Open Agent Chat", and the persistent bottom "Agent Chat" bar. Keep one (header) + the bottom bar; delete the Pi-tab duplicate.
- **Output drawer blocks the page:** after a run, `pre#out` overlays content and intercepts clicks (it physically covered the Pi-tab buttons during this review; had to close it first). Drawer must not trap pointer events outside itself, and should auto-minimize to the status pill on tab switch.
- **Output shows internals first:** every run echoes the full `docker compose exec … bash -c …` command line with absolute paths before any result. Collapse the command into a `<details>` line; show the result/summary first.
- **Two "Report bug" buttons** (Actions bottom + Output drawer) with unclear scope (page bug? last run?). Keep one, in the Output drawer, prefilled with last-run context.
- **No "copy output" button.** For a phone-first tool, add Copy + Share before more features.

## ⚡ Actions tab

| Box | Feedback |
|---|---|
| System Health (live box) | Useful. But there is a *second* "System health" button under Server that prints the same data into Output. Merge: the live box IS the health view; drop the button. Disk/Mem bars would beat "136.2 / 456.3 GB (30%)" text. |
| Empty card under System Health | This is the attention box crashing (bug 1). Renders as a blank rounded rectangle — looks broken because it is. |
| Agent Status: Jules + Refresh | Works. But Jules status now appears in **three** places (here, Briefing "Agent sessions", Project "Jules sessions"). Keep one canonical card (here) and link to it from the others. "Refresh" is a tiny ghost button — auto-refresh on tab open instead. |
| Local Repair Worker | "Dispatch repair" with empty input does nothing visible (no validation message — contrast Project tab, which at least prints "Title is required"). Add inline validation. "Repair Status: idle" + collapsed "Repair progress and log" is fine. Which repo does a bare issue number target? Ambiguous — label it (uses Active repo? parses URL repo?). |
| Server · arii/remote-server (collapsed by default after reload) | Collapse state doesn't persist (bug 3). Buttons: Status / Logs are read-only but produce Output dumps — fine. **Deploy / Recreate terminal / Recreate VS Code** are destructive, full-width, one tap + confirm. They already have amber edges — good — but "Deploy buttons (rebuild this page)" subtitle "(drops ~30s)" understates: the page dies mid-rebuild with no reconnect guidance. Add post-click "rebuilding, retry in ~40s" state. |
| Briefing · repo from picker (5 buttons) | Every subtitle repeats "(repo from picker above)" — 5× redundancy; the group title already says it. "Refresh briefing" subtitle says "from td" (internal tool name, meaningless to users). "Sync agent sessions" is the 20 KB JSON dump (bug 5). "Create plan from briefing" writes to `.agents/plans/<date>-briefing-plan.md` — after success, show the file path + what to do with it, not just exit 0. |

## 📋 Briefing tab

User verdict ("not useful") is mostly explained by bugs 1 + 7 and the wall of Dependabot text:

- **"Needs attention" duplicates "Failing runs".** Same runs listed twice with near-identical 4-line text. Merge into one section: one row per failing run (workflow · branch · cause chips) + expand for detail.
- **Failing-run names are paragraphs.** The `npm_and_yarn in /. for @grpc/…, @grpc/… (×40 packages)` run name repeats in full per item despite the 600-char `shorten` cap. Truncate run names to ~120 chars + count ("+38 more packages"); the package list is never the actionable part — the *likely cause* is.
- **"Draft issue from failure" / "Ask Pi to diagnose" on every item** (4+ identical button pairs). These are the highest-risk buttons on the page (one files a GitHub issue, one burns minutes of local LLM) and they sit inline with zero preview/confirm affordance shown. Collapse to one "⋯" menu per row with a confirm step showing what will be filed/asked.
- **No open-PRs state.** With zero open PRs the tab shows no PR section at all — no "No open PRs ✓" empty state, so users can't tell "clean" from "failed to load" (especially with a console ReferenceError present). Add explicit empty states everywhere.
- **"How this works" collapsed** — fine, keep.
- **Agent sessions** shows one line + "(repo unscoped)" — i.e. the scoping bug 6 leaking into copy. Fix the scoping instead of documenting it.

## 🤖 Pi agent tab

- **Suggested next actions:** runs minutes ("Asking Pi (local model, may take minutes)…") with **no cancel button**, no progress, and output goes to the Output drawer behind the tab. Add Cancel (the chat claims "real-time streaming, tools, and cancellation" — suggest should match). Auto-running this on tab open would remove a click; at minimum remember last suggestions per repo instead of re-running.
- **Ask Agent / Chat** = duplicate chat entry (see drawers). Delete.
- **Pi sessions:** "List sessions" works and per-session ✕ works. Issues: prompts truncated mid-word with no expand ("Use ONLY the cont…"); list includes junk test sessions ("what is 2 + 2?") with no timestamps-to-delete or search; **"Clear Pi sessions" has no confirm** and no scope label (this repo? all repos?) — destructive + ambiguous. Add confirm + scope label; add per-row expand.
- Three stacked "How this works" expanders with near-identical generic copy — merge into one tab-level line.

## 📊 Project tab (weakest tab — most "no effect" reports)

| Box | Feedback |
|---|---|
| Enriched PR Review | With no open PRs: dropdown shows only "— Pick open PR —" yet **Inspect PR stays enabled** and clicking just prints "Please select a PR to inspect." Disable the button when empty (same for "Review plan for PR → Generate"). When PRs exist this needs a re-test; currently unverifiable. |
| Workflow Health Analysis | Auto-loads AND has "Refresh workflow health" — pick one mental model (auto-load + subtle "updated Xm ago · refresh" link). Same Dependabot wall-of-text as Briefing. Per-failure "Draft issue / Ask Pi" duplication again. |
| Issues | Works (Refresh, Details/assign expander with labels + assignee). Gaps: assignee control is a bare "Assign" with no control visible until expand; no filter (open/all, label); "unassigned" on everything suggests assignment rarely works — verify end-to-end. |
| Structured Issue Builder | Title is a full-width white unstyled `<input>`, Body a tiny 2-line unstyled `<textarea>` — looks broken vs the dark theme. "Create issue" with empty title prints "Title is required" (good) but only *after* the click — disable until valid. After create, show the issue link (Ledger proves the URL exists). |
| Jules sessions & Agent Handoff | Dispatch form works with validation ("Prompt/task description is required" — good). But: branch field optional-or-required is unstated; after dispatch, does the list refresh? (unverified — don't click without intent). Session list unscoped (bug 6). "Refresh Jules sessions" spins "Loading…" with no timeout/error state observed. |
| Plans | "Audit open issues" → raw JSON dump (bug 7). "Audit workflows" clicked with **no visible effect at all** (no output, no error — the "doesn't work" report, reproduced). "Review plan for PR — + Generate" with empty dropdown: no-op. "Aggregate PRs" listbox is empty with zero PRs, so Generate can only fail ("Select at least one PR"). The whole Plans box needs empty-states + disabled-until-valid + rendered results, or removal. |

## 🧰 Jobs tab

- **Top builders are reasonable** (Triage repo / Fix CI on a PR / Fix issues via repair worker / Nudge agents). Gaps: "Fix CI on a PR" shows "No open PRs" dropdown with an **enabled Start job** — disable it or hide the box when empty. "Nudge agents" two-step (preview → Approve) is the right pattern — reuse it for Draft-issue/Ask-Pi.
- **"Active and past jobs" keeps growing** (user's report, confirmed: ~18 cards, almost all `failed`). Mitigations now exist (Clear past jobs + per-card ✕) but: failed steps show `failed` with **no reason inline** — every card requires expanding elsewhere to learn why. Add one-line failure reason per failed step. Auto-collapse terminal jobs older than the newest active one; cap the list (e.g. 20) with "show more".
- Every Triage job fails at "Preflight check" with done/done/failed — a systemic failure worth one investigation, not 18 identical cards. If preflight-on-tech-dancer is known-broken, say so in the Jobs header until fixed.
- Checkbox issue picker duplicates the Project Issues list (same 4 issues, different interaction). One shared issues component would halve maintenance.

## 📜 Ledger tab

- Best-behaved tab. Kind filter + Refresh/Clear work.
- **5× "unknown entry" rows** — the renderer hits ledger shapes it can't format ([server.py](/srv/remote-server/buttons/server.py:4761)). Either format them or filter them; "unknown entry" with a timestamp teaches nothing.
- "output tail" expanders are good. Timestamps are absolute; add relative ("2h ago") for scanability.
- "Clear ledger" — destructive, check it confirms (didn't click). Should offer scope (clear_runs only vs all).

## Text to cut or rewrite (concrete strings)

- "Repo work — briefing, warm context, preflight, PR actions. Tap this card to select it." ×3 → one shared caption; per-card keep only the repo name + one-line role.
- "This stack — terminal, buttons, oauth2-proxy. The Server buttons below act here." → "This stack (Server actions act here)."
- "(repo from picker above)" ×5 → delete (group title covers it).
- "Open PRs, CI, conflicts from td" → "Open PRs, CI status, merge conflicts".
- "doctor + typecheck + lint + overlap + merge simulation (compact digest)" → "Full health + merge-safety check".
- "Regenerate .agent-context.json if stale" → "Refresh cached repo context".
- "Save briefing to .agents/plans/<date>-briefing-plan.md" → "Save briefing as a plan file".
- "Auto-pull: not yet" → delete (bug 8) or make real.
- "Fixed actions only, no free-text commands." → move to About/tooltip.
- "Interactive assistant with real-time streaming, tools, and cancellation…" → "Ask about this repo or dispatch a task." (half the features named don't exist in this drawer).
- "(repo unscoped)" → fix scoping (bug 6), delete the note.

## Suggested priority

- **P0 (broken):** bugs 1–4 (shorten crash, Project-tab staleness, no persistence, hardcoded default). These explain "doesn't switch correctly" and "briefing not useful" almost entirely.
- **P1 (trust-destroying output):** bugs 5–7 (raw JSON dumps, unscoped Jules lists, server paths) + Plans-box no-ops + failed-job reasons. Every raw dump teaches users the page "doesn't work".
- **P2 (friction):** repo-card collapse, Output-drawer click trap, duplicate chat entries, disabled-until-valid buttons, empty states, confirm on destructive/costly actions (Clear Pi sessions, Draft issue, Ask Pi, Dispatch repair/Jules).
- **P3 (polish):** text cuts above, relative timestamps, Disk/Mem bars, Terminal/VS Code `target=_blank`, copy-output button.

## Product goal & future directions (what this page is supposed to become)

### The stated goal

Per `docs/buttons.md` + `docs/autonomous-triage-runbook.md`, the page is meant to be a **phone-first operations deck**: run stack and repo ops without typing on a phone keyboard, **status-first** (health → needs-attention → act), **safe** (fixed allowlisted commands, confirm gates, audit ledger), and increasingly an **agent-supervision surface** — a human approving agent work (Jules, Pi, repair worker) from their phone, on the road toward the **autonomous multi-repo triage daemon** (20-minute cycle: discover PRs → nudge Jules → validate & merge → cleanup → dispatch backlog). The Nudge-agents job's "preview then Approve" pattern is the template for all autonomy here: agents propose, human taps once.

None of the critique above disputes that goal. What follows is where the current page falls short of it — capabilities that are documented, half-built, or missing entirely.

### 1. Status-first is promised but broken

The docs describe Actions as "status-first": System Health card, Needs-attention section, stale-rebuild banner — then buttons. Today the attention section crashes (bug 1), health exists twice, and no tab tells you anything before you open it. Future: every tab carries a **badge count** (Briefing `2 failing`, Jobs `1 failed`, Ledger `·` for new), and Actions opens on a single merged **attention inbox**, not three overlapping sections.

### 2. All pull, no push — the server can't tap you on the shoulder

Everything requires opening the page and tapping. "Notifications: off" is permanent furniture; Test push dead-ends; no evidence the digest ever arrives. For a 24/7 server + phone UI, push is the core value prop. Future: wire **three events** to ntfy first (CI run failed, job failed, Jules session completed), then add per-event toggles and a morning digest that provably sends (with a "last sent" timestamp in the UI, not just a button).

### 3. Scattered surfaces instead of one supervision inbox

Jules status lives in three places, repair progress in a fourth, approvals only inside Jobs. A supervisor needs one queue: **"Needs decision"** — approve nudge, merge PR, assign issue, retry failed job — each row one tap with a preview, each completing inline. The approve-gated Nudge pattern should be the universal interaction, reused for Draft-issue / Ask-Pi / Dispatch-repair (which today are zero-preview fire-and-forget).

### 4. Plans don't flow into action

The Plans box is scaffolding without a loop: audits dump JSON to nowhere, Generate buttons no-op, and nothing connects a plan to a dispatch. The daemon vision is spec-driven (boomtick#1056), but no plan file is reachable from the UI. Future: **audit → rendered plan → one-tap "Dispatch to Jules / repair worker"**, with the plan file linked and the resulting session/issue linked back. Until that loop exists, consider hiding Plans behind a "labs" flag rather than presenting dead buttons.

### 5. The highest-value flow — PR lifecycle from phone — is unproven

`docs/buttons.md` describes structured PR cards with Fix CI / Update branch / Merge. This review saw **zero open PRs and none of those buttons**, so the golden path (CI fails → notified → read diagnosis → tap Fix CI → tap Merge) has never been demonstrated end-to-end. Future: make it the golden path explicitly — seed a test PR, walk the whole flow from the phone, keep it as the regression script. Merge-from-phone done safely is the page's reason to exist; everything else is scaffolding for it.

### 6. The triage daemon exists on paper only

The runbook specifies a 20-minute autonomous cycle, but the UI shows no daemon status — not even "not running." Jobs are manual one-shots that mostly fail at preflight. Future: a **daemon card** (last cycle, next cycle, actions taken this week, pause/resume, per-phase toggles) with the Jobs tab as its manual-override panel. Even before the daemon exists, showing its absence honestly beats implying it with job templates named "Triage repo."

### 7. Single-repo UI vs multi-repo reality

The picker forces one repo at a time, yet triage is inherently multi-repo (3 repos cycled) and Jules data is already global (just mislabeled). Flipping repos to check each one's attention list is the core "unnecessary clicks" complaint. Future: an **"All repos" attention view** as the default Briefing/Actions scope, with per-repo sections and counts; keep single-repo scope for actions that need it (preflight, warm context), where the picker makes sense.

### 8. The ledger is write-only memory

The audit log works, but nothing reads it back. Future: derive product from it — **"since yesterday" briefing** generated from ledger deltas, **failure trends** ("preflight failed 12× this week, all on tech-dancer"), job→run→ledger cross-links so a failed step leads somewhere. The digest (direction 2) should be composed from ledger + briefing, not a separate mystery job.

### 9. Trust blockers to clear before autonomy grows

- **Preflight is cross-repo broken by admission**: `td preflight` hardcodes BoomTick's checks and fails spuriously elsewhere (`docs/buttons.md`, `docs/boomtick-issues.md`). Every Triage job failing at step 3 teaches users to ignore jobs. Fix the checks or scope the job honestly.
- **One-run-at-a-time global lock with no queue**: can't stack briefing + preflight; second tap errors. Add a visible queue (or at least "run X is active — your tap is queued/cancelled" instead of an error toast).
- **Attribution**: `docs/agent-guide.md` sets up the `onasafari` agent identity — issues/PRs the page files should visibly carry it, and the UI should say which identity an action will use before it fires.
- **Docs drift**: `docs/buttons.md` says five tabs; the page has six (Project undocumented). Every new box needs one docs paragraph or it rots into mystery buttons like Plans.

### Suggested build order

1. Fix P0/P1 in this doc — no autonomy on a crashing UI.
2. Attention inbox + tab badges (composition of data that already exists; no new backends).
3. Push for 3 events + provable digest (ntfy is already referenced; wire it).
4. Plan→dispatch loop (render audits, one-tap dispatch, backlinks).
5. Daemon status card in UI (honest "not running" first, controls later).
6. PR golden-path E2E with a real PR; keep as the regression script.
7. Multi-repo "All" attention view.

## Re-test checklist (after fixes)

1. Select boomtick → Project headers + chat label update *without* re-clicking the tab; reload → repo + tab preserved.
2. Briefing tab with failing runs → zero console errors, one row per run, no duplicated sections.
3. Sync agent sessions → summary list, no JSON, no server paths.
4. Project tab with zero PRs → all PR-dependent buttons disabled with reason; Plans box either works or is hidden.
5. Dispatch Jules / Draft issue / Ask Pi → each shows preview + confirm + post-success link.
6. Jobs tab → failed steps carry one-line reasons; list capped/collapsible.

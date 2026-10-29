# Buttons — phone action page

A tabbed phone UI at `https://<domain>:<port>/buttons` (same URL as
the terminal, same GitHub OAuth gate) with buttons for the stack
operations that are painful to type on a phone keyboard. No free-text
command input anywhere: every button runs one fixed command list.
The Ask Pi box takes free-text *instructions*, but Pi only proposes —
every execution still goes through a confirm dialog and a validated
command path (see Ask Pi below).

## Layout

Five tabs share one sticky header: **Actions** (stack buttons),
**Briefing** (briefing + PR cards), **Pi agent** (suggestions + Ask Pi
+ Pi sessions), **Jobs** (multi-step tasks, see below), **Ledger**
(audit log). The header holds the repo picker
(the repo chip — every repo-scoped button and the briefing follow
it) plus a server status dot, green while the status endpoint
answers, red when it does not. Run output lives in a slide-up
drawer at the bottom of the screen, reachable from every tab: it
opens when a run starts, holds the status line above the output
pane, and carries Clear output and Close in its header.

The Actions tab is status-first: a **Needs attention** section on
top shows failing runs and attention items from the latest
briefing, plus the stale-rebuild banner when the served page
predates the checkout. Action buttons are grouped in collapsible
sections (Server stays closed; the repo-action group stays open).

The theme is dark navy (`--bg-app: #0b1120`, `--bg-card: #131d31`,
`--primary: #3b82f6`, `--danger: #ef4444`, `--warning: #f59e0b`,
`--success: #10b981` on `:root`). Action buttons are left-aligned
cards; Deploy buttons and Recreate terminal keep the amber left
border — they destroy or recreate containers, unlike the read-only
actions. Briefing PR cards are structured: `#123` badge + CI status
dot + linked title on the header row, chips for branch / CI /
merge state / updated / flags (CONFLICTED, draft, stale), failing
CI details as a shortened line, and an action strip with a
primary/ghost hierarchy (blue filled Fix CI and Merge, ghost
outline Update branch).

## Buttons

The page itself says what each button does: buttons are grouped
(Server / Briefing) and each carries a one-line subtitle under its
label, so you do not have to remember the docs to use the page.
Button text is left-aligned for readability; Deploy buttons and
Recreate terminal carry an amber left border — they destroy or
recreate containers, unlike the read-only actions.

| Button | Runs |
|---|---|
| Status | `git status` + `git log -3` in the checkout, `docker compose ps`, terminal image tags |
| Deploy buttons (rebuild this page) | Runs in the TERMINAL container, not buttons itself: `git pull --ff-only`, `docker compose up -d --build buttons`, then a verify loop curling the new container's status endpoint (60s, fails visibly with `compose ps` output on timeout) — rebuilds the buttons container from the checked-out code |
| Recreate terminal | `docker compose up -d --force-recreate terminal` — use after editing `.env` (editing `.env` does not change a running container) |
| Logs | `docker compose logs --tail=100 terminal ollama`, then `buttons/pi_logs.py` in the terminal container — readable Pi session audit, see below |
| Refresh briefing | `buttons/briefing_summary.py --briefing` inside the terminal container: parses td JSON plus `gh` enrichment into a readable briefing — see below. Repo follows the picker above, and the cards refresh when the run completes. This is the page's only briefing action: the old standalone Refresh button, which re-fetched without a run record, is gone, so a manual refresh now logs a run |
| Sync & nudge agents | `td agent list-sessions` (falling back to `td jules list-sessions`, same fallback the briefing's Jules section uses), then `td agent run-feedback-check` — list active Jules sessions, post CI/review feedback to stalled ones (global across repos, not picker-scoped). A missing JULES_API_KEY is not a failure: both steps print a notice and exit 0, so the run stays green instead of turning the banner red for config you haven't done |
| Warm repo context | `td context-warm` — regenerate the picked repo's `.agent-context.json` if stale |
| Preflight check | `td --json preflight` piped through `buttons/preflight_digest.py` — one OK/FAIL line per check (doctor, typecheck, lint, overlap, merge simulation) plus a summary, instead of 30KB of raw JSON. Raw output is kept only when the JSON won't parse; `pipefail` preserves td's own exit code |

Deploy buttons, Recreate, and Sync & nudge agents ask for
confirmation first, as do the per-PR action buttons on the
Briefing cards and the destructive Clear buttons (ledger, Pi
sessions). The confirm is a custom in-page modal, not
`window.confirm`: title + body text, red Confirm button for
destructive actions, backdrop tap or Cancel to back out. Status,
Logs, Refresh briefing, Warm repo context, and Preflight check do
not ask.
One run at a time: starting a
second run while one is active returns an error until the first
finishes. Output streams into the slide-up drawer (polled every
2s); the drawer opens when a run starts and stays reachable from
every tab. Reloading mid-run re-attaches to it. Recreating the
terminal kills any running
Pi session in it — that is the point of Recreate, but do not press
it mid-conversation.

There is deliberately no Deploy-latest-for-terminal button anymore.
It was cut once the stack reached a stable state: its pull step
(pull the terminal image) is not something a stable setup does
from the phone, its git pull is already covered by Deploy buttons,
and its restart is covered by Recreate terminal. Recreate only
restarts, with the current image and `.env` — no pull, no code
update. A plain `up` leaves an unchanged container alone, so
`.env` edits by themselves never take effect; that is the case
Recreate exists for. When a terminal image update is genuinely
needed, it is a host-shell job, not a phone button:
`docker compose pull terminal && docker compose up -d terminal`.

Deploy buttons is special: it recreates the very container serving
the page, so the run's connection drops mid-run. The page handles
that case, not as a failure: when a deploy-buttons run's polls
start failing (404 for the dead run id, or unreachable), it polls
the status endpoint instead and reloads into the new container
as soon as it answers; after 120s with no answer it says so with
the terminal command that shows the real state. For any OTHER
action, 5 consecutive failed polls still mean "connection lost —
refresh in ~30s" with the buttons re-enabled.

Why the steps run in the terminal: the original Deploy ran
`docker compose up --build buttons` from the buttons container
itself, and that strands the site. Compose stops the buttons
container to recreate it, which kills the compose CLI
mid-sequence — the new container is never created, and a stopped
container is never auto-restarted, so the page stays down until
someone runs `up -d` on the host. That was the reported "Deploy
crashes the site and it never comes back" bug. The terminal
stays up across a buttons recreate, so running the same sequence
there completes. If the build itself fails, the old container
keeps serving and the run fails visibly in it; fix from the
terminal and press Deploy buttons again.

## Failed runs show their cause

Run steps stream through a PTY, so stdout and stderr both reach
the output pane as they arrive. The remaining failure trap was
a step that exits non-zero without printing anything: the pane
showed the command and then a blank gap, which reads like a
rendering bug. `run_action` now detects that case (no step
output at all, excluding the handler's own error line) and
appends an explicit note — the step printed nothing before
failing, re-run the command in the terminal, check
JULES_API_KEY and the td agent/jules group names first — ahead
of the `step failed` line. A step that printed anything keeps
the old behavior (its own output is the cause).

## Progress, Clear output, pass/fail colour

- **Progress line:** while a run is active the status line shows
  `running: <action> — step N/M — <elapsed> elapsed, last output
  <age> ago`, plus the current step's command (truncated). The
  elapsed / last-output ages tick every second between polls, so a
  long silent step (model pull, Pi briefing) reads as "3m12s
  elapsed, last output 45s ago" instead of looking dead. A step that
  is still producing lines keeps "last output" near zero; one that
  has gone quiet for minutes is genuinely stuck or waiting, not
  just slow.
- **Clear output:** the button in the drawer header clears the
  stored output server-side (`POST /buttons/api/clear`), so the next
  poll does not refill the pane with old lines. A running run keeps
  its progress line and only new lines appear after the clear; a
  finished run keeps its done/failed status with an empty pane.
  Clear cancels nothing and runs no command. The button sits in the
  drawer header (not the Actions tab) on purpose, so it is never
  disabled while a run is in progress.
- **Failure banner:** a failed run's status line carries the single
  most error-like output line (read from the output pane, so ANSI
  is already stripped) instead of the raw tail — the pane sits
  below the fold on a phone, so the cause was never visible. The
  echoed `$ ...` command line and the server's own `step failed …
  aborting` trailer are filtered first (they say nothing about the
  cause); then a JSON `"message"`/`"error"` field, a traceback, or
  an Error/failed line wins, falling back to the raw last line.
  One line, truncated at 220 chars: the banner is a pointer, not
  a log.
- **Green / red:** the status line and the output pane's border go
  green on exit 0, red on any other exit code, amber while running.
  Border colour comes **only from the exit code**, never from
  parsing the output. The output text itself *does* now show the
  command's own terminal colours: steps run under a PTY
  (`_run_step` in `server.py`, `TERM=xterm-256color`), so git,
  compose, and td emit the ANSI colours they would show in the
  terminal, and the page converts SGR codes to styled spans
  (`ansiToHtml` — basic + bright + 256-colour + truecolour, `\r`
  progress redraws collapsed to their final line, non-SGR escapes
  stripped, output HTML-escaped first). That PTY detail has one
  guard attached: a TTY stdout makes some tools page their output
  (git log into less would hang a run), so the run environment
  forces `GIT_PAGER=cat` and `PAGER=cat`, and step stdin is
  `/dev/null` so a prompt fails fast instead of hanging.

- **Safe fetch:** every page `fetch` goes through `fetchJson()`,
  which never throws on an empty or non-JSON body — it returns
  `{ok, status, data}` with `data.error` set from the body text
  (truncated) or the HTTP status, and `"network error: ..."` when
  the fetch itself fails. Error strings render via `textContent`
  only, never `innerHTML`. (A repo switch once crashed the
  briefing with a raw `SyntaxError: Failed to execute 'json' on
  'Response'`; that path now shows "briefing unavailable: ..."
  instead.)
  Every run ends with an
  explicit trailer line in the output: `finished: exit 0`, or
  `step failed with exit code N; aborting` on the failing step
  (later steps never run).

## Buttons are server-rendered, not JS-generated

The action buttons are rendered into the HTML by the server
(`BUTTONS_HTML` in `buttons/server.py`), and JavaScript only
attaches handlers to them. This is deliberate, after a
2026-10-28 incident where the page rendered with no action
buttons at all: the buttons had been JS-generated, so any
failure in the page script left the static parts (Clear output,
Briefing heading) visible but zero buttons and no clue why. Now
the buttons exist in the raw HTML whether or not any script
runs. The page script is also split in two: the core block
(handlers, polling, progress, Clear) and the Briefing block, so
a Briefing failure cannot take the core buttons down. A
diagnostic
line at the top of the page ("JavaScript is not running…")
is hidden by the core script on load — if it stays visible,
the core script never ran: hard-refresh and check the browser
console. Without JavaScript the buttons are visible but cannot
start a run; there is no no-JS form fallback yet.

A second lesson from the same incident, fixed by a separate commit:
the HTML template in `server.py` is a **raw string** (`r"""..."""`).
It was not, and Python ate the JS `\n` escapes inside it — turning
them into literal newlines inside JS string literals, which is a
browser SyntaxError, which killed the whole script. That was the
actual root cause of the empty-buttons page; the server-rendered
buttons are what made the page survive it anyway. If the template
ever stops being raw, the jsdiag line will tell you first.

## Live Briefing view

Below the output pane, a Briefing section renders the same
briefing the fast button prints — but as cards, not a markdown
dump. It runs on page load (with a spinner while the td/gh
calls complete) and re-fetches on Refresh; it does not
auto-refresh, so a card you are reading does not shift under
you. The data comes from `GET /buttons/api/briefing`, which
runs `buttons/briefing_summary.py --briefing-json` in the
terminal container and passes its JSON through. The JSON and
the text briefing come from one `analyze()` function, so the
cards and the text output cannot disagree about a PR.

The section is repo-general, not tech-dancer-only — and the
repo set is discovered, not hardcoded. The server lists
`~/repos` on every request and covers every git checkout there
plus the stack checkout itself (remote-server lives outside
the repos dir, so it stays special); the repo strip cards and
the picker options render per request from that discovery, so
a repo added to `~/repos` — boomtick, or any future one —
appears on the next page load with no code change and no
container restart. The picker, above the action buttons so it precedes every control it governs, sets the repo for the cards,
their action buttons, and the repo-scoped buttons (briefing,
warm context, preflight) — the picker bar is sticky, so the
current repo stays visible while scrolling; every endpoint validates the repo
against the discovered set, and the summarizer takes `--repo`
(the picker's name) plus `--gh` (the checkout's git-remote
owner/repo — it carries no repo table, so a new repo needs no
summarizer change). The td commands run with cwd = that repo's
checkout in the terminal container, so td picks up that repo's
`project_config.json` — remote-server has a minimal one
(`github_repo` + `packageName`, mirroring tech-dancer's
shape) and ignores `.agent-context.json` there, so warming it
cannot dirty the checkout. A repo without a td project config
degrades per source like any other td failure; it does not
silently brief the wrong repo. Discovery is defensive about
names: a directory name or remote that fails validation is
skipped with a log line, never mapped to another repo's data,
because both values are interpolated into td/gh command
lines. Webhook pulls and warming follow the same discovered
set: any repo's main push auto-pulls, and warming runs for
any repo (pnpm install only when the lockfiles changed AND
the checkout has a package.json; context-warm always). Sync &
nudge stays global: Jules sessions span repos, so it lists
and nudges across all of them regardless of the picker.

Each open PR renders as a card: number + title (linked to
GitHub), branch, CI state with failing checks, merge state,
conflict/stale/draft flags, review verdicts, and up to two
review excerpts. Below the cards come failing runs (with failed
jobs and steps — long job/step strings are truncated so a
Dependabot package dump cannot blow out the card height) and
active Jules sessions. The section degrades
per source like the text briefing: a failed source shows as a
note, not a blank section.

Each card also carries action buttons — the point of the view
is clearing the queue from the phone, not just reading it:

| Card button | Runs | When it renders |
|---|---|---|
| Merge (squash) | `gh pr merge N --squash --delete-branch` | CI passing, merge clean, not a draft |
| Update branch | GitHub update-branch API for the PR | Merge state behind, not a draft |
| Fix CI with Jules | `td agent fix-ci --pr N --execute` (dispatches a Jules session — not instant) | CI failing, not a draft |

Every button asks for a browser confirm first, and the server
re-checks fresh state at tap time before running anything — a
card rendered minutes ago may be stale, so the server gates
Merge on CI passing + merge clean, Update on behind only, and
Fix CI on CI failing, and refuses with a reason otherwise. A
DIRTY (conflicted) branch gets no Update button: td's
resolve-conflicts resolves in a separate worktree without
pushing, so it cannot update the PR branch from here — resolve
conflicts in the terminal. The PR number is validated as an
integer server-side before it is ever interpolated into a
command; there is no free-text input on this page.

The old Plans section (plan files in
`tech-dancer/.agents/plans/` with tickable checkboxes) is gone:
ticking a checkbox only edited an untracked file nothing read,
and the briefing cards plus their action buttons replace both
the plan file and its checkboxes. `briefing_summary.py
--plan-dir` still exists as a CLI for writing a plan file from
the terminal, but no button calls it.

## Suggested next actions (Pi)

The Briefing section has a Suggest next actions button. It
asks Pi to read the briefing context for the picked repo and
suggest td commands, then runs only what you approve:

1. The server fetches the briefing JSON for the picked repo
   (same transport as the Briefing view).
2. It compresses that JSON to a compact context — attention
   items, one line per PR (number, title, CI, merge, conflict,
   review), failing runs, Jules count, capped at 6000 chars.
   Pi gets limited, relevant context plus a short task
   instruction — not the full briefing, and not the contents of
   `APPEND_SYSTEM.md`.
3. Pi runs in print mode (`pi -p`) with read-only tools
   (`read,grep,find,ls`): it can read the checkout for more
   context but cannot run anything. Its prompt travels
   base64-encoded, so briefing titles carrying quotes or `$`
   cannot break out of the pi argument. BoomTick MCP tools are
   excluded by instruction (they are scoped to tech-dancer,
   so they would mislead on any other repo).
4. Pi must output only `SUGGEST: <td command> :: <reason>`
   lines. The server parses them through a fixed td-command
   allowlist (exact token shapes: `fix-ci --execute`,
   `read-pr-reviews`, `read-pr-comments`, `resolve-conflicts`,
   `context-warm` without `--force`, `preflight`,
   `list-sessions` in both spellings, `run-feedback-check`).
   Anything else — extra flags, pipes, other verbs — is
   dropped and shown as text, never run.
5. Each surviving suggestion renders with its reason and a
   Run button. Tapping Run asks for confirmation, then
   `/api/run-td` re-validates the command server-side and
   runs it as a normal run (streamed output, ledger entry
   with action `run-td`). There is no batch run and no
   auto-run in v1: one tap per action is the approval gate,
   matching Pi's own read-only boundary.

Failure modes, all visible: a briefing failure or Pi failure
returns 500 with the cause; Pi suggesting nothing runnable
shows Pi's raw output instead of an empty section; a
suggestion that names a repo that vanished mid-flight fails
visibly rather than running against another repo. Latency is
real — briefing fetch plus a local-model Pi run can take
minutes; the section shows a spinner line while it thinks.
Suggestions themselves are not ledger entries (reads, like
briefing reads); executions are.

## Ask Pi

Below Suggest next actions, the Pi tab has a free-text box for
open-ended instructions about the picked repo — "draft an issue
for the flaky briefing refresh", "where does the webhook worker
live?", anything you would otherwise type into a terminal. This
is the one place the page accepts free text, and it never becomes
a command directly:

1. `POST /buttons/api/ask` takes `{repo, instruction}` (2000 chars
   max, repo must be known). The server wraps your instruction in
   a prompt that tells Pi to investigate read-only and, if you
   asked for a write, to propose the exact change and stop —
   never execute it. The prompt travels base64-encoded like the
   suggest flow.
2. Pi runs in print mode with read-only tools
   (`read,grep,find,ls`). The reply renders inline. If Pi emitted
   a `PROPOSE` block in the strict form (`PROPOSE issue` +
   `TITLE:`/`BODY:`/`END PROPOSE`, or `PROPOSE td` +
   `COMMAND:`/`END PROPOSE`), the page shows it as a proposal
   card with the title/body or command verbatim.
3. Nothing runs until you tap the proposal's button and confirm
   in the dialog. A td proposal goes through `/buttons/api/run-td`
   (re-validated against the same allowlist as suggestions). An
   issue proposal goes through `/buttons/api/create-issue`, which
   validates the repo, caps title/body length, and shlex-quotes
   both into `gh issue create --repo <discovered gh path>` —
   free text never reaches a shell unquoted. Issue creations are
   ledger entries (`pr-action` / `create-issue`, with the issue
   URL as the note).

Anything not in the exact PROPOSE form is shown as text, never
run — a dithering model degrades to a readable reply, not a
broken action. Like suggest: slow by design on the local model,
minutes not seconds.

## Jobs (multi-step tasks with approval gates)

The Jobs tab turns repeated multi-step work into named jobs with
a visible plan. A job = goal + plan checklist + audit. Jobs only
run the same allowlisted actions the buttons already offer — they
add orchestration, not capability. v1 is local-only and
deterministic; Pi judgment stays in the Suggestions / Ask Pi flows.

The approval gate guards writes, not reads: read-only steps run on
their own as soon as the previous step finishes — tapping "Start
job" is the approval for the plan itself. Only steps that act
outside the box (dispatching a Jules session) wait for your
**Approve**/**Decline** tap. Steps run in order — later steps unlock
only when earlier ones are done. A second concurrent run still
409s; the step stays pending and you retry.

Templates (fixed, no free text):

| Template | Plan | Taps needed |
|---|---|---|
| Triage | Refresh briefing → Warm repo context → Preflight check | None — runs on its own |
| Pre-merge check | Preflight check → Refresh briefing | None — runs on its own |
| Fix CI | Dispatch Jules fix-ci on PR `#{pr}` | One Approve (dispatches an agent) |

Every terminal job (done / failed / declined) writes an audit:
a JSON file on the buttons volume (`job-audits/audit-<id>.json`
with goal, plan, every approval with timestamp, run ids,
outcome) plus a `job` ledger entry. The Jobs tab lists active
and past jobs newest-first with per-step state badges. Restarting
the buttons container fails any job left mid-step, visibly.

Open design questions for v2: Pi-judgment steps inside jobs,
Gemini fallback for the judgment parts, whether phone approval
should unlock currently-unavailable writes. v1 deliberately
changes none of the approval boundaries.


## Pi session audit

The Logs button now shows more than container logs. Container
logs show the terminal process, not what the agent did — so
after `docker compose logs --tail=100 terminal ollama`, Logs
also runs `buttons/pi_logs.py` in the terminal container, which
reads Pi's own transcripts from the pi-data volume
(`/root/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl`)
and renders them readably: the newest session files, then the
latest session's header (id, cwd, start) and its prompts,
replies, tool calls by name, and truncated tool results. The
full JSONL stays on disk as the archive; this is the readable
audit view. The script never fails the Logs run: no sessions,
an unreadable file, or an unknown entry shape all degrade to a
plain note.

## Ledger (durable audit log)

Run records used to live only in server memory: a container
restart wiped them, and webhook pulls and PR-action taps were
never stored at all. The ledger fixes that. `server.py` appends
one JSON line per action to `/var/lib/buttons/ledger.jsonl`,
which is a named volume (`buttons-data`) — never a checkout,
so pulls and rebuilds cannot dirty or erase it:

- `run`: `{action, repo, exit, output}` — output is the last
  2000 chars, appended when the run completes.
- `webhook`: `{repo, ok, detail, advanced}` — appended when
  the pull record lands.
- `pr-action`: `{repo, pr, action, ok, output|error}` — success
  and refusal both logged; a refused tap is audit-worthy too.
- `system`: `{event}` — currently just `buttons started`, so a
  rebuild is visible as a fresh start line.

What is NOT logged, deliberately: briefing reads (reads are
not actions — logging them would bury the actions), and Deploy
buttons completion (the container restarts mid-run, so the old
process never gets to log it; the new container's `buttons
started` line is the deploy evidence instead).

The page's Ledger section — behind a closed drawer, one tap to
open, so the audit log does not dominate the page — reads it via
`GET /buttons/api/ledger?kind=&limit=` (kind allowlisted:
`all`, `run`, `webhook`, `pr-action`, `system`; limit clamped
1–200, default 50), newest first, with a kind picker and
Refresh. It refreshes on load, on kind change, after any run
completes, and after a successful PR action. Entry data renders
via textContent, never innerHTML, so a log line cannot inject
markup. A ledger append never fails the action it records: any
failure prints to the container log and the action proceeds.
Growth is bounded: past 2,000,000 bytes the file is rewritten
keeping the last 2000 lines before the next append.

Clearing: the Ledger section's Clear ledger button (confirm
dialog) posts `POST /buttons/api/ledger/clear`, which truncates
the file and then appends one tombstone entry
(`{kind: "system", event: "ledger cleared"}`) — so the clear
itself stays on the record, and a cleared ledger never looks
like an untouched empty one. The output pane's Clear output
button is separate and older: it clears only the displayed run
output, not the ledger.

## Pi sessions (browser)

The Logs button renders only the latest session. The Pi
sessions block under Ledger browses all of them: List sessions
fetches `GET /buttons/api/pi-sessions`, which runs
`pi_logs.py --list-json` in the terminal container (no TD
prefix — it reads `/root/.pi` directly) and returns
`{sessions: [{file, mtime, cwd, title}]}` newest first, capped at 20.
`cwd` comes from the session header line and `title` is the first
user prompt (whitespace-collapsed, capped at 80 chars, from the
head of the file only — a title must not cost a full read of a
long transcript); either is empty when absent. The page's list
buttons show date, repo (last segment of cwd), and title, with
the filename as hover text, falling back to the filename when
there is no title.
Tapping a session fetches
`GET /buttons/api/pi-session?session=<file>`, which runs
`pi_logs.py --session <file>` and returns the readable render.
The query is validated server-side against
`^[A-Za-z0-9._-]+$` before it is interpolated into the
`bash -c` string — session filenames are
`<timestamp>_<uuid>.jsonl`, which that pattern covers, and
anything else is rejected with 400 rather than quoted and
hoped. `--session` matches by filename substring: no match
prints a note, multiple matches list the candidates, and an
empty or missing session directory still exits 0. Clear Pi
sessions (confirm dialog) posts
`POST /buttons/api/pi-sessions/clear`, which runs
`pi_logs.py --clear` in the terminal container: it deletes
every session file `_list_files` finds (files only, never
directories, no argument, so nothing from the request steers
which files go) and reports the count; clearing an empty set
exits 0. Job bundles
(briefing + follow-up actions + transcripts under one id) are
the later step: the ledger records the actions, the browser
lists the transcripts, and linking a run to the sessions it
produced is what a bundle adds — the ledger does not duplicate
transcripts.

## The readable briefing (`briefing_summary.py`)

The fast briefing is a summarizer, not a td dump — and that is
deliberate. td defaults to JSON output, and its human mode for
search-prs only prints "Found N PRs", so neither td mode gives a
briefing a person can act on. The summarizer (`buttons/
briefing_summary.py`, stdlib only, run with cwd = the tech-dancer
checkout) parses `td --json gh search-prs --state open`,
`td --json gh status-board`, `td --json gh detect-conflicts --all`,
and `td --json repo workflow-runs` (`--json` is a top-level td
flag — per-subcommand `--json` fails on real td for all but
search-prs), then enriches each open PR with `gh pr view` (CI
from `statusCheckRollup`, review decision, merge state) — td's
PR data carries no CI state, so this step is what makes
"CI: failing" a real claim. `gh` is authed in the terminal
container; if a `gh` call fails, that PR's CI reads "unknown"
instead of failing the whole briefing, and any source that fails
degrades only its own section.

Format, in order: Needs attention, Open PRs (one line each: number,
title, branch, CI, merge state, updated date, draft marker, review
verdicts), Reviews, Work board, Conflicts, Workflow health
(failing runs only, each with its failed jobs and steps where
`gh api` can fetch them), Agent sessions (Jules). Attention
ranking: conflicted, then CI failing, then changes requested,
then stale (updated over 30 days ago). REVIEW_REQUIRED is
deliberately not attention — in a solo repo most open PRs sit
there, so flagging them would make every briefing shout. Drafts
are listed, never attention items.

The v2 rule behind the Reviews and Agent sessions sections: the
local agent reads existing expert work, it does not redo it.
Reviews come from `td --json gh read-pr-reviews --pr N` (top-level
review objects: reviewer, verdict, body), so a changes-requested
attention item quotes what was asked for instead of just naming
the objector. Failing runs gain their failed job and step names
from `gh api .../actions/runs/<id>/jobs` — the likely cause,
not just the run name. Jules sessions come from
`td --json agent list-sessions` (falling back to
`td --json jules list-sessions`, since td registers the group
under either name); only sessions whose source context names
tech-dancer are listed as such, other repos' active sessions are
counted, and sessions with no repo info are listed as unscoped
rather than claimed. If Jules is unavailable (no
`JULES_API_KEY`, no agent group), that section degrades to a
note and the briefing still exits 0.

The script lives in the checkout, which the terminal container
sees at `/srv/remote-server` (the same mount the TD prefix uses
for `.env`), so a git pull updates it with no container rebuild —
but the button definitions in `server.py` that call it still need
`up -d --build buttons` after a `server.py` change.

Briefing notes: the agent briefing's MCP tools in Pi print mode
are unverified on the S5 — its prompt falls back to `td`/`gh`
CLI if MCP tools are absent there. The plan file lands untracked
in the tech-dancer checkout; commit or delete it from the
terminal as usual.

## GitHub webhook auto-pull

Pushes to `main` on `arii/remote-server` or `arii/tech-dancer`
can auto-pull the matching S5 checkout — pull only, never rebuild,
never dependency install. The endpoint is `POST /buttons/api/webhook`
in the buttons service (same port, so caddy's existing `/buttons*`
route covers it), and it answers 202 immediately while the pull
runs in a background thread.

Setup, in order:

1. Add `WEBHOOK_SECRET` to the S5 `.env` (generate with
   `openssl rand -hex 32`). Empty/unset means the endpoint refuses
   everything — fail closed, never accept unsigned webhooks.
2. Recreate oauth2-proxy and buttons so the compose changes take
   effect: `docker compose up -d oauth2-proxy buttons` (buttons
   needs a rebuild for the handler itself:
   `docker compose up -d --build buttons`).
3. Create the GitHub-side webhooks from repo code, not the GitHub
   UI — `scripts/setup-webhooks.sh` reads the payload URL and the
   secret from the same `.env`, lists each repo's hooks, PATCHes
   the one matching the URL or POSTs it if absent, push events
   only, so re-running never duplicates anything:

   ```bash
   npm run webhooks:check   # list hooks, no changes
   npm run webhooks:setup   # create-or-update both repos
   ```

   These route through the terminal container, where `gh` auth is
   verified; the script also runs directly (`bash
   scripts/setup-webhooks.sh`) wherever `gh` is authenticated. The
   token `gh` uses needs Administration: read and write
   (`admin:repo_hook`) on both repos — a 403/404 from `gh api`
   means that permission, and the script says so. Manual fallback
   if `gh` cannot do it: in each repo, Settings -> Webhooks ->
   Add webhook, payload URL
   `https://<domain>:<port>/buttons/api/webhook`, content type
   `application/json`, secret = the same `WEBHOOK_SECRET`,
   trigger "Just the `push` event".

Why the ingress is shaped this way: GitHub cannot pass the GitHub
OAuth gate, so oauth2-proxy skips auth for exactly one route,
`--skip-auth-route=POST=^/buttons/api/webhook$` (POST only, full
path, no wildcards). That flag is also why oauth2-proxy is pinned
to v7.15.2: CVE-2026-41059 lets 7.5.0–7.15.1 with `--reverse-proxy`
+ skip-auth rules be bypassed via a spoofed `X-Forwarded-Uri`
header. Even if the skip rule were bypassed, the handler still
verifies the HMAC-SHA256 signature (`X-Hub-Signature-256`) over
the raw body before doing anything, and it ignores cookies
entirely.

Rules the handler enforces: signature valid, event is `push`
(`ping` answers pong, anything else is ignored), repository is
allowlisted (`arii/remote-server` -> `/home/ari/remote-server`,
`arii/tech-dancer` -> `/home/ari/repos/tech-dancer` — the buttons
container mounts `${HOME}/repos` at the host-identical path for
this), ref is `refs/heads/main`, merge is `--ff-only`. Duplicate
pushes while a pull is running coalesce instead of racing. The
last pull per repo (time, ok/failed, detail) shows under the page
header and in `/buttons/api/status` (`webhooks` field); failures
also land in the buttons container logs.

Troubleshooting: 503 = `WEBHOOK_SECRET` unset in the buttons
container; 401 = wrong secret or missing signature header; 200
with `ignored`/`skipped` = wrong repo, wrong event, or a non-main
push (all intentional); a recorded pull failure almost always
means a diverged checkout or local modifications — fix it on the
host shell (`git status`, resolve, pull), never force from the
webhook. A pull updates the checkout only: buttons code changes
still need `up -d --build buttons`, `.env` changes still need
Recreate terminal, and a terminal image update is still a
host-shell job.

### The two repos on the page

The page covers two repos and says so at the top: one card per
repo with its scope and its last auto-pull result.
`arii/remote-server` is the stack itself — the Server buttons
act there. `arii/tech-dancer` is the repo work — the Briefing
buttons read that repo only, and plans live in its
`.agents/plans/`. Group headings repeat the repo name for the
same reason, so no button is ambiguous about which repo it
touches.

### Live updates on the page

The page does not wait for a reload to show webhook activity. It
re-fetches `/buttons/api/status` every 10 seconds (skipped while
the tab is hidden) and updates the live bar under the header: a
spinner shows while a refresh is in flight or a pull is pending,
per-repo pull results land on the repo cards as they arrive, and
a timestamp shows when the page last refreshed. A pull that is
still running shows as "pulling <repo>…" with the spinner, from
the status API's `webhooks_pending` field. The Briefing view
re-fetches only on load or Refresh, for the same reason: a card
you are reading should not shift under you. Note also that
When a remote-server pull actually advances the checkout after
the buttons container started, the page also shows a rebuild
banner and highlights the Deploy buttons button — the pull
alone never rebuilds, and the banner is how the page says so. A
redundant delivery (already up to date) does not light it, and
rebuilding clears it, because the rebuild restarts the
container. GitHub's ping
(sent when a webhook is created) does more than answer pong: for
an allowlisted repo it queues an auto-pull, so creating the
webhook immediately syncs the checkout.

### Post-pull warming (tech-dancer)

After a successful tech-dancer auto-pull, the buttons server
spawns a background warming thread (never holding the pull
lock, never failing the pull record): `pnpm install
--frozen-lockfile` in the terminal container, but only when
`package.json` or `pnpm-lock.yaml` changed across that pull,
and `td context-warm` always (without `--force`, so it rewrites
only when stale or missing). `.agent-context.json` is gitignored
in tech-dancer, so warming never dirties the checkout or blocks
a later fast-forward. Warming applies to webhook pulls only in
the sense that host pulls already trigger the tracked
post-merge hook, which runs `pnpm install` itself.

## Tracked git hooks + setup script

Git never tracks `.git/hooks`, so hooks live in `hooks/` (revision
controlled) and `scripts/setup-hooks.sh` copies them into place:

```bash
npm run hooks:install   # or: bash scripts/setup-hooks.sh
npm run hooks:check     # or: bash scripts/setup-hooks.sh --check
```

It installs `hooks/post-merge` into the remote-server checkout and
`hooks/post-merge-tech-dancer` into `~/repos/tech-dancer`
(override with `TECH_DANCER_DIR`). Re-run after any fresh clone
and whenever `hooks/` changes; `--check` reports missing,
non-executable, or outdated installs. (Repo file modes may arrive
as 644 via connector pushes — the installer `chmod +x`s, so the
tracked mode does not matter.)

What the hooks do, and their guards:

- remote-server `post-merge`: if the merge touched `buttons/`,
  `docker-compose.yml`, or `caddy/Caddyfile`, it rebuilds ONLY the
  buttons container — never the terminal, which would kill live Pi
  sessions. It refuses to rebuild from inside the buttons container
  itself (`/app/server.py` present), because that would kill the
  pulling process. An oauth2-proxy config change only prints a
  restart reminder; restarting the auth edge stays a human call.
- tech-dancer `post-merge`: if `package.json` or `pnpm-lock.yaml`
  changed and `pnpm` exists, it runs `pnpm install
  --frozen-lockfile`; otherwise it prints a notice. Nothing else.
- Both hooks exit 0 no matter what: a failed hook must not make a
  good merge look bad.

Design split, stated plainly: automated pulls (webhook, Deploy
buttons) pass `-c core.hooksPath=/dev/null` and skip hooks
entirely. Hooks run only for interactive host pulls. That is what
keeps the webhook pull-only and the buttons rebuild race-free.

## package.json scripts

A root `package.json` (private, no dependencies — node/npm are
only the script runner) gives short names to the recurring
commands, so bring-up and clean-up stop being lengthy shell lines:

| Script | Runs |
|---|---|
| `up` / `down` / `restart` / `ps` / `logs` | `docker compose up -d` / `down` / `restart` / `ps` / `logs --tail=100` |
| `clean` | `docker compose down --remove-orphans` (containers only — volumes, and therefore Pi/Ollama/agy state, are preserved) |
| `pull` / `status` | `git pull --ff-only` / `git status` + `docker compose ps` |
| `buttons:rebuild` / `buttons:logs` | `docker compose up -d --build buttons` / logs for buttons |
| `terminal:recreate` / `terminal:logs` | `up -d --force-recreate terminal` / logs for terminal + ollama |
| `update:buttons` | `git pull --ff-only && docker compose up -d --build buttons` (the Deploy buttons action, from a shell) |
| `hooks:install` / `hooks:check` | `scripts/setup-hooks.sh` / with `--check` |
| `webhooks:setup` / `webhooks:check` | `scripts/setup-webhooks.sh` via the terminal container / with `--check` |
| `models:pull` | `bash scripts/pull-models.sh` (the old Pull models button, as a shell command) |
| `check` | `python3 -m py_compile buttons/server.py buttons/briefing_summary.py buttons/pi_logs.py` |

Run with `npm run <script>` from the checkout. It needs node/npm
where you run it, plus docker compose and git on PATH.

## Architecture

```
browser -> oauth2-proxy (TLS + GitHub OAuth, same gate as the terminal)
        -> caddy:8080 (internal router, compose network only)
           -> /          -> terminal:7681 (ttyd, unchanged)
           -> /buttons*  -> buttons:8000
```

- `caddy` is a plain router (Caddyfile in `caddy/`). It handles the
  ttyd websocket upgrade automatically. No TLS here — oauth2-proxy
  terminates TLS, and port 8080 is never published to the host.
- `buttons` is a stdlib-only Python server (`buttons/server.py`) in a
  small image (`docker:27-cli` + python3 + git + compose plugin),
  built on the S5 at deploy time — no GHCR push. The server binds
  0.0.0.0:8000 inside its container; the container port is not
  published, so the only ingress is via oauth2-proxy -> caddy.

## Security properties

- **Allowlist, not a shell.** The server maps nine button names to
  fixed argv lists. There is no input field, no query parameter, and
  no interpolation of user input into any command. A reached page can
  only run those nine things (plus plan-list/toggle, which take only
  a validated filename and line number).
- **Same gate as the terminal.** No separate login, no separate port.
  If oauth2-proxy is down, the buttons page is down too.
- **Webhook exception, tightly scoped.** The one path that skips
  OAuth (`POST /buttons/api/webhook`, exact match) is gated by HMAC
  signature, an allowlist of two repos, main branch only, and
  fast-forward merges only — and it can only pull checkouts, never
  run a button action, rebuild a container, or install anything.
  oauth2-proxy is pinned to v7.15.2 for CVE-2026-41059 (see the
  webhook section). If the secret ever leaks, rotate it in `.env`
  and in both repos' webhook settings; the old value stops working
  immediately.
- **Docker socket = root-equivalent.** The buttons container mounts
  `/var/run/docker.sock`, same as the terminal container. This is the
  same trust level as the terminal itself — the OAuth single-user gate
  is the boundary for both. Do not publish the buttons port or bypass
  oauth2-proxy for it.
- **SSH key mount (read-only).** Webhook auto-pulls run `git fetch`/`merge`
  over SSH, so the container mounts `${HOME}/.ssh:/root/.ssh:ro`
  (same as the terminal container) for the key and `github.com`
  in `known_hosts`. Without it, pulls fail with "Host key
  verification failed". (Deploy's own pull moved to the terminal
  container, which has the same mount.) This adds no trust: the
  socket above already gives the container root-equivalent host
  access.

## The path-identity trick (load-bearing)

The buttons container runs `docker compose` against the host daemon,
and compose resolves relative paths (`.`, `.env`) against the project
directory *as seen by the CLI*. The checkout is therefore mounted at
the same path inside the container as on the host
(`${HOME}/remote-server` -> `/home/ari/remote-server`), and the
container's `HOME` is set to `/home/ari` so `${HOME}` in the compose
file interpolates to host paths. If the container path ever differed
from the host path, `up` would hand the daemon a nonexistent bind
source and the terminal container would come up with an empty
`/srv/remote-server`. Do not "clean up" this mount to `/srv/...`
without also changing every path in `buttons/server.py`.

Deploy buttons is the deliberate exception: it runs compose
from the TERMINAL container, where the project directory is
`/srv/remote-server`, with `HOME=/home/ari` prefixed to the
compose calls so `${HOME}` still interpolates to host paths
(the terminal runs as root, so its own `HOME` would point the
daemon at `/root/repos`). The other services' relative-path
volumes in the compose file are never created by this command —
`up buttons` only touches the buttons service, whose own mounts
are all `${HOME}`-based — so the `/srv` project directory is
safe here.

Two smaller details in the same vein:

- `git pull` runs with `-c safe.directory=...`: the container runs as
  root while the checkout is owned by `ari`, and git refuses to run
  otherwise ("detected dubious ownership").
- Deploy buttons chains with abort-on-first-failure: if `git pull` fails
  (e.g. local modifications in the checkout, as in the 2026-10-27
  stub incident), the build step never runs. Fix the checkout from
  the host shell, then press Deploy buttons again.

## Deploy

On the S5, from `~/remote-server`:

```bash
git pull
docker compose up -d --build buttons caddy
docker compose up -d oauth2-proxy  # repoint upstream to caddy:8080
```

Then verify, in order:

1. `https://<domain>:<port>/` still opens the terminal (caddy passes
   `/` through to ttyd, websocket included).
2. `https://<domain>:<port>/buttons` shows the buttons page.
3. Press Status — output should show git state, running containers,
   and the terminal image tag.

At first boot, caddy may start before terminal/buttons are listening;
the first request can 502 and then recover on its own (Caddy retries
upstreams). `docker compose logs caddy buttons` if it does not.

## Rollback

If the terminal breaks behind caddy, revert the oauth2-proxy upstream
to `http://terminal:7681` and `docker compose up -d oauth2-proxy`,
then `docker compose down caddy buttons`. The terminal path is
unchanged apart from the upstream line, so rollback is one edit.

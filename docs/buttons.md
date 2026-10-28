# Buttons — phone action page

A one-page phone UI at `https://<domain>:<port>/buttons` (same URL as
the terminal, same GitHub OAuth gate) with buttons for the stack
operations that are painful to type on a phone keyboard. No free-text
command input anywhere: every button runs one fixed command list.

## Buttons

The page itself says what each button does: buttons are grouped
(Server / Briefing) and each carries a one-line subtitle under its
label, so you do not have to remember the docs to use the page.

| Button | Runs |
|---|---|
| Status | `git status` + `git log -3` in the checkout, `docker compose ps`, terminal image tags |
| Deploy buttons (rebuild this page) | `git pull --ff-only`, `docker compose up -d --build buttons` — rebuilds the buttons container itself from the checked-out code |
| Recreate terminal | `docker compose up -d --force-recreate terminal` — use after editing `.env` (editing `.env` does not change a running container) |
| Logs | `docker compose logs --tail=100 terminal ollama`, then `buttons/pi_logs.py` in the terminal container — readable Pi session audit, see below |
| Repo briefing (td, fast) | `buttons/briefing_summary.py --briefing` inside the terminal container: parses td JSON plus `gh` enrichment into a readable briefing — see below. Repo follows the Briefing picker |
| Sync & nudge agents | `td agent list-sessions`, then `td agent run-feedback-check` — list active Jules sessions, post CI/review feedback to stalled ones (global across repos, not picker-scoped) |
| Warm repo context | `td context-warm` — regenerate the picked repo's `.agent-context.json` if stale |
| Preflight check | `td preflight` — doctor + typecheck + lint + overlap + merge simulation for the picked repo |

Deploy buttons, Recreate, and Sync & nudge agents ask for a
browser confirm first, as do the per-PR action buttons on the
Briefing cards (their own confirm dialogs). Status, Logs, Repo
briefing (td), Warm repo context, and Preflight check do not.
One run at a time: starting a
second run while one is active returns an error until the first
finishes. Output streams to the page (polled every 2s); reloading
mid-run re-attaches to it. Recreating the terminal kills any running
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
the page, so the run's connection drops mid-run. The page treats
5 consecutive failed polls as "connection lost — refresh in ~30s"
and re-enables the buttons instead of spinning forever; that is
the expected outcome for this button, not a failure. If the build
itself fails, the old container is gone but the terminal still
works — fix from there and run `docker compose up -d --build
buttons` from the host shell. The same connection-lost message
covers any other case where the buttons server goes away
mid-run.

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
- **Clear output:** the button below the action buttons clears the
  stored output server-side (`POST /buttons/api/clear`), so the next
  poll does not refill the pane with old lines. A running run keeps
  its progress line and only new lines appear after the clear; a
  finished run keeps its done/failed status with an empty pane.
  Clear cancels nothing and runs no command.
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

The section is repo-general, not tech-dancer-only: a picker
above the cards selects the repo, and it sets the repo for the
cards, their action buttons, and the repo-scoped buttons
(briefing, warm context, preflight) — the server validates the
repo against an allowlist (`tech-dancer`, `remote-server`) on
every endpoint, and the summarizer takes `--repo` for its gh
api paths. The td commands run with cwd = that repo's checkout
in the terminal container, so td picks up that repo's
`project_config.json` — which is why this batch adds one to
remote-server (`github_repo` + `packageName`, mirroring
tech-dancer's shape) and ignores `.agent-context.json` there,
so warming it cannot dirty the checkout. A repo without a td
project config degrades per source like any other td failure;
it does not silently brief the wrong repo. Sync & nudge stays
global: Jules sessions span repos, so it lists and nudges
across all of them regardless of the picker.

Each open PR renders as a card: number + title (linked to
GitHub), branch, CI state with failing checks, merge state,
conflict/stale/draft flags, review verdicts, and up to two
review excerpts. Below the cards come failing runs (with failed
jobs and steps) and active Jules sessions. The section degrades
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

The page's Ledger section reads it via
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
`{sessions: [{file, mtime}]}` newest first, capped at 20.
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
- **SSH key mount (read-only).** Deploy buttons runs `git pull` over SSH, so
  the container mounts `${HOME}/.ssh:/root/.ssh:ro` (same as the
  terminal container) for the key and `github.com` in `known_hosts`.
  Without it, pull fails with "Host key verification failed". This
  adds no trust: the socket above already gives the container
  root-equivalent host access.

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

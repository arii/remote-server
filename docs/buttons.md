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
| Deploy latest | `git pull --ff-only`, `docker compose pull terminal`, `docker compose up -d terminal` |
| Deploy buttons (rebuild this page) | `git pull --ff-only`, `docker compose up -d --build buttons` — rebuilds the buttons container itself from the checked-out code |
| Recreate terminal | `docker compose up -d --force-recreate terminal` — use after editing `.env` (editing `.env` does not change a running container) |
| Pull models | `scripts/pull-models.sh` (slow, GBs — runs in the background, output streams to the page) |
| Logs | `docker compose logs --tail=100 terminal ollama` |
| Repo briefing (td, fast) | `td gh status-board` + `td gh search-prs --state open` + `td gh detect-conflicts --all` inside the terminal container (tech-dancer) |
| Repo briefing (agent, slow) | Pi print mode (`pi -p`) with the agent brief: open PRs, conflicts, failing checks, review comments, stale PRs, one top action. Slow on the local 8b model (minutes) |
| Create plan from briefing | Saves the deterministic briefing to `tech-dancer/.agents/plans/<date>-briefing-plan.md` (untracked until committed) |

Deploy, Deploy buttons, Recreate, Pull models, Repo briefing
(agent), and Create plan ask for a browser confirm first. Status,
Logs, and Repo briefing (td) do not. One run at a time: starting a
second run while one is active returns an error until the first
finishes. Output streams to the page (polled every 2s); reloading
mid-run re-attaches to it. Recreating the terminal kills any running
Pi session in it — that is the point of Recreate, but do not press
it mid-conversation.

Deploy latest vs Recreate terminal, since they look redundant:
they are not. Deploy updates — it pulls the latest code *and* the
terminal image, then restarts. Recreate only restarts, with the
current image and `.env` — no pull, no code update. A plain `up`
leaves an unchanged container alone, so `.env` edits by themselves
never take effect; that is the case Recreate exists for. Use
Deploy when you want the newest thing, Recreate when you changed
`.env` (or want a clean restart) and deliberately do *not* want a
new image. Note Deploy pulls whatever image compose is configured
for (default `:latest`, or a pinned tag if `TERMINAL_IMAGE` is
set) — the button name says "latest", the pull step says "whatever
compose resolves".

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
  (later steps never run). One exception to read carefully:
  Create plan deliberately exits 0 even if a `td` step inside it
  fails, because the error lands in the plan file instead of losing
  the plan — green there means "plan file written", not "every td
  step passed"; check the file's contents.

## Buttons are server-rendered, not JS-generated

The action buttons are rendered into the HTML by the server
(`BUTTONS_HTML` in `buttons/server.py`), and JavaScript only
attaches handlers to them. This is deliberate, after a
2026-10-28 incident where the page rendered with no action
buttons at all: the buttons had been JS-generated, so any
failure in the page script left the static parts (Clear output,
Plans heading) visible but zero buttons and no clue why. Now
the buttons exist in the raw HTML whether or not any script
runs. The page script is also split in two: the core block
(handlers, polling, progress, Clear) and the Plans block, so a
Plans failure cannot take the core buttons down. A diagnostic
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

## Plans

Below the output pane, a Plans section lists the briefing plans in
`tech-dancer/.agents/plans/*.md` (newest first), each with a
completed/total count, a progress bar, and its last-modified
date. The newest plan starts expanded; the rest start collapsed.
Tap a plan to expand its checklist, parsed from `- [ ]` / `- [x]`
lines (also `*` bullets and capital `X`) anywhere in the file —
not just the Next actions section. An expanded plan also shows a
short excerpt of the briefing itself — the plan file's td output
(PRs, CI state, conflicts), not just the checklist — because a
plan that only shows four generic checkboxes says nothing about
what the briefing actually found. And each expanded plan has
action buttons of its own: Re-run briefing (td, fast), Create a
new plan, and a Terminal link, so a plan is a starting point for
action rather than a dead end. Those run through the same
allowlisted actions as the main buttons and their output appears
in the output pane above.

Tapping a checkbox flips that one character in the plan file
itself, so the state is the file: it shows up in `git status` in
the tech-dancer checkout, survives a container recreate, and
commits with the plan. The toggle sends only a validated plan
filename and a line number to the server; the server re-checks
that the line is still a checkbox before flipping it, so a
stale tap after the file changed fails instead of editing the
wrong line. There is no free-text input: items can only be
ticked, never added or reworded from this page — add items from
the terminal if a plan needs new ones.

Create plan now appends a `## Next actions` checklist to every
new plan (review PRs/CI, resolve conflicts, triage failing
checks/comments, do the single most important action), so new
plans always have something to tick. Older plans show only the
checkbox lines they already contain.

Implementation note: the buttons container has no `/srv/repos`
mount, so Plans list/toggle run a fixed Python script inside the
terminal container via `docker compose exec terminal python3 -`
(the same transport as the briefing buttons), with the plan
payload embedded in the script — `python3 -` reads the script
from stdin, so stdin cannot also carry the payload. If the
terminal container is down, the Plans section reports
"plans unavailable" instead of a list; the action buttons are
unaffected except for the ones that also exec into terminal.

Briefing notes: the td briefing needs GitHub auth in the terminal
container (`gh` auth or `GITHUB_TOKEN`); auth errors show in the
output instead of a briefing. The agent briefing's MCP tools in Pi
print mode are unverified on the S5 — its prompt falls back to
`td`/`gh` CLI if MCP tools are absent there. The plan file lands
untracked in the tech-dancer checkout; commit or delete it from the
terminal as usual.

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
- **Docker socket = root-equivalent.** The buttons container mounts
  `/var/run/docker.sock`, same as the terminal container. This is the
  same trust level as the terminal itself — the OAuth single-user gate
  is the boundary for both. Do not publish the buttons port or bypass
  oauth2-proxy for it.
- **SSH key mount (read-only).** Deploy runs `git pull` over SSH, so
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
- Deploy chains with abort-on-first-failure: if `git pull` fails
  (e.g. local modifications in the checkout, as in the 2026-10-27
  stub incident), the pull/up steps never run. Fix the checkout from
  the host shell, then press Deploy again.

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

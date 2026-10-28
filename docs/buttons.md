# Buttons — phone action page

A one-page phone UI at `https://<domain>:<port>/buttons` (same URL as
the terminal, same GitHub OAuth gate) with buttons for the stack
operations that are painful to type on a phone keyboard. No free-text
command input anywhere: every button runs one fixed command list.

## Buttons

| Button | Runs |
|---|---|
| Status | `git status` + `git log -3` in the checkout, `docker compose ps`, terminal image tags |
| Deploy latest | `git pull --ff-only`, `docker compose pull terminal`, `docker compose up -d terminal` |
| Recreate terminal | `docker compose up -d --force-recreate terminal` — use after editing `.env` (editing `.env` does not change a running container) |
| Pull models | `scripts/pull-models.sh` (slow, GBs — runs in the background, output streams to the page) |
| Logs | `docker compose logs --tail=100 terminal ollama` |
| Repo briefing (td, fast) | `td gh status-board` + `td gh search-prs --state open` + `td gh detect-conflicts --all` inside the terminal container (tech-dancer) |
| Repo briefing (agent, slow) | Pi print mode (`pi -p`) with the agent brief: open PRs, conflicts, failing checks, review comments, stale PRs, one top action. Slow on the local 8b model (minutes) |
| Create plan from briefing | Saves the deterministic briefing to `tech-dancer/.agents/plans/<date>-briefing-plan.md` (untracked until committed) |

Deploy, Recreate, Pull models, Repo briefing (agent), and Create
plan ask for a browser confirm first. Status, Logs, and Repo
briefing (td) do not. One run at a time: starting a second run while
one is active returns an error until the first finishes. Output streams
to the page (polled every 2s); reloading mid-run re-attaches to it.
Recreating the terminal kills any running Pi session in it — that is
the point of Recreate, but do not press it mid-conversation.

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
  Colour comes **only from the exit code**, never from parsing the
  output — and that is the correct source here. Steps run via
  `subprocess` with stdout/stderr merged and no PTY, so commands
  do not emit terminal colours at all (that is why the page never
  showed red/green from the output itself). Every run ends with an
  explicit trailer line in the output: `finished: exit 0`, or
  `step failed with exit code N; aborting` on the failing step
  (later steps never run). One exception to read carefully:
  Create plan deliberately exits 0 even if a `td` step inside it
  fails, because the error lands in the plan file instead of losing
  the plan — green there means "plan file written", not "every td
  step passed"; check the file's contents.

## Plans

Below the output pane, a Plans section lists the briefing plans in
`tech-dancer/.agents/plans/*.md` (newest first), each with a
completed/total count and a progress bar. Tap a plan to expand its
checklist, parsed from `- [ ]` / `- [x]` lines (also `*` bullets
and capital `X`) anywhere in the file — not just the Next actions
section.

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

- **Allowlist, not a shell.** The server maps five button names to
  fixed argv lists. There is no input field, no query parameter, and
  no interpolation of user input into any command. A reached page can
  only run those five things.
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

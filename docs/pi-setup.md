# Pi + open model — first agentic tool (active 2026-10-27)

Pi (Zechner's terminal coding harness, `@earendil-works/pi-coding-agent`)
against a local open-weights model on the S5. No cloud API, no new
infrastructure: Pi runs inside the phone terminal, Ollama serves the model.

## What runs where

- **`ollama`** (compose service, active): CPU-only model server,
  loopback-published (`127.0.0.1:11434`). Models live in the `ollama-data`
  volume; `OLLAMA_KEEP_ALIVE=24h` so the interactive model stays warm.
- **`terminal`** (compose service): ttyd + zellij image now also carries
  Node 24.16.0 + Pi 1.4.0 (pinned in `terminal/Dockerfile` via build args,
  smoke-tested with `--version` at build time). Pi's config (`terminal/pi-config/`)
  is baked into the image and bind-mounted directly into `/root/.pi/agent/` so
  edits apply across recreates without losing sessions in `pi-data`.
- **Models** (`OLLAMA_MODELS` in `.env`): `qwen3:8b` (default — snappier
  on the phone, ~6–12 tok/s) and `qwen3:14b` (stronger, ~3–6 tok/s).
  Switch mid-session with `/model`.

## Deploy / update on the S5

From `/srv/remote-server` (or wherever the repo lives on the box):

```bash
git pull
docker compose pull terminal   # CI-built image (or: docker compose build terminal)
docker compose up -d ollama terminal
./scripts/pull-models.sh       # pulls OLLAMA_MODELS, skips present ones
docker compose exec ollama ollama list # confirm both tags are there
```

## Use it from the phone

Open the terminal in the browser, then:

```bash
pi                             # interactive TUI, default model qwen3:8b
pi --model qwen3:14b             # stronger model for a hard task
```

Inside Pi: `/model` switches models, `/help` lists commands. Pi's default
tools are read/write/edit/bash — for a **read-only review** of a repo:

```bash
pi --tools read,grep,find,ls "review the latest diff for bugs and nits"
```

Caveats carried over from `docs/agent-harness.md` §3:

- Keep prompts diff-scoped: feed Pi the diff plus excerpts, never whole
  repos. Cap context at 8K–16K for review jobs.
- Expect triage-grade output from the 8B model (summaries, nits,
  first-pass bug spotting). Cross-file reasoning and subtle
  concurrency/security flaws are where it misses — verify anything
  load-bearing yourself.
- Latency is real: a 500-token answer takes ~1 min (8B) to ~2–4 min
  (14B). This is a background-task tool, not a snappy chatbot.

## Measure the box (do this once)

```bash
time docker exec ollama ollama run qwen3:8b "say hello in one sentence"
```

Record the tok/s — the 6–12 / 3–6 estimates above are unmeasured and
should be replaced with the real number.

## BoomTick MCP tools (active 2026-10-27)

Pi also loads the BoomTick MCP server (`@arii/boomtick-mcp`, pinned to
0.6.2 in `terminal/pi-config/mcp.json` and globally installed in the
terminal image), configured with `"exposure": "direct"` and
`"toolExposure": {"*.*": "hidden"}` so the 35 underscore-named tools are
declared directly to the model without duplicate dot-alias collisions or
QuickJS `codemode` indirection. It adds structured tools Pi's built-ins
don't have:

- **GitHub ops:** search PRs, get diffs, detect merge conflicts, open
  replacement PRs, triage comments.
- **Repo ops:** inspect changed files, extract package scripts, map app
  routes, read CI logs.
- **Validation:** isolated repair-branch creation, verification suites
  (tests, Lighthouse, Playwright).
- **Jules ops:** create, monitor, and interact with macro-agent Jules
  sessions. Plus MCP resources (`repo://diff/{pr}`, `repo://ci/{pr}` …)
  and prompts (`prompt://repair-agent`, `prompt://pr-writer` …).

### Single-repo scoping — read this before use

One server entry targets **one** repo: the baked entry points at
`arii/tech-dancer` checked out at `/srv/repos/tech-dancer` (the repo the
server was built for). To retarget, edit `/root/.pi/agent/mcp.json`
inside the volume and `/reload` in Pi; for a second repo, add a second
server entry with its own `GITHUB_REPO` / `BOOMTICK_REPO_PATH`.

### What the server needs (all wired in compose already)

- **The checkout:** `~/repos/tech-dancer` on the host, mounted at
  `/srv/repos` (see § repos below). Clone it over SSH so pushes use
  your key. The server runs `td config view` at startup with cwd set
  to this checkout — if the directory doesn't exist, Node reports it
  misleadingly as `spawnSync /bin/sh ENOENT` (a nonexistent cwd
  surfaces that way).
- **`td` on PATH:** the server runs `td doctor` as a pre-flight
  check; if `td` isn't resolvable, startup fails with
  `spawnSync td ENOENT` and the connection closes. `td` is baked
  into the terminal image (venv `/opt/venvs/td`, `boomtick-cli`
  `cli-v0.14.2` installed over `0.14.0` deps for Python 3.11
  compatibility, on `PATH`) — no manual install.
- **Git safe.directory:** `terminal/gitconfig` (`safe.directory = *` +
  user identity) is baked into `/etc/gitconfig` and bind-mounted by
  compose so bind-mounted host checkouts pass `td doctor` and git
  ownership checks across container recreates.
- **GitHub auth:** `BOOMTICK_GITHUB_TOKEN` in `.env` (fine-grained PAT,
  repo scope) → `GITHUB_TOKEN` / `GH_TOKEN` inside the container. The
  server requires `gh` authenticated in `PATH` — `gh` is installed in
  the terminal image and reads the same token. Without the token, only
  public read-only ops work.
- **Write guards:** mutating operations (commits, branch creation, PR
  opening) require explicit `writeMode: true` / `pushMode: true` flags —
  Pi won't mutate a repo unless you tell it to pass those.

### S5 setup (after `git pull` + CI green on this change)

```bash
mkdir -p ~/repos
grep -q github.com ~/.ssh/known_hosts || ssh-keyscan github.com >> ~/.ssh/known_hosts
cd ~/repos && git clone git@github.com:arii/tech-dancer.git
# add BOOMTICK_GITHUB_TOKEN=<fine-grained PAT, repo scope> to .env
docker compose pull terminal && docker compose up -d terminal
# The pi-data volume predates mcp.json, so copy it in (image re-seed
# only happens on volume creation):
docker cp terminal/pi-config/mcp.json remote-terminal-1:/root/.pi/agent/mcp.json
```

Then in Pi: `/mcp` should list `boomtick` as connected. Verify with a
read-only ask, e.g. "use boomtick to search tech-dancer PRs needing
rescue." First run downloads the npm package via `npx` (needs network;
Node 22 satisfies the server's stated Node >= 22).

## Agent brief

Pi's standing instructions live in `terminal/pi-config/agent-brief.md`
(baked into the terminal image; same volume rule as `mcp.json` — an
existing `pi-data` volume needs
`docker cp terminal/pi-config/agent-brief.md remote-terminal-1:/root/.pi/agent/agent-brief.md`).
The brief covers: MCP health checks first every session, repos
watched (tech-dancer only to start — MCP is single-repo-scoped per
entry), the briefing list (open PRs + CI, conflicts, failing checks,
unresolved review comments, stale PRs, one top action), and the
read-only boundary (no merge/push/post without explicit approval).

Interactive use: start Pi and ask it to read
`/root/.pi/agent/agent-brief.md` and follow it. The buttons page can
also trigger briefings without opening Pi: "Repo briefing (td, fast)"
runs td commands, "Repo briefing (agent, slow)" runs `pi -p` with the
brief (print mode: one-shot reply, no TUI), "Create plan" saves a
dated plan to tech-dancer's `.agents/plans/`. See docs/buttons.md.

## Repos in the terminal (active 2026-10-27)

The terminal container mounts the host's `~/repos` at `/srv/repos` and
your `~/.ssh` read-only at `/root/.ssh`, so Pi can work on any checkout
and push over SSH as you:

```bash
cd ~/repos && git clone git@github.com:arii/<repo>.git   # host side
# inside Pi/the container:
cd /srv/repos/<repo> && pi
```

Prerequisites: `github.com` in the host `~/.ssh/known_hosts` (command
above — a read-only mount can't add it on first connect). Note files Pi
creates under `/srv/repos` are root-owned on the host (the container
runs as root); `chown` if that bothers you. Trust note: the container
already holds the Docker socket (root-equivalent), so the SSH mount
doesn't widen the real boundary — the single-user GitHub OAuth gate is
it.

## Terminal runtimes: what lives where

The rule that makes this durable: **system runtimes bake into the
image; repo dependencies install into the checkout.** Anything
installed with `apt` inside the running container vanishes on the next
recreate; anything installed into `/srv/repos` (a host bind mount)
persists.

- **Baked into the terminal image** (pinned, CI smoke-tested):
  Node 24.16.0 + npm, pnpm 10.28.2, `@arii/boomtick-mcp` 0.6.2,
  Python 3 + venv/pip, `td` (`boomtick-cli` `cli-v0.14.2` in venv
  `/opt/venvs/td`, on `PATH`), `gh`, `git` (with `/etc/gitconfig`
  `safe.directory = *`), Docker CLI. This covers the interpreters and
  package managers every repo needs before its own setup can run.
- **Installed per checkout** (persists in the bind mount):
  - JS repos (e.g. tech-dancer): `pnpm install` in the checkout;
    `node_modules` persists the same way. The boomtick-mcp
    validation tools (tests, Lighthouse, Playwright) run against
    these installed deps.
  - Other Python CLIs: same pattern as `td` used to need — a venv
    inside the checkout (bind mount, so it survives recreates).
- **Heavy/isolated jobs stay in the repo's own container** via the
  Docker socket (the grasping-2026 demo already works this way) —
  don't install browsers or CUDA-style toolchains into the terminal
  image.

After a CI rebuild, verify the base layer on the S5 with:
`python3 --version`, `pnpm --version`, `gh --version`, `td --help`
(`td` needs no checkout install — it's baked in).

## Config changes

Pi's provider config lives in `terminal/pi-config/` (baked into the
image). To change models or the provider:

1. Edit `terminal/pi-config/models.json` / `settings.json`.
2. Rebuild + recreate: `docker compose build terminal && docker compose up -d terminal`.
3. Note: the `pi-data` volume is **not** re-seeded on recreate — it keeps
   the old config. To force the new config in, either edit the files
   inside the volume (`docker exec terminal cat /root/.pi/agent/models.json`)
   or recreate the volume (`docker compose down -v`, which also loses
   sessions).

If Pi ever rejects the config (schema drift between Pi versions), check
`pi --help` and `https://pi.dev` — the provider block shape above
(`baseUrl` → Ollama `/v1`, `api: "openai-completions"`, `compat` flags
for the `developer` role / `reasoning_effort`) comes from the 2026-10-22
evaluation and is the first thing to re-verify.

## What's still on hold

OpenClaw, Copilot code review automation, and the Gemini route stay
deferred (see `docs/agent-harness.md`). Pi + Ollama is step one; the
others get revisited only if Pi's limits start to bite.

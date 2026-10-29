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
  smoke-tested with `--version` at build time). Pi's config lives in
  `terminal/pi-config/` and is bind-mounted into the container at
  runtime (see § Config changes); sessions persist in the `pi-data`
  volume (image content seeds it on first use). Git identity and
  `safe.directory '*'` come from the repo file `terminal/gitconfig`,
  copied to `/etc/gitconfig` in the image and bind-mounted there at
  runtime (terminal and buttons services), so git inside the
  container — which runs as root over host-owned checkouts in
  `/srv/repos` and `/srv/remote-server` — no longer trips
  dubious-ownership errors, and `td doctor` passes that check on
  every `compose up` with no manual step.
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

Pi also loads the BoomTick MCP server (`@arii/boomtick-mcp` 0.6.2,
installed globally in the terminal image and referenced via `npx -y`
in `terminal/pi-config/mcp.json`, so the global install satisfies it
without a download on every Pi start).
0.6.2 removes the duplicate tool names 0.6.1 registered, which
collided when Pi normalized `.` to `_`. The entry also sets
`"exposure": "direct"`, so the tools are declared to the model like
built-ins instead of the default codemode, where the model would
have to write QuickJS scripts to reach them (and `pi -p` would not
wait for the server at startup). It adds structured tools Pi's
built-ins don't have:

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
  into the terminal image (venv `/opt/venvs/td`, on `PATH`) — no
  manual install, and never `pip install td`: the package is
  `boomtick-cli`, the binary is `td`. It is installed from the
  BoomTick repo main branch
  (`git+https://github.com/arii/boomtick.git@main#subdirectory=cli`),
  so td updates on every terminal rebuild — pin `BOOMTICK_CLI_REF`
  to a `cli-vX.Y.Z` tag to freeze it. PyPI `boomtick-cli==0.14.0`
  predates the fix and still crashes on the image's Python 3.11
  (f-string backslash in `orchestrator.py`), which is what killed
  `td doctor` and the MCP pre-flight in the first place.
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
docker compose pull terminal && docker compose up -d --force-recreate terminal
# No docker cp needed anymore: mcp.json, models.json, and
# agent-brief.md are bind-mounted from the checkout, so the recreated
# container reads the repo files directly. Sessions survive the
# recreate — they live in pi-data, which the file mounts only
# shadow for those three paths. (settings.json is still baked in
# only — it has no compose bind mount.)
```

Then in Pi: `/mcp` should list `boomtick` as connected. Verify with a
read-only ask, e.g. "use boomtick to search tech-dancer PRs needing
rescue." The server is installed globally in the image (npx resolves
it without a download; Node 24 satisfies the server's stated
Node >= 22, and matches the 24.16.0 pinned in tech-dancer's
`.node-version`).

## Agent brief

Pi's standing instructions live in `terminal/pi-config/agent-brief.md`
(baked into the terminal image and bind-mounted over the pi-data
volume at runtime, like `mcp.json` — no docker cp needed after a
recreate).
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
  Node 24.16.0 + npm, pnpm 10.28.2, Python 3 + venv/pip, `td`
  (`boomtick-cli` from the BoomTick repo main branch in venv
  `/opt/venvs/td`, on `PATH` — see § BoomTick MCP tools), `gh`,
  `git` (with `terminal/gitconfig` as `/etc/gitconfig`, so host-owned
  checkouts work under the root container without a per-run `-c`
  flag), Docker CLI, and `@arii/boomtick-mcp@0.6.2` installed
  globally. `PATH` also includes `/root/.local/bin`, where the
  Antigravity CLI (`agy`) binary lives. This covers the
  interpreters and package managers every repo needs before its
  own setup can run.
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

Pi's provider config lives in `terminal/pi-config/` and is
bind-mounted into the container at runtime (see
`docker-compose.yml`): `mcp.json`, `models.json`, and
`agent-brief.md` each shadow the `pi-data` volume for exactly
that path (`settings.json` is baked in only, no bind mount).
To change models, the provider, or the brief:

1. Edit the file in `terminal/pi-config/` and commit it.
2. On the S5: `git pull`, then `docker compose up -d --force-recreate
   terminal` (or the buttons page's Recreate terminal — plain `up`
   will not recreate when the image is unchanged, and a
   bind-mounted file replaced by `git pull` can otherwise keep
   pointing at the old inode).
3. Sessions survive: they live in `pi-data`, which the file mounts
   do not touch. Never `docker compose down -v` to force a config
   change — that wipes sessions for no benefit now.
4. Verify inside the container that the mount, not a stale volume
   copy, is what Pi reads:
   `docker compose exec terminal cat /root/.pi/agent/mcp.json`
   should match the repo file.

The same persistence rule covers the rest of the container's home:
`pi-data` holds Pi sessions/auth/logs, `gemini-data` holds
`/root/.gemini` state (Antigravity/`agy` OAuth, settings, MCP config,
and chat history), `local-data` holds `/root/.local` (where the
`agy` binary itself is installed), and the host checkouts/SSH key
are bind mounts, so nothing a recreate needs to keep lives only in
the container's writable layer. The agy MCP config
(`/root/.gemini/config/mcp_config.json`, boomtick entry) lives in
`gemini-data`, not in the repo — it survives recreates via the
volume, and was pre-seeded from the live container's home dirs.

If Pi ever rejects the config (schema drift between Pi versions), check
`pi --help` and `https://pi.dev` — the provider block shape above
(`baseUrl` → Ollama `/v1`, `api: "openai-completions"`, `compat` flags
for the `developer` role / `reasoning_effort`) comes from the 2026-10-22
evaluation and is the first thing to re-verify.

## What's still on hold

OpenClaw, Copilot code review automation, and the Gemini route stay
deferred (see `docs/agent-harness.md`). Pi + Ollama is step one; the
others get revisited only if Pi's limits start to bite.

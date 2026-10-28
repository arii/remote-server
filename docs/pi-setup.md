# Pi + open model — first agentic tool (active 2026-10-27)

Pi (Zechner's terminal coding harness, `@earendil-works/pi-coding-agent`)
against a local open-weights model on the S5. No cloud API, no new
infrastructure: Pi runs inside the phone terminal, Ollama serves the model.

## What runs where

- **`ollama`** (compose service, active): CPU-only model server,
  loopback-published (`127.0.0.1:11434`). Models live in the `ollama-data`
  volume; `OLLAMA_KEEP_ALIVE=24h` so the interactive model stays warm.
- **`terminal`** (compose service): ttyd + zellij image now also carries
  Node 22 + Pi 1.4.0 (pinned in `terminal/Dockerfile` via build args,
  smoke-tested with `--version` at build time). Pi's config is baked into
  the image (`terminal/pi-config/`); sessions persist in the `pi-data`
  volume (image content seeds it on first use).
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
0.6.1 in `terminal/pi-config/mcp.json`), run via `npx` inside the
terminal container. It adds structured tools Pi's built-ins don't have:

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
  your key.
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

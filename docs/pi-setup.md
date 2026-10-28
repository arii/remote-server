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
docker exec ollama ollama list # confirm both tags are there
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

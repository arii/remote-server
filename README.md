# remote-server

Home-server stack for the TRIGKEY S5 Mini PC (Ryzen 7 PRO 5850U,
32 GB RAM, 500 GB SSD), with a phone-friendly terminal and the Buttons
operator console behind GitHub OAuth. The stack reuses the host's DDNS
and certbot certificates. Tailscale provides a separate admin-access path.

This README describes the checked-in implementation, not the currently
running host. Source checked November 21, 2026. Building an image, merging
a PR and deploying that image are separate steps.

## Current operator workflow

Open `/buttons/` for the React console (`frontend/`) backed by FastAPI
(`backend/`). The navigation offers Chat, Now, History and Browser, with
Review, Jobs, Queue and Briefing sections. Reads gather repository state;
writes use validated backend actions and approval gates.

- **Chat:** `/api/chat/stream` starts `deep-agent/deep_chat.py` and streams
  a turn over SSE. Approval requests resume through `/api/chat/resume`.
  The frontend uses `/buttons/api/*`; Caddy strips `/buttons` before
  forwarding to FastAPI.
- **Model routing:** `deep-agent/model_router.py` selects local Ollama
  through Tokens with Taste when available, then the TwT provider
  waterfall. Explicit preferences and local-only controls affect that
  choice. See the module and `.env.example` for the actual environment
  contract; do not infer the deployed route from this README.
- **Provider CLI turns:** the separate `/api/agent/turn` path in
  `backend/exec/agent_turn.py` allows Muse and agy. It does not run Pi.
- **Repair work:** issue dispatch uses `/api/repair/dispatch`; jobs and
  their results are exposed through `/api/jobs/*`. See
  [Buttons](docs/buttons.md) and the implementation for endpoint contracts.

Pi is not the Buttons chat/session backend. The terminal Dockerfile still
installs Pi and seeds `terminal/pi-config/`, so those files remain relevant
to manual terminal use. Their presence does not mean a Pi session powers
Buttons. The legacy `/api/pi-progress/{rid}` name remains only as a progress
fallback; the old Ask/Suggest start routes were removed.

## Architecture

```text
phone browser
  -> https://TERMINAL_DOMAIN:TERMINAL_PORT
  -> oauth2-proxy (TLS + GitHub OAuth)
  -> caddy:8080 (internal router)
       /                         -> terminal:7681 (ttyd + zellij)
       /buttons* + /cdp-stream*   -> buttons:8000 (FastAPI + React)
       /code*                    -> code-server:8080
       /hrm*                     -> terminal:3000 (when that service runs)
```

The route table is [`caddy/Caddyfile`](caddy/Caddyfile). Compose also defines
Ollama, the shared Chromium browser, browser relay, Tailscale and skills
provisioning. OpenClaw and actions-runner examples are commented out, not
started services. Tailscale is not required for the OAuth-gated terminal.

## Initial host setup

These are provisioning instructions, not commands to run during a docs
check or to refresh an existing deployment. Review the scripts, volume
mounts and environment settings before changing a host.

1. Follow [the Ubuntu setup plan](docs/setup-plan.md), including
   `scripts/bootstrap.sh` and `scripts/install-docker.sh`.
2. Confirm certbot already holds a valid certificate for the hostname used
   as `TERMINAL_DOMAIN`. The stack reads the existing certificate directory.
3. Copy `.env.example` to `.env`. Set the host paths, domain, ports, OAuth
   settings and only the optional integrations you intend to use. Generate
   secrets locally, for example with `openssl rand -base64 32`; never commit
   `.env`.
4. Create a GitHub OAuth App with the exact callback
   `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>/oauth2/callback` and set the
   matching redirect URL and credentials in `.env`.
5. From the host checkout, `docker compose up -d --build` builds/starts the
   configured stack. This changes running services. Existing hosts need a
   separate reviewed deployment plan, not a blind repeat of this step.
6. Install the certbot renewal hook with
   `sudo ./scripts/install-certbot-deploy-hook.sh` if required. The hook
   restarts oauth2-proxy on renewal to pick up the new certificate.
7. Verify the public terminal from an external network and inspect
   `docker compose ps`. Verify Buttons separately at `/buttons/`.

The terminal image defaults to the published
`ghcr.io/arii/remote-server-terminal:latest`; see `TERMINAL_IMAGE` in
`.env.example` for pinning or local builds. Pulling an image alone does not
replace a running container.

## Durable issue queue

[`scripts/queue_runner.py`](scripts/queue_runner.py) implements the
sequential, restart-safe TwT queue, with a
[user-service unit](scripts/twt-queue.service),
[manifest](scripts/config/twt-queue-issues.json), per-issue state and ledgers.
Configure paths/runtimes through `--config` or `QUEUE_CONFIG`. Bounded
reservations count toward its maximum $0.50 recorded-spend cap; unknown
unbounded spend halts. Installing this repository does not start the queue.
See [the queue runbook](docs/twt-queue.md) before enabling or recovering it.

## CI and local checks

[CI](.github/workflows/ci.yml) runs on main pushes and PRs: shell syntax and
shellcheck, Compose validation, skills integrity/shadow checks, secret scan,
code audit, frontend/extension/backend tests, eval fixtures and formatting.
Terminal image builds are classified separately: frontend and `docs/*.md`
only PRs can skip them; root README changes do not qualify for that skip.
Image publication uses the workflow's `GITHUB_TOKEN` on main.

The separate [AI review workflow](.github/workflows/ai-review.yml) runs on
PR open/synchronize/reopen and uses configured GitHub App and Gemini
secrets. Therefore "CI needs no secrets" applies to the build/validation
workflow, not every workflow in the repository. A green job is not proof
of substantive impact review: inspect the actual findings and failure logs.

For local frontend checks, use the scripts in `frontend/package.json`
(`pnpm test`, `pnpm exec tsc --noEmit`). Backend and root test commands and
required Python paths are recorded in the CI workflow. These commands do
not establish host deployment or live-service health.

## Source and documentation map

- `frontend/src/App.tsx`: console navigation and sections.
- `backend/server.py`, `backend/api/routers/`: API assembly and contracts.
- `backend/exec/`: allowlisted jobs, provider turns and execution helpers.
- `deep-agent/`: chat, repair and model-routing implementation.
- `terminal/`: terminal image, zellij configuration, CLI tools and retained
  Pi configuration; `docker-compose.yml`: services, mounts and environment.
- `chrome-extension/`, `buttons-pair/`: browser pairing and control.
- `scripts/`: host setup, verification, queue and maintenance scripts.
- [Access and threat model](docs/remote-access.md),
  [durability](docs/durability.md), [browser setup](docs/browser-setup.md),
  [repo previews](docs/repo-previews.md),
  [deployment history](docs/deployment-history.md) and
  [release process](docs/release-train.md).

Historical migration material includes [Pi setup](docs/pi-setup.md),
[agent-harness evaluation](docs/agent-harness.md),
[DeepAgents direction](docs/deepagents.md) and
[LangChain migration notes](docs/langchain-execution-guide.md). These retain
old Pi/session and cutover descriptions. Use the current source above for
what Buttons runs, not their historical rollout status. Archived material
is under `docs/archive/` and proposals under `docs/specs/`.

## Security boundaries

- Keep `.env` and credentials out of Git; inspect diffs before pushing.
- The public terminal is OAuth-gated. Protect the allowed GitHub account.
  Pairing WebSocket exceptions use their own token checks; inspect
  `.env.example` and the backend before enabling them.
- The terminal mounts the Docker socket, which grants host-level control.
  Treat that access like an admin shell.
- Certbot certificates are mounted read-only as a whole directory so
  renewal symlink changes remain visible.
- Tailscale is a separate VPN path. `TS_SERVE=1` is opt-in; do not enable a
  second public ingress as part of routine verification.

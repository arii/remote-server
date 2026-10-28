# remote-server — TRIGKEY S5 home server

Repeatable bring-up of a 24/7 home server on a TRIGKEY S5 Mini PC
(Ryzen 7 PRO 5850U, 32 GB RAM, 500 GB SSD): browser-based terminal access
with GitHub OAuth (no SSH client needed), reusing Ari's **existing dynamic
DDNS + certbot** for the terminal's TLS. Tailscale provides the VPN mesh for
admin access. Pi + local Ollama are **active** (see `docs/pi-setup.md`); the
rest of the agent harnesses stay on hold (see `docs/agent-harness.md`).

## Architecture

```
phone browser ── https://TERMINAL_DOMAIN:TERMINAL_PORT ──▶ oauth2-proxy
   (any network, no app needed)     (existing DDNS + LE cert,      │
                                     router port-forward)          │ TLS terminates here
                                                                   │ GitHub OAuth, single-user
                                                                   ▼
                                                            terminal:7681 (compose net)
                                                            ttyd --writable --max-clients=1
                                                              └─ zellij attach --create main
                                                                 (close tab = detach;
                                                                  reopen = re-attach)
```

Tailscale (VPN, outbound-only) gives admin access to everything else
(SSH, future services) and is the fallback if the public port is blocked.
The terminal itself does not require Tailscale.

Nothing else listens publicly. UFW default-deny inbound with exactly two
allow rules: the terminal port (public, OAuth-gated) and SSH from the LAN.
See `docs/remote-access.md` for the threat model.

## Quickstart

1. Install Ubuntu Server 24.04 per `docs/setup-plan.md` (phases 0–3):
   `scripts/bootstrap.sh`, then `scripts/install-docker.sh`.
   (Your existing certbot/DDNS setup is untouched — we only read its certs.)
2. `certbot certificates` — confirm a valid cert exists for the hostname
   you'll use as `TERMINAL_DOMAIN`.
3. `cp .env.example .env` and fill in every value. Generate secrets:
   `openssl rand -base64 32`.
4. Create the GitHub OAuth App with callback
   `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>/oauth2/callback`
   (GitHub allows non-443 ports — register it exactly); put the callback
   URL in `.env` (`OAUTH2_PROXY_REDIRECT_URL`).
5. `docker compose up -d --build` — builds the terminal image (pinned
   ttyd + zellij), starts oauth2-proxy + terminal + tailscale.
6. `sudo ./scripts/install-certbot-deploy-hook.sh` — restarts oauth2-proxy
   whenever certbot renews, so the new cert is picked up.
7. Phone (on cellular, not WiFi): open
   `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>` → GitHub login → terminal.
   No Tailscale app needed for this. `docker compose ps` from the phone
   to prove the socket mount works.

## GitHub repo + CI

1. Create the repo (public is fine) and push this directory. Nothing secret
   is in it: `.env*` is git-ignored and CI scans every push with TruffleHog.
2. CI (`.github/workflows/ci.yml`) runs on every push/PR:
   - shellcheck + `bash -n` on `scripts/`
   - `docker compose config` validation (dummy env, deleted after)
   - terminal image build from `terminal/Dockerfile`, pushed to
     `ghcr.io/arii/remote-server-terminal` (`main`, `sha-*`, `latest` tags)
   - TruffleHog secret scan (`--only-verified`)

   The Docker workflow replicates `arii/BoomTick`'s pattern:
   `.github/actions/setup-docker-buildx` (GHCR login + buildx),
   `docker/metadata-action` tags, `build-push-action` with GHA layer cache.

   **CI needs zero secrets** — pushes use the automatic `GITHUB_TOKEN`.
   Dependabot (`.github/dependabot.yml`) watches the Dockerfile base image
   and the Actions weekly; ttyd/zellij pins are build ARGs — bump
   `TTYD_VERSION`/`ZELLIJ_VERSION` in `.env.example` manually.
3. Secrets live in exactly one place: `.env` on the box. If you ever want
   CI-driven deploys later, that's when repo Actions secrets would enter
   the picture — not needed for build/validate/scan.
4. To run the CI-built image on the box instead of building locally:
   nothing to do — it's the default (`TERMINAL_IMAGE` in compose
   defaults to `ghcr.io/arii/remote-server-terminal:latest`, public, so
   `docker compose pull terminal` needs no login). To pin a tag or build
   locally, see the `TERMINAL_IMAGE` comment in `.env.example`.

## Docs

- `docs/remote-access.md` — investigation: browser terminal without SSH,
  GitHub OAuth. Recommends oauth2-proxy (TLS with existing LE certs) +
  ttyd + zellij; Tailscale as VPN; DDNS/certbot reuse mechanism; threat model.
- `docs/agent-harness.md` — ⏸️ **PARTLY ON HOLD** (Pi activated
  2026-10-27; OpenClaw / Copilot / Gemini still deferred). Preserved
  evaluation ("pi", LangChain, OpenClaw, OpenHands, Aider, Goose,
  PR-Agent) + three model routes (local Ollama / Copilot Pro / Gemini API).
- `docs/pi-setup.md` — Pi 1.4.0 + Node 22 in the terminal image against
  local Ollama (`qwen3:8b` default, `qwen3:14b` stronger): deploy, use
  from the phone, read-only review mode, measurement step.
- `docs/setup-plan.md` — phased Ubuntu install, hardening, Docker,
  DDNS/certbot wiring, VPN, backups, repeatability. Every phase has a
  verification step.
- `docs/usb-install-and-network.md` — bootable USB from your Linux box
  (exact ISO link + `dd`/Disks/Etcher), S5 install notes, Nest Wifi DHCP
  reservation + port forward (Google Home app path), No-IP DUC install,
  certbot.

## Repo layout

```
docker-compose.yml        services: oauth2-proxy (TLS+OAuth, public port),
                          terminal (ttyd+zellij+Node+Pi, built from terminal/),
                          tailscale (VPN), ollama (local LLM, CPU-only).
                          openclaw / actions-runner commented out
                          under profiles: ["agents"] (decision deferred).
terminal/                 Dockerfile (pinned ttyd + zellij + Node 22 + Pi
                          + docker CLI), config.kdl (phone-tuned zellij
                          config), pi-config/ (Pi Ollama provider + defaults)
config/                   oauth2-proxy.cfg (no secrets)
scripts/                  bootstrap.sh, install-docker.sh,
                          tailscale-entrypoint.sh,
                          certbot-deploy-hook.sh,
                          install-certbot-deploy-hook.sh, pull-models.sh
                          (pull-models.sh is dormant until the agents profile
                          is enabled)
.env.example              every variable, no values (copy to .env)
docs/                     the three research/plan docs
```

## Security notes

- `.env` is git-ignored. No secret is ever committed; review diffs before push.
- The terminal port is **public by design** (your existing DDNS setup).
  The GitHub OAuth single-user allowlist (`GITHUB_USER`) is the entire auth
  boundary — protect the GitHub account with passkey/2FA.
- oauth2-proxy allowlist is exactly one GitHub user; 12h cookie expiry,
  1h refresh; `reverse_proxy = false` (it is the TLS edge — don't trust
  client X-Forwarded headers).
- The terminal container mounts the Docker socket (root-equivalent, so the
  stack is manageable from the phone). Same trust level as SSH key auth.
- Let's Encrypt privkey is mounted read-only from the host certbot dir;
  the key never leaves the host. Whole-dir mount (not file mounts) so
  renewals don't go stale; the deploy-hook restarts the proxy on renewal.
- Tailscale is VPN-only here — no `tailscale funnel` (that would be a second
  public ingress); the serve fallback is documented, not enabled.
- Keep images pinned and updated; re-read `docs/remote-access.md` "what I
  could not verify" before trusting version-sensitive flags.

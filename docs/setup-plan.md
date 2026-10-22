# Setup plan — TRIGKEY S5 home server (Ubuntu 24.04 LTS + Docker)

Target: repeatable, documented bring-up of the S5 as a 24/7 home server.
Revised 2026-10-22: the agent-harness decision is **deferred** (services live
in a commented `profiles: ["agents"]` block; bring-up is the remote server
only), and remote access **reuses Ari's existing dynamic DDNS + certbot**
instead of provisioning new DNS/certs. All service config lives in this repo;
re-provisioning is `bootstrap.sh` + `docker compose up`.

Hardware: Ryzen 7 PRO 5850U (8C/16T, Vega 8 iGPU), 32 GB RAM, 500 GB SSD,
WiFi 6. Use wired Ethernet for the server role — WiFi is the fallback, not
the plan.

## Phase 0 — Physical + BIOS + router

1. Connect Ethernet, power, and a display/keyboard for install only.
2. BIOS: enable auto-power-on after power loss (AC recovery → Power On),
   disable sleep states, set boot order to USB first.
3. Note the MAC address (sticker or BIOS) for a DHCP reservation on the
   router — keeps the LAN IP stable without static config on the box.
4. Router: confirm `TERMINAL_PORT`/tcp (the separate port your existing
   certbot-served site uses) is forwarded to the S5's LAN IP. Your existing
   service presumably already forwards this port — reuse that forwarding;
   do not open anything else.

Verify: box powers itself back on after you pull the plug for 10 seconds;
from outside your LAN, `curl -vk https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>/`
reaches *something* (it'll be an OAuth login page after Phase 5 — at this
stage, just confirming the port forward works).

## Phase 1 — Ubuntu Server 24.04 LTS install

1. Flash the 24.04 LTS server ISO to USB (Balena Etcher / `dd`).
2. Boot, choose manual partitioning (GPT):
   - EFI: 1 GiB, FAT32, `/boot/efi`
   - LVM physical volume: remainder of the 500 GB
     - `lv_root`: 80 GiB, ext4, mounted at `/`
     - `lv_var`: 60 GiB, ext4, mounted at `/var` (Docker data lives here)
     - `lv_home`: 30 GiB, ext4, mounted at `/home`
     - leave the rest unallocated in the VG for snapshots / growth
   - No dedicated swap partition; use zram (enabled by default on
     Ubuntu 24.04 via `zram-generator` behavior — confirm with `zramctl`).
3. Create one admin user (e.g. `ari`), enable OpenSSH server during
   install for the initial bring-up only, import your GitHub SSH key.

ext4-on-LVM vs ZFS: ext4 wins here. ZFS wants RAM for ARC, adds complexity,
and buys you checksumming you don't need on a single new SSD. LVM gives you
snapshots and painless growth, which is all this box needs.

Verify: `lsblk` shows the LV layout; `df -h / /var /home` shows the three
mounts; `zramctl` shows compressed swap; system boots without the USB key.

## Phase 2 — First-boot hardening + public terminal port

Run `scripts/bootstrap.sh` (idempotent, `set -euo pipefail`; pass
`TERMINAL_PORT`/`LAN_SUBNET` via env to override defaults). It does:

1. `apt update && apt full-upgrade`
2. Install: `unattended-upgrades`, `ufw`, `fail2ban`, `curl`, `git`
3. `unattended-upgrades`: enable automatic security updates
   (`Unattended-Upgrade::Automatic-Reboot "true"` with a 03:30 reboot
   window — a home server can reboot at night).
4. `ufw`: default deny incoming, default allow outgoing, with exactly two
   allow rules:
   - `TERMINAL_PORT`/tcp **from anywhere** — the public web terminal.
     The GitHub OAuth single-user gate is the entire auth boundary for this
     port; read the threat model in docs/remote-access.md before opening it.
   - 22/tcp **from your LAN subnet only** — SSH for bring-up/emergencies.
   Adjust `LAN_SUBNET` to yours.
   (Tailscale and certbot renewals are outbound-initiated; they need no
   inbound rules.)
5. `fail2ban`: enable the sshd jail (defense in depth for the LAN rule).
6. Disable password SSH auth (`PasswordAuthentication no`), keep key-only.
7. Set timezone, hostname (`s5` or similar), install Tailscale-free
   mDNS (`avahi-daemon`) so `s5.local` resolves on the LAN.

Your existing certbot/DDNS setup is **not touched** by this script — it
keeps running exactly as before; we only read its certificates.

Verify: `ufw status verbose` shows deny-incoming + the two allow rules;
`sudo unattended-upgrades --dry-run` succeeds; SSH with a password
is refused; `ping s5.local` works from another LAN device.

## Phase 3 — Docker

`scripts/install-docker.sh` installs Docker Engine from Docker's official
apt repo (not the Ubuntu snap, not the distro package — both lag). Adds
the admin user to the `docker` group, enables the daemon, sets
`log-driver: json-file` with rotation (`max-size: 10m`, `max-file: 3`)
in `/etc/docker/daemon.json` so logs can't fill `/var`.

Docker vs LXD vs bare metal: Docker. Every service in this plan ships as a
maintained container image or a pinned upstream binary baked into our small
`terminal/` image; compose files are the repeatable unit. LXD adds VM-grade
isolation you don't need for a single-user box and worse image availability.

Verify: `docker run --rm hello-world` works as the admin user (after
re-login for the group); `docker info` shows the log rotation defaults.

## Phase 4 — Service stack (docker compose)

`docker-compose.yml` at repo root; one compose project (`remote`).
Every secret via environment variables; `.env` is git-ignored, and
`.env.example` documents every variable with a placeholder.
`restart: unless-stopped` on everything; healthcheck on the terminal.

| Service | Purpose | Image | Exposure |
|---|---|---|---|
| `oauth2-proxy` | TLS edge (existing LE certs) + GitHub OAuth gate, single-user allowlist | `quay.io/oauth2-proxy/oauth2-proxy` (pinned v7) | **public** `TERMINAL_PORT`/tcp |
| `terminal` | browser terminal: ttyd + zellij, Docker CLI, repo + socket mounts | built from `terminal/Dockerfile` (pinned ttyd/zellij) | compose network only, via oauth2-proxy |
| `tailscale` | VPN mesh for admin access (SSH, future services) | `tailscale/tailscale` | outbound only |

Agent services (`ollama`, `openclaw`, `actions-runner`) are commented out
under `profiles: ["agents"]` — decision deferred, see docs/agent-harness.md.

Build then bring up:

```bash
cp .env.example .env   # then fill in every value (Phase 5 first for the OAuth ones)
docker compose build terminal   # bakes pinned ttyd + zellij; smoke-tests both
docker compose up -d
```

Verify: `docker compose ps` shows all three `Up` (terminal `Up (healthy)`);
`ss -tlnp` on the host shows the terminal port and nothing else public
except what you expect; `curl -k https://localhost:${TERMINAL_PORT}/oauth2/sign_in`
returns the GitHub sign-in redirect (proves TLS + proxy are up).

## Phase 5 — Remote access wiring (DDNS + certbot reuse)

Per docs/remote-access.md. No new DNS, no new certificates, no Tailscale
required for the terminal.

1. **Confirm the cert**: `certbot certificates` — you need a valid,
   unexpired cert whose name matches `TERMINAL_DOMAIN`. If your existing
   cert is for a different name, either use that name as `TERMINAL_DOMAIN`
   or issue an additional cert with your existing certbot (out of scope
   here — your certbot already works).
2. **Fill in `.env`**: `TERMINAL_DOMAIN`, `TERMINAL_PORT`, `LE_DIR`
   (default `/etc/letsencrypt`), plus the Tailscale key.
3. **Create the GitHub OAuth App** (github.com/settings/developers → New
   OAuth App). Authorization callback URL, registered **exactly**:
   `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>/oauth2/callback`
   (GitHub allows non-443 ports). Put the client ID/secret/cookie secret
   and the callback URL in `.env` (`OAUTH2_PROXY_REDIRECT_URL`).
4. **`docker compose up -d`** (or `up -d --build` on first run).
5. **Install the certbot deploy-hook** (once, as root):
   `sudo ./scripts/install-certbot-deploy-hook.sh`. This restarts
   oauth2-proxy whenever certbot actually renews, so the new cert is
   picked up (the proxy reads certs once at startup).
6. **Phone test** (on cellular — not WiFi): open
   `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>` → GitHub login → terminal
   with the zellij session. Run `docker compose ps` from the phone.
   Negative tests: revoke the OAuth grant at
   github.com/settings/applications and confirm access dies (proves auth
   is enforced, not a landing page); confirm `http://` (no TLS) on the
   port refuses (proves no plaintext listener).

Verify: the phone gets a working shell over TLS with a valid (browser-trusted,
non-self-signed) certificate — check the lock icon; a cert warning means the
wrong cert is being served.

## Phase 5b — VPN (Tailscale) for admin access

`docker compose up -d tailscale` (uses `TS_AUTHKEY`); approve the device in
the Tailscale admin console. The phone's Tailscale app gives you VPN access
to the box for anything not on the public port (SSH via a terminal app,
future services). The web terminal itself does not need Tailscale.

If the public terminal port is ever blocked (restrictive network), the
manual fallback is documented in `scripts/tailscale-entrypoint.sh`
(`tailscale serve` the proxy over the tailnet).

Verify: with the phone on cellular and Tailscale on, `ping` the tailnet IP
of the box (or SSH to it) — proves the mesh works independently of the
public port.

## Phase 6 — Backups

What: (a) this repo (compose files, configs, scripts, `terminal/`) — pushed
to GitHub, which is backup #1; (b) named-volume data that isn't
reproducible: `tailscale-state` (re-enrollment is annoying — back it up).

How: `restic` in a cron container or host cron, nightly, to a local
USB disk (first copy) — plus `rclone` weekly to cheap object storage
if you want offsite. The `.env` file and the restic repo password are the
only irreplaceable bits — they live in a password manager, never in this
repo.

Verify: run the backup job manually once, then restore one file from
the snapshot to /tmp and diff it. A backup you haven't restored is a
wish.

## Phase 7 — Repeatability

Full re-provision from a dead SSD:

1. Reinstall Ubuntu per Phase 1 (15 min, USB).
2. Reinstall/re-verify your existing certbot + DDNS (your pre-existing
   setup — outside this repo, but the terminal depends on it).
3. `git clone <this repo>`, run `scripts/bootstrap.sh`,
   `scripts/install-docker.sh`, copy `.env.example` → `.env` and fill
   in values, `sudo ./scripts/install-certbot-deploy-hook.sh`,
   `docker compose up -d --build`.
4. Restore the restic snapshot for stateful volumes.

Optional later: a cloud-init `user-data` file or a small Ansible
playbook that performs Phases 2–3 hands-free. Not required on day one —
the scripts are the contract; automation is a refactor, not a
prerequisite.

Verify: wipe-and-rebuild is documented well enough that you could do
it from these docs alone in under an hour. Rehearse it once on a spare
drive if you're paranoid (good paranoid).

## Operational notes

- Updates: unattended-upgrades handles the OS; monthly,
  `docker compose pull && docker compose up -d` for images, plus
  `docker compose build --pull terminal` when bumping `TTYD_VERSION` /
  `ZELLIJ_VERSION` in `.env`.
- Logs: `docker compose logs -f <service>`; host journal via
  `journalctl`. All of this works from the phone terminal.
- Power: ~15–30 W idle for this class of box; fine for 24/7.
- If the box ever needs a rebuild, the only irreplaceable bits are
  `.env` values and the restic repo password — keep those in a password
  manager, not in this repo.

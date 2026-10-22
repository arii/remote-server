# Remote access — browser terminal without SSH, GitHub OAuth auth

Researched October 2026; **revised 2026-10-22** for Ari's actual setup:
he already runs dynamic DDNS + certbot serving on a separate (non-443) port.
This design **reuses that existing DDNS hostname and those Let's Encrypt certs**
for the terminal's TLS instead of provisioning new ones. Target: Ubuntu Server
24.04, Docker, residential NAT, dynamic IP. Single user (Ari). Phone web
browser, no SSH client. GitHub OAuth as the identity.

## Recommendation

**`https://<DDNS>:<PORT>` → oauth2-proxy (terminates TLS with Ari's existing
Let's Encrypt certs, GitHub provider, single-GitHub-user allowlist) →
ttyd + zellij. Tailscale stays as the VPN mesh for admin access; the terminal
does not require it.**

Request flow:

```
phone browser ──https://TERMINAL_DOMAIN:TERMINAL_PORT──▶ oauth2-proxy
   (any network, no app)                                  │ TLS w/ existing LE cert
                                                          │ GitHub OAuth, 1-user allowlist
                                                          ▼
                                                   terminal:7681 (compose net)
                                                   ttyd --writable --max-clients=1
                                                     └─ zellij attach --create main
                                                          (session persists; closing the
                                                           tab "detaches", reopening
                                                           re-attaches)
```

Why this shape: Ari already owns the hard parts (DDNS hostname, valid certs,
a forwarded port). oauth2-proxy terminates TLS *itself*
(`--tls-cert-file`/`--tls-key-file`, verified flags in v7.x), so there is no
Caddy/Traefik to run and no new certificate to babysit — the certbot
deploy-hook restarts the proxy on renewal. GitHub is enforced at the only
public listener. Tailscale remains for everything the terminal shouldn't
expose (SSH, future services) and as a fallback path if the public port is
blocked on some network (see §VPN).

## Terminal stack — what was evaluated

### Web terminal: ttyd vs wetty vs GoTTY

All serious web terminals render with **xterm.js** (ttyd, wetty, VS Code,
code-server) — so the on-screen terminal itself is near-identical. The choice
is about the server side:

| | ttyd | wetty (WeTTY) | GoTTY |
|---|---|---|---|
| Language / size | C, single static binary (~2 MB) | Node.js app | Go |
| Frontend | xterm.js | xterm.js | hterm (older, worse mobile) |
| Maintenance (Oct 2026) | Active: v1.7.7, 12.5k stars, releases current | Maintained (butlerx/wetty) | Effectively stale (yudai/gotty) |
| Auth story | None built-in — designed for a proxy in front (`-H` auth-proxy header support) | SSH/login based; heavier | Basic-auth password only |
| Mobile-relevant | `-W` writable, `--max-clients`, `-t` client options (fontSize), ping keepalive | Comparable xterm.js UX, needs an SSH daemon | Skip |

**Pick: ttyd 1.7.7.** Lightest, actively maintained, built for exactly this
"proxy handles auth" topology. Two gotchas baked into the compose file:
(1) ttyd ≥ 1.7.5 is **read-only by default** — `--writable` is required or the
shell is view-only; (2) `--max-clients 1` — one phone, one session.

wetty is the fallback if you ever want SSH-credential login instead of OAuth;
GoTTY is out (stale, hterm).

### Session persistence: zellij vs tmux vs abduco/dtach

The phone problem with tmux is not tmux — it's the **prefix key**. Touch
keyboards (Gboard etc.) have no usable Ctrl/Alt, so tmux's invisible
`Ctrl-b`-then-key system is unusable on a phone without switching to a hacker
keyboard. What actually matters on a phone:

| | zellij (picked) | tmux | abduco / dtach |
|---|---|---|---|
| Discoverability | **On-screen hint bar** — bindings visible, no memorization | Must memorize prefix sequences | N/A (no panes at all) |
| Touch | **Mouse mode on by default**: tap-to-focus panes, touch scroll | Needs `set -g mouse on` config | N/A |
| Keybindings | Modal, no prefix key for pane ops | `Ctrl-b` prefix for everything | Detach-only (`Ctrl+\`-class) |
| Resume | `zellij attach --create main`; sessions auto-persist | `tmux new -A -s main` | Attach/detach only, **no scrollback restore** |
| Config | `simplified_ui`, `pane_frames` — tuned for small screens | Needs a full `.tmux.conf` to get close | Nothing to configure |
| Footprint | Rust binary, ~22 MB RSS | C, ~2 MB/session | 52 KB |

**Pick: zellij 0.45.1.** The decisive phone-UX points: the hint bar (you can
see what to press), tap-to-focus without any modifier key, and
`simplified_ui` (no nerd-font glyphs the phone can't render).

Key design insight: **with `zellij attach --create main` as the ttyd command,
the user never needs the detach keybinding at all.** Closing the browser tab
kills the websocket; ttyd SIGHUPs the zellij client; the session persists
server-side; reopening the URL re-attaches to the exact same session,
scrollback intact. Detach becomes "close the tab" — the most phone-native
gesture there is. abduco/dtach were considered for their zero-keybinding
minimalism, but they restore no terminal state on reattach (blank screen),
which is worse than zellij on every axis that matters here.

Concrete mobile tuning (in `terminal/config.kdl`, baked into the image):
`simplified_ui true`, `mouse_mode true`, `pane_frames false`
(small screens), `default_shell "bash"`. ttyd flags:
`--writable --max-clients=1 --ping-interval=30 -t fontSize=15`
(30s ping keeps the websocket alive on flaky mobile data).

The terminal image (`terminal/Dockerfile`, built by compose) bundles
ttyd + zellij + the Docker CLI, mounts the repo at `/srv/remote-server` and
the Docker socket — so `docker compose ps/up/logs` all work from the phone.
The socket mount is root-equivalent by design; the GitHub OAuth single-user
gate is the security boundary (see threat model).

## VPN — Tailscale kept, WireGuard documented as the alternative

Ari asked for a VPN in the stack. Decision: **Tailscale only** (already in the
compose). Rationale:

- The terminal's primary path is the public DDNS URL — no VPN needed for it.
- Tailscale covers what the public path must not: SSH, and any future service
  (dashboards, Ollama if the agent profile is ever enabled) — all reachable
  over the mesh with zero additional public listeners.
- It is the fallback when the public port is unreachable: restrictive
  hotel/café firewalls often block non-443 ports, while Tailscale's NAT
  traversal usually gets through. Manual fallback command is in
  `scripts/tailscale-entrypoint.sh` (serve the proxy over the tailnet).
- Phone UX is one app toggle; full-tunnel mode is available in the app.

**Alternative (documented, not chosen): self-hosted WireGuard**
(`linuxserver/wireguard` container). You get no third-party coordination
server and full control of keys — at the cost of a UDP port forward on the
router, manual key distribution, and configuring the WireGuard app on the
phone yourself. Choose this if Tailscale-the-company ever becomes a problem;
the compose change is one service.

**Rejected: both.** Two VPNs for one user is two key systems, two phone apps,
two things to debug — with no additional capability.

## Reusing the existing DDNS + certbot — concrete mechanism

Nothing new is provisioned. The moving parts:

1. **`.env` parameters** (`TERMINAL_DOMAIN`, `TERMINAL_PORT`, `LE_DIR`):
   `TERMINAL_DOMAIN` must be a hostname your DDNS already keeps pointed at
   the box **and** a domain certbot already holds a cert for — verify with
   `certbot certificates`. `TERMINAL_PORT` is the separate port your existing
   setup already serves (your router presumably already forwards it; confirm
   it forwards to the S5). Works with any DDNS provider — only the hostname
   changes.
2. **TLS termination in oauth2-proxy**: `--https-address=0.0.0.0:${TERMINAL_PORT}`
   with `--tls-cert-file=/certs/live/${TERMINAL_DOMAIN}/fullchain.pem` and
   `--tls-key-file=.../privkey.pem`. The **whole** `${LE_DIR}` (`/etc/letsencrypt`)
   is mounted read-only at `/certs` — not individual files — because `live/`
   contains symlinks that certbot re-points on every renewal; file-level mounts
   would pin the old certificate. The proxy runs as root in-container solely
   to read the root-owned privkey; the key never leaves the host.
3. **Renewal**: `scripts/install-certbot-deploy-hook.sh` (run once as root)
   installs `scripts/certbot-deploy-hook.sh` into
   `/etc/letsencrypt/renewal-hooks/deploy/`. certbot runs deploy-hooks only
   when a cert actually renews; the hook restarts the oauth2-proxy container
   so it loads the new certificate. oauth2-proxy reads certs once at startup —
   without the restart it would serve the expired cert indefinitely.
4. **GitHub OAuth App**: callback URL
   `https://<TERMINAL_DOMAIN>:<TERMINAL_PORT>/oauth2/callback`, registered
   **exactly** (GitHub allows non-443 ports in callback URLs). Put it in
   `.env` as `OAUTH2_PROXY_REDIRECT_URL`.
5. **No Tailscale needed for the terminal.** The phone opens the public URL
   directly — GitHub login in the mobile browser, then the terminal.

## Threat model note (updated for public exposure)

This design **does** expose a port to the internet (unlike the earlier
Tailscale-only draft) — deliberately, reusing Ari's existing public setup.
The consequences:

- **The oauth2-proxy GitHub gate is the entire auth boundary.** There are no
  passwords anywhere; identity is GitHub OAuth with a single-username
  allowlist (`GITHUB_USER`), 12h cookie expiry, 1h refresh against GitHub.
  A GitHub account takeover = shell access. **Protect the GitHub account with
  a passkey/2FA** — this is the sharpest residual risk and it is unchanged
  from the earlier design.
- **What the public sees**: one TLS port serving an OAuth login page. No
  terminal bytes, no headers, no version strings leak before authentication
  (oauth2-proxy serves its own sign-in page; ttyd is unreachable except
  through the proxy on the compose network).
- **Defense in depth**: `--max-clients 1` on ttyd (no concurrent sessions);
  `reverse_proxy = false` (don't trust client X-Forwarded headers at the
  edge); `cookie_secure/httponly/samesite`; UFW default-deny with exactly two
  allow rules (terminal port public, SSH LAN-only); fail2ban on the SSH jail.
  Consider adding fail2ban or rate-limiting on the oauth2-proxy logs later —
  not in this revision.
- **Terminal container = root-equivalent** (Docker socket mounted so the
  stack is manageable from the phone). This is intentional and documented;
  it means the OAuth gate's integrity is load-bearing — same as SSH key
  auth on a normal admin box.
- **Out of scope**: physical security of the box; Tailscale control-plane
  trust (only used for admin VPN, not the terminal path); GitHub's own
  security.

## What I could not verify

- **ttyd release asset name** (`ttyd.x86_64`): the Dockerfile downloads it
  from the GitHub release and smoke-tests with `--version` at build time, so
  a rename fails loudly with the fix location in the comment. zellij's
  `zellij-x86_64-unknown-linux-musl.tar.gz` naming is long-standing.
- **zellij `attach --create` flag name** in 0.45.x: stable for years, but
  confirm with `zellij attach --help` inside the built image if attach fails.
- **zellij KDL keys** (`simplified_ui`, `mouse_mode`, `pane_frames`,
  `default_shell`): long-standing; zellij warns (does not crash) on unknown
  keys — check `docker compose logs terminal` after first start.
- **GitHub OAuth callback with port**: allowed per GitHub's docs and widely
  used, but register the URL exactly — a mismatch fails closed at GitHub.
- **oauth2-proxy `user: "0:0"`**: the official image already runs as root;
  the directive makes the LE-privkey read explicit. If a future image ships
  non-root, this line is what keeps TLS working.
- **quay.io/oauth2-proxy v7.9.0 flag names**: `--tls-cert-file`,
  `--tls-key-file`, `--https-address`, `--http-address=` (empty disables) are
  current v7.x; flag names occasionally deprecate — check `--help` at deploy.
- **Phone UX specifics** (touch keyboard behavior, pinch-zoom in xterm.js):
  assessed from xterm.js/ttyd/zellij docs, not hands-on testing on Ari's
  phone — verify on first login.

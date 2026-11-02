# BrowserMCP pairing — agent sees your Chrome (via extension)

BrowserMCP's relay is **loopback-only by design**: the extension talks to
`ws://127.0.0.1:<port>` on the machine where Chrome runs, and the daemon
binds the first free port of 9010–9012 (recorded in `~/.browsermcp/daemon.json`
next to the agent). So the laptop's extension cannot dial the S5 daemon
directly — traffic goes through an SSH tunnel plus the in-stack relay:

laptop extension -> laptop localhost:9010 -> ssh -L -> S5 host ->
terminal:19010 (compose network) -> browser-relay ->
daemon 127.0.0.1:9010 (inside terminal's network namespace)

`browser-relay` shares terminal's network namespace, so no terminal recreate
is needed. It listens on 19010 because the daemon already holds 127.0.0.1:9010
there, and it publishes no ports (forbidden with this network mode) — the
host reaches it as `terminal:19010`, which is what the tunnel targets.

## One-time setup (laptop)

1. Install the [Browser MCP extension](https://browsermcp.io/install) in
   Chrome and pin it to the toolbar.
2. Join the tailnet on the laptop (Tailscale app, same account as the S5).
3. Run the one-shot laptop setup (writes the tunnel wrapper + systemd unit,
   enables them, and verifies the daemon answers). On the laptop:

   ```sh
   scp ari@100.85.113.67:/home/ari/remote-server/scripts/setup-browsermcp-tunnel.sh /tmp/ \
     && bash /tmp/setup-browsermcp-tunnel.sh
   ```

   This auto-starts the tunnel on login, restarts it on drops, and
   re-resolves the terminal IP every start (terminal recreates heal
   automatically). If SSH asks for a password, set up key auth first:
   `ssh-copy-id ari@100.85.113.67`. Re-run anytime (idempotent).
   Manual alternative (no autostart): `ssh -N -L 9010:<terminal-ip>:19010 ari@100.85.113.67`.

4. In any S5 agent (Pi, Muse, agy), ask it to open a page. The first time,
   click **Allow** in the extension popup after matching the six-digit code.

## Troubleshooting

- With the tunnel up, `curl -s http://localhost:9010/browsermcp/status` on
  the laptop should answer with a browsermcp status line; if refused, the
  tunnel or the relay is down (`docker compose logs browser-relay` on the S5).
- `cat /root/.browsermcp/daemon.json` in the terminal shows the daemon's live
  port — the extension's port setting must match it (default 9010).
- One browser at a time: a second Chrome (e.g. phone + laptop) replaces the
  first; the replaced one gets the connection back when the other goes away.
- The daemon exits after 10 idle minutes; agents respawn it on next use.

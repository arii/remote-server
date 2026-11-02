# Local buttons UX (laptop)

Run the phone UI on your laptop without the S5 edge (no OAuth2-proxy,
Caddy, Tailscale, or DDNS). The page and all actions work the same;
only the login gate is skipped, so keep it on loopback.

## Prerequisites

- Docker with compose v2.
- `.env` copied from `.env.example` with `HOST_HOME=$HOME`.
- `$HOME/repos` containing your checkouts (at minimum whatever repo you
  want to brief; `tech-dancer` is the default brief target).
- `gh` authenticated (run `gh auth login` once inside the terminal
  container, or bind your existing `~/.config/gh`).
- Disk for the Ollama model (`qwen3:8b` ≈ 5GB; pulled on first use).

## Start

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml \
  up -d --build buttons terminal ollama
```

Open [the local buttons page](http://localhost:8000/buttons) in your
laptop browser.

Stop: `docker compose -f docker-compose.yml -f docker-compose.local.yml
down` (volumes keep gh auth, models, and history).

## Agent access (browser-mcp)

`localhost:8000` is your laptop's loopback — it is **not** reachable from
inside the S5 Docker network, so an agent driving browser-mcp cannot use
that URL. To let an agent see the local UX, give it an address that is
reachable from the agent's network instead:

- easiest: your Tailscale IP, e.g. `http://100.x.y.z:8000/buttons`
  (requires binding the port to that interface instead of loopback), or
- `ssh -R 18000:localhost:8000 <s5>` and share
  `http://localhost:18000/buttons` *on the S5 side* (from a container:
  `http://host.docker.internal:18000/buttons` where supported,
  else the bridge gateway IP — ask the agent to resolve it).

Production remains `https://dev-onasafari.ddns.net:8443/buttons`.

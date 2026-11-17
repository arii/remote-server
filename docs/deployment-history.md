# Deployment history

## #1332: Open WebUI retired

Open WebUI, its `/webui` shell shim, and its Caddy proxy routes were removed.
Ambient chat uses the Buttons Chat view. This entry preserves the legacy names
solely for operator cleanup; it is not an active service setup guide.

Buttons keeps `/buttons/api/*`: Caddy strips `/buttons` before proxying to the
backend. The production frontend builds with that base. Root `/api/*` had only
the retired upstream and now has no dedicated route. It reaches the existing
terminal catch-all, where ttyd returns 404 for unknown paths. No replacement API
route was added. The OAuth gate and all remaining Caddy handlers are unchanged.

### Apply on the S5 host

From the updated host checkout, recreate **only `buttons`**. Reload `caddy`;
its directory-mounted configuration does not require container recreation.
Never recreate or restart `terminal`. No other retained service needs a restart.

```bash
./scripts/buttons-up.sh
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

Compose service removal does not stop an already-running orphan. After backing
up any chat/upload data you need, record the actual volume name before removing
the retired container. Run these commands manually on the S5, in one shell:

```bash
retired_volume="$(docker inspect remote-openwebui-1 --format '{{range .Mounts}}{{if eq .Destination "/app/backend/data"}}{{.Name}}{{end}}{{end}}')" &&
test -n "$retired_volume" &&
printf 'Retired data volume: %s\n' "$retired_volume" &&
docker stop remote-openwebui-1 &&
docker rm remote-openwebui-1
```

The former `open-webui-data` volume is retained on disk until an operator chooses
to delete it. Compose may have prefixed its name with the project name. After
checking the recorded name and approving irreversible data deletion, manually
run in that same shell:

```bash
docker volume inspect "$retired_volume"
docker volume rm "$retired_volume"
```

No startup script deletes data. Avoid full-stack `up`, `down`, or broad orphan
cleanup, which can affect the terminal or unrelated containers.

### Verify after deployment

- Signed out: private routes still require GitHub OAuth.
- Signed in: `/buttons/`, `/buttons/api/agent/state` and `/cdp-stream/` respond;
  the terminal, Chat view and the other configured apps still work.
- Signed in: `/api/agent/state` and `/webui/` return 404. An OAuth redirect before
  login is not evidence of the application response.

These are operator checks after deployment, not a claim that this commit was
deployed or tested live on the S5. Shared Nebius configuration remains because
both Buttons and the terminal use it.

# TODO — immediate steps after container restart

## 1. Sync checkout (`#987` merged)
```bash
cd /srv/remote-server
git checkout -q main && git pull --ff-only origin main
git branch -D feat/provision-skills-bringup
```

## 2. Restart the stack
```bash
docker compose up -d --build buttons
docker compose ps   # all services healthy / running
```

## 3. Verify skill provisioning (new in `#987`)
```bash
docker compose ps skills-provisioner   # must show Exited (0)
ls .agents/skills | wc -l              # expect 26+ (26 locked + tracked lane skills)
bash scripts/verify-skills.sh          # expect: OK (26 locked skill(s) match)
```

## 4. Verify services
- Terminal: open `https://${TERMINAL_DOMAIN}:${TERMINAL_PORT}` (OAuth → ttyd → zellij).
- Buttons health: `curl -fsS http://127.0.0.1:<buttons-port>/health` (see compose).
- Smoke: `/opt/venvs/td/bin/td audit-impact --help` inside terminal; `boomtick-mcp` present.

## 5. Known non-blocking items (no action unless red)
- `test:root` shows ~12 metering errors locally (`Nebius spend cap unverifiable`) — host ledger pollution, CI is green. Clean the ledger if you want them quiet.
- GitHub API flaked during the `#987` merge (GraphQL errors); merge landed via REST. If `gh` acts up again, wait and retry.

## Done when
- [x] Checkout on `main`, in sync with `origin/main`, no leftover branches.
- [x] `skills-provisioner` exited 0 and `verify-skills.sh` passes.
- [x] Terminal + buttons reachable; smoke commands pass.

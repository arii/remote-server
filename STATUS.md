# Component status

Last verified: 2026-11-21 on `main` at #1521, Python 3.11, Node 22, no Docker, no openssl, no live S5 host. "Working" means the automated checks below pass in that sandbox. It does not mean the component is healthy on the S5 host.

| Component | Status | Evidence |
| --- | --- | --- |
| backend (`backend/`) | working | 1633 unittest pass, 4 skipped (`npm run test:backend`). `/health` boot smoke test (`backend/tests/test_health_boot_smoke.py`) starts uvicorn with `PYTHONPATH=<repo>` only. The modules the backend shares with deep-agent (`cli`, `runs`, `pricing`, `model_router`, `s5tools`, `ledger`, `twt_*`) live in `backend/shared/` ([#1508](https://github.com/arii/remote-server/issues/1508)). |
| frontend (`frontend/`) | working | 1380 vitest tests in 61 files pass. `pnpm --filter frontend lint` (tsc, knip, oxlint) reports 0 errors. `pnpm --filter frontend build` succeeds. |
| deep-agent (`deep-agent/`) | experimental | Covered by backend and root tests. Live model calls are not run in the sandbox. `run_coder` honors `model_tier` ([#1521](https://github.com/arii/remote-server/pull/1521)): `strong` uses the env var `CODER_STRONG_MODEL` and raises `ValueError` if it is unset or blank (`pipeline.py` turns that into BLOCKED "pipeline error: ..."); `fast` or none keeps `CODER_MODEL` or the router default. Gaps: tests stub `build_model`, so a real `CODER_STRONG_MODEL` value is unconfirmed against `model_router`; `scripts/queue_runner.py` now routes `model:strong` issues and auto-escalated issues (default 2 counted failures) straight to the strong tier and requires `CODER_STRONG_MODEL` in its config `environment` (see `docs/twt-queue.md`); the plan path (`_default_plan_fn`) and the resume path still call `build_model(CODER_MODEL)` and never escalate. `backend/shared/task_router.py` now picks concrete `provider/id` pins per tier from the live TwT catalog at dispatch (task features -> tier suggestion + model pick, audited as `routing-decision` ledger rows); the worker applies `picks.fast`/`picks.strong` as `CODER_MODEL`/`CODER_STRONG_MODEL`. |
| terminal (`terminal/`) | unverified | Image build needs Docker. CI builds it, this sandbox cannot. |
| caddy (`caddy/`) | unverified | Needs Docker and the S5 host. |
| docker-compose stack | unverified | No Docker in the sandbox. Compose validation runs in CI only. The `/var/lib/buttons` named-volume contract and a read-only check (`scripts/buttons-volume-check.sh`) are documented in `docs/buttons.md`; the check has not been run against the live S5 volume: [#1517](https://github.com/arii/remote-server/issues/1517). |
| webhook and certbot hooks (`hooks/`) | experimental | `test_certbot_deploy_hook` errors in the sandbox (needs openssl). Not exercised against a real certificate. |
| chrome-extension | experimental | Profile learning from pasted text, files, fetched URLs and read-only Google Drive import (disabled until an OAuth client ID is set) feeds the Review & Merge Queue with user-editable prompt guidance and accept-only guidance suggestions and an Ask card answering from ticked page/tab/vault/target sources (vitest suite passes). Canonical form-observer extension is undecided: [#1510](https://github.com/arii/remote-server/issues/1510). |
| buttons-pair | experimental | 31 tests pass. Lint runs in `npm run lint`. |
| browser relay | broken on S5, untested here | `tests/test_browser_relay.py` compose test skips without a working `docker compose`. Upstream not restored on S5: [#1513](https://github.com/arii/remote-server/issues/1513). |
| gdocs-access | experimental | No automated tests run in this sandbox. Not exercised against Google. |
| scripts (`scripts/`) | working | `bash -n` and shellcheck run in `npm run lint` and CI. `scripts/check-no-shadow-skills.sh` passes. |
| root tests (`tests/`) | working | 1552 run, 6 errors that need docker, 28 skipped. Tests needing a working `docker compose` or `openssl` skip with a reason when absent. `test_twt_bounds` is fixed by [#1515](https://github.com/arii/remote-server/pull/1515). CI runs `npm run test:root`. [#1509](https://github.com/arii/remote-server/issues/1509). |

## Not verifiable in the sandbox

- Docker builds and `docker compose` behavior.
- Live S5 services: Tailscale, relay, runtime data, S5 storage.
- Live LLM provider calls.

## Reproduce

```
npm run lint && npm test
npm run test:root
pnpm --filter frontend build
```

# Component status

Last verified: 2026-11-21 on branch `chore/cleanup-and-open-issues`, Python 3.11, Node 22, no Docker, no openssl, no live S5 host. "Working" means the automated checks below pass in that sandbox. It does not mean the component is healthy on the S5 host.

| Component | Status | Evidence |
| --- | --- | --- |
| backend (`backend/`) | working | 1633 unittest pass, 4 skipped (`npm run test:backend`). Clean-checkout `/health` smoke test is still missing: [#1508](https://github.com/arii/remote-server/issues/1508). |
| frontend (`frontend/`) | working | 1379 vitest tests in 61 files pass. `pnpm --filter frontend lint` (tsc, knip, oxlint) reports 0 errors. `pnpm --filter frontend build` succeeds. |
| deep-agent (`deep-agent/`) | experimental | Covered by backend and root tests. Live model calls are not run in the sandbox. `coder.py` accepts `model_tier` but ignores it, so model escalation in `pipeline.py` has no effect on the coder. |
| terminal (`terminal/`) | unverified | Image build needs Docker. CI builds it, this sandbox cannot. |
| caddy (`caddy/`) | unverified | Needs Docker and the S5 host. |
| docker-compose stack | unverified | No Docker in the sandbox. Compose validation runs in CI only. Named-volume boundary is undocumented: [#1517](https://github.com/arii/remote-server/issues/1517). |
| webhook and certbot hooks (`hooks/`) | experimental | `test_certbot_deploy_hook` errors in the sandbox (needs openssl). Not exercised against a real certificate. |
| chrome-extension | experimental | 65 vitest tests pass. Canonical form-observer extension is undecided: [#1510](https://github.com/arii/remote-server/issues/1510). |
| buttons-pair | experimental | 31 tests pass. Lint runs in `npm run lint`. |
| browser relay | broken on S5, untested here | `tests/test_browser_relay.py` compose test needs Docker. Upstream not restored on S5: [#1513](https://github.com/arii/remote-server/issues/1513). |
| gdocs-access | experimental | No automated tests run in this sandbox. Not exercised against Google. |
| scripts (`scripts/`) | working | `bash -n` and shellcheck run in `npm run lint` and CI. `scripts/check-no-shadow-skills.sh` passes. |
| root tests (`tests/`) | partly broken | 1546 run, 10 errors, all environmental: `test_certbot_deploy_hook` 1, `test_durability` 4, `test_browser_relay` compose 1 (no Docker or openssl), `test_twt_bounds` 4 subtests (fix in open PR [#1515](https://github.com/arii/remote-server/pull/1515)). Clean-checkout runnability: [#1509](https://github.com/arii/remote-server/issues/1509). |

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

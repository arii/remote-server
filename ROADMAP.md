# Roadmap

`remote-server` is scoped to the S5 host infrastructure and the core Buttons console and agent loop. Product definition: [PRODUCT.md](PRODUCT.md) and [PRD.md](PRD.md). Parked items: [docs/ideas/IDEAS.md](docs/ideas/IDEAS.md).

## In scope (Want)

1. Attention queue ("Now") with tap-to-confirm destructive actions.
2. PR review, merge and update, plus issue list, assign and create.
3. Job dispatch with live progress and the repair worker.
4. Chat with the agent (SSE stream, approvals, resume).
5. History: unified ledger of jobs and webhooks, plus health and traces.
6. Model routing (local Ollama, then provider waterfall) and cost tracking (`twt`, TokensWithTaste).
7. Phone-friendly terminal behind GitHub OAuth (ttyd, zellij, Caddy).
9. LocalForm AI (`chrome-extension/`): hybrid AI form and job-application filling workflow. Kept and built on.
12. Webhook auto-pull, certbot hook and Tailscale access.

## Reconfigure

10. `gdocs-access/`: kept. Replace the hardcoded doc ID with proper config and OAuth, then use it for the agent ledger and webhook plans.

## Out of scope for `main`

- Parked as Maybe (see IDEAS.md): browser relay and `buttons-pair/` (#8), `.agents/skills` orchestration (#11), skills provisioning and lockfile (#13).
- Archive: tag `pre-cleanup-2026-11-21`, branch [`archive/pre-cleanup`](https://github.com/arii/remote-server/tree/archive/pre-cleanup), commit [`7f3a2cc`](https://github.com/arii/remote-server/commit/7f3a2cc9db42156a9549145839ff99819cacce78).

## Now: verify on the S5 host

Run the smoke tests on the S5 host, which has docker, openssl and pytest:

- Backend: `PYTHONPATH=./deep-agent python3 -m unittest discover -s backend/tests` (sandbox baseline: 1633 pass, 4 skipped).
- Frontend: `pnpm --filter frontend test` (1379 pass).
- Extensions: `pnpm run test:extension` (buttons-pair 31, chrome-extension 65 pass).
- Static checks: `npm run compile-check && npm run lint:python`.
- Root suite: `PYTHONPATH=./deep-agent python3 -m unittest discover -s tests`. The sandbox run had 5 failures and 10 errors. Most are likely environmental: `test_eval_fixtures_gate`, `test_pipeline_e2e` and the `test_coder_backend` verification test need pytest. `test_certbot_deploy_hook`, `test_durability` and `test_browser_relay` need openssl and docker. Confirm these pass on S5.

### Reproduce `test_twt_bounds` on S5

`test_twt_bounds::BoundsTests::test_fanout_and_resolver_merge_explicit_output_budget` fails in the sandbox with four subtests (entrypoints `triage_fanout` and `resolve_conflicts`, ceilings 8192 and 1500). Each raises `AttributeError: 'NoneType' object has no attribute 'kwargs'`. The cause is unexplained.

1. Run it on S5: `PYTHONPATH=./deep-agent python3 -m unittest tests.test_twt_bounds -v`.
2. If it fails there, find which object is `None` where `.kwargs` is read. This is probably a mocked model or call capture that was never set.
3. If it passes on S5, record the missing sandbox dependency.
4. Fix the cause or document it.

## Next: core loop hardening

- Keep each Want area (#1 to #7, #12) covered by passing tests on S5.
- Resolve the open decisions in PRODUCT.md (NTFY UI, `/api/agents*` shape).

## Cleanup (gated on PR review)

`chrome-extension/` and `gdocs-access/` stay in `main`. No deletion or pruning of the 85 remote branches happens until the exact diff and file list is reviewed in a PR.

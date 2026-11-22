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

### `test_twt_bounds` (fixed)

Fixed in PR #1515 (branch `fix/twt-bounds-explicit-output-budget`). Refactor #1483 moved model construction into `model_router.build_chat_model` and dropped the explicit output-token caps (`min(2000, ceiling)` in `triage_fanout.py`, `min(4000, ceiling)` in `resolve_conflicts.py`). The test also patched a dead `ChatOpenAI = None` placeholder. No S5 reproduction is needed. The S5 smoke tests above still cover the remaining root-suite errors that need docker and openssl: `test_certbot_deploy_hook`, `test_durability` (4) and `test_browser_relay::test_compose_browser_relay`.

## Next: core loop hardening

- Keep each Want area (#1 to #7, #12) covered by passing tests on S5.
- Resolve the open decisions in PRODUCT.md (NTFY UI, `/api/agents*` shape).

## Cleanup (gated on PR review)

`chrome-extension/` and `gdocs-access/` stay in `main`.

Done, on the user's approval: 71 remote branches whose PRs were merged with an identical branch tip were deleted. All are recoverable from the merged PRs' `refs/pull/<n>/head` and from `archive/pre-cleanup` at `7f3a2cc`.

Held for a decision (11 branches); none are pruned until the exact diff and file list is reviewed in a PR:

- `fix/timeline-failure-details-1453`
- `single-canonical-repo-path-2090810062843671711`
- `fix/preaudit-gate-1403`
- `docs/branch-protection-config-2956332781543753827`
- `fix/report-dialog-compact`
- `fix/review-issue-selection-232030301160202618`
- `tap-to-fire-jobs-actions-12378469273495919378`
- `feat/localform-ai-chrome-extension-untracked`
- `s5-heal-rescue`
- `docs/operator-notes-batch-d`
- `skill/gemini-repo-ask`

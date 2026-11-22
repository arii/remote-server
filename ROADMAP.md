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

- Backend: `PYTHONPATH=./deep-agent python3 -m unittest discover -s backend/tests` (sandbox baseline: 1633 pass, 4 skipped at #1521).
- Frontend: `pnpm --filter frontend test` (1380 pass).
- Extensions: `pnpm run test:extension` (buttons-pair 31, chrome-extension 65 pass).
- Static checks: `npm run compile-check && npm run lint:python`.
- Root suite: `PYTHONPATH=./deep-agent python3 -m unittest discover -s tests`. The sandbox run had 1552 tests, 6 docker-only errors and 28 skipped. Tests that need openssl or docker (`test_certbot_deploy_hook`, `test_durability`, `test_browser_relay`) cannot run there. Confirm these pass on S5.

### `test_twt_bounds` (fixed)

Fixed in PR #1515. Refactor #1483 moved model construction into `model_router.build_chat_model` and dropped the explicit output-token caps (`min(2000, ceiling)` in `triage_fanout.py`, `min(4000, ceiling)` in `resolve_conflicts.py`). The test also patched a dead `ChatOpenAI = None` placeholder. No S5 reproduction is needed. The S5 smoke tests above still cover the remaining root-suite errors that need docker and openssl: `test_certbot_deploy_hook`, `test_durability` (4) and `test_browser_relay::test_compose_browser_relay`.

## Next: core loop hardening

- Keep each Want area (#1 to #7, #12) covered by passing tests on S5.
- Resolve the open decisions in PRODUCT.md (NTFY UI, `/api/agents*` shape).

## Cleanup (gated on PR review)

`chrome-extension/` and `gdocs-access/` stay in `main`.

Done, by owner approval, deleted from origin: 71 branches whose PRs were merged with a tip equal to the merged PR head; 5 closed-unmerged branches (#803, #1420, #1074, #365, #367, each closed with a written reason as a duplicate or supersession); and `skill/gemini-repo-ask`, `fix/timeline-failure-details-1453` and `single-canonical-repo-path-2090810062843671711`. All are recoverable from `refs/pull/<n>/head` and from `archive/pre-cleanup` at `7f3a2cc`.

Merged: #1515 (twt caps fix), #1516 (dead docs removed), #1519 (cleanup, guardrails, partial fixes for #1508, #1487, #1490, fixes #1509 and #1517), #1520 (ruff format), #1521 (`model_tier`).

Six merged work branches are still on origin and wait for the repo owner to decide on deletion: `chore/cleanup-and-open-issues`, `chore/remove-dead-docs`, `docs/salvage-ideas-roadmap`, `fix/coder-honor-model-tier`, `fix/twt-bounds-explicit-output-budget`, `style/ruff-format-1515-files`.

Held for a decision (3 branches); none are pruned until the exact diff and file list is reviewed in a PR:

- `s5-heal-rescue`: one Nov 16 commit adding modifier-key handling in `backend/api/routers/browser_view.py`. `main` has similar `_MODIFIER_MASK` code, so it is probably superseded (unconfirmed).
- `docs/operator-notes-batch-d`: a 10-line Batch D note appended to `docs/operator-comments-2026-11-06.md`, which #1516 deleted from `main`.
- `feat/localform-ai-chrome-extension-untracked`: 7 commits, Nov 10 to 13. The extension landed as #1277 and #1315, but this tip differs in `sidepanel.js` and has a 3,301-line prototype, `chrome-extension/localform_ai_7_phase_chrome_extension_suite_builder.tsx`. The owner keeps `chrome-extension/`.

### Open items

- [#1508](https://github.com/arii/remote-server/issues/1508): backend imports five deep-agent modules at load, so booting needs the Dockerfile `PYTHONPATH`.
- [#1487](https://github.com/arii/remote-server/issues/1487): failing cobrowser schedules live in the S5 data volume.
- [#1490](https://github.com/arii/remote-server/issues/1490): the blank terminal needs S5 host diagnosis. The Browser layout was fixed in #1499.
- [#1491](https://github.com/arii/remote-server/issues/1491): batch issue; remaining findings need a pass against the deployed console.
- [#1514](https://github.com/arii/remote-server/issues/1514): docker tailscale TUN conflict. The code part is that container restarts do not appear in History or Attention.
- [#1334](https://github.com/arii/remote-server/issues/1334): buttons-pair; no failing symptom is recorded yet.

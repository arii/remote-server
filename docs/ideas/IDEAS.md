# Ideas parked outside `main`

Items marked **Maybe** during the salvage triage. They are not on the roadmap. Each one needs a decision before it returns to `main` or moves out.

Archive reference: tag `pre-cleanup-2026-11-21`, branch [`archive/pre-cleanup`](https://github.com/arii/remote-server/tree/archive/pre-cleanup), commit [`7f3a2cc`](https://github.com/arii/remote-server/commit/7f3a2cc9db42156a9549145839ff99819cacce78).

Nothing here has been deleted. Any removal from `main` happens only after the exact file list is reviewed in a PR.

## 8. Shared Chromium, browser relay and `buttons-pair/`

- **What:** a shared Chromium service and browser relay (see `docker-compose.yml`), plus `buttons-pair/` for pairing your own Chrome.
- **Open question:** does browser driving belong in `remote-server` or in a dedicated browser-agent repo?
- **Until decided:** keep in place. Archived at [`archive/pre-cleanup`](https://github.com/arii/remote-server/tree/archive/pre-cleanup) ([`7f3a2cc`](https://github.com/arii/remote-server/commit/7f3a2cc9db42156a9549145839ff99819cacce78)).
- **Note:** the relay tests (`test_browser_relay`) need openssl and docker, so they only run on a full host.

## 11. Multi-agent lane orchestration (`.agents/skills`)

- **What:** `lane-coder`, `lane-orchestrator`, `swarm`, `talker-*`, `td-tools`, `audit-*`, `independent-tests`.
- **Extraction boundary:** reusable, cross-repo skills move to the private `arii/skills` repository. `remote-server` keeps only repo-specific hooks.
- **Archived at:** [`archive/pre-cleanup`](https://github.com/arii/remote-server/tree/archive/pre-cleanup) ([`7f3a2cc`](https://github.com/arii/remote-server/commit/7f3a2cc9db42156a9549145839ff99819cacce78)).
- **Next step:** list which skills are reusable and which are repo-specific.

## 13. Skills provisioning and lockfile

- **What:** `skills-lock.json`, `deep-agent/skills-lock.json`, `scripts/provision-skills.sh`, `scripts/verify-skills.sh`.
- **Open question:** these are useful once `arii/skills` is the single source of truth. Until then they duplicate a skill registry inside `remote-server`.
- **Extraction boundary:** the registry and lockfile follow the skills into `arii/skills`. `remote-server` would only consume it.
- **Archived at:** [`archive/pre-cleanup`](https://github.com/arii/remote-server/tree/archive/pre-cleanup) ([`7f3a2cc`](https://github.com/arii/remote-server/commit/7f3a2cc9db42156a9549145839ff99819cacce78)).
- **Depends on:** idea 11.

## Marked "Don't want" (for the record)

These are not ideas to revisit. They are candidates for removal, subject to PR review.

- **9. LocalForm AI form export/import extension (`chrome-extension/`):** a standalone, on-device Chrome extension unrelated to S5 host operations. Extract it to its own repository or archive it.
- **10. Google Docs access spike (`gdocs-access/`):** an unfinished spike with a hardcoded personal doc ID. Archive it and remove it from `main`.

# S5 Control Panel Rewrite — PRD

Status: draft for approval. Owner: south-sagitta (Vite/backend routes); agent execution: blush-alnitak (builds against this contract).

## 1. Problem

The `/buttons` control panel (8,958-line `buttons/server.py`, six tabs + drawers) is unmaintainable and unusable on a phone: no "needs me now" surface, destructive actions one tap away with no confirm, raw git stderr as UI copy, ~30 unactionable FAILED rows in the ledger, per-tab polling, dead controls. Independent critique scored it **11/40 (Critical)** with 7/8 cognitive-load failures. Full findings: `.impeccable/critique/2026-11-02T20-31-44Z__dev-onasafari-ddns-net-buttons.md`.

## 2. Goals / Non-goals

Goals:

- G1. One console answers "does anything need me?" in seconds, on phone or laptop.
- G2. Every human-meaningful capability of the old page is restored (parity-mapped, §4) — nothing silently dropped.
- G3. Machine work runs automatically with zero UI and is observable in History.
- G4. Destructive actions are guarded (hold-to-confirm + server-side confirm protocol).
- G5. Cut over with tested rollback; delete the legacy template UI; one shipping UI.

Non-goals:

- N1. No backwards compatibility, legacy preservation, or compat shims.
- N2. No light theme (night ops; dark only).
- N3. No Jules UI (service dead; sessions cancelled). No Pi session browser (deep-agent APIs replace it).
- N4. No free-text command execution; fixed actions only.

## 3. Users

Ari, sole operator. Phone at night (triage, approvals); laptop for review sessions. No other audiences.

## 4. Information architecture

Five views, one job each. Global header: product title, repo picker (sticky, restated per view), status, Terminal (`/`) + Code (`/code*`) tool links. Picker shows per-repo info (branch, clean state, container health).

| View | Contents | Backend (all exist unless noted) |
|---|---|---|
| Now | Attention queue: failing runs, failed jobs/webhooks, approvals — one primary action each; calm all-clear empty state; single Report-bug | `GET /api/attention` (shipped #226); `POST /api/create-issue` |
| Review | PR list → details (file stats, AI prompts) → merge/update via hold-to-confirm; issues list/assign/create/draft | M4 routers (shipped #224) |
| Jobs | Approved-job cards + dispatch + live progress tail; repair worker dispatch + progress; agent command runner (approved td/agent commands + known audits, single + multi-repo, output tail); Stack section: per-container status + redeploy / recreate terminal+VS Code / merge-approved-PRs (all confirmed, with dry-run text) | jobs router + `POST /api/repair/dispatch` (shipped); Stack + agent-run + multi-repo-audit endpoints: **to add** |
| Chat | Run list + active agent prompt + SSE token stream + abort + approval card + ask/suggest + history | M5 chat router (shipped #222); runs endpoints + approval/resume (shipped, Pi sessions retired) |
| History | Unified jobs + ledger (plain language, incident collapse, recovery links only on failures), Kind filter, system observability (stack health, auto-run traces) | jobs/ledger/system/workflow-health routers (shipped) |

Machine work (zero UI, scheduled/evented, visible in History): briefing refresh, warm-context (both shipped #227), preflight triggers (editor/API-only), digest sends, refresh polling (one shared store).

## 5. Functional requirements

- F1. Repo picker lists all discovered checkouts (`GET /api/repos`, **to land**); selection drives every view and survives tab switches; each action group restates its repo.
- F2. Now queue items carry exactly one primary action; destructive actions render `TapToFire` (tap, then inline confirm with consequence text) and POST `{intent, confirmed: true}`; server rejects anything else with 400 (shipped #226, migrated #508).
- F3. Identical failing runs collapse to one `×N` row (shipped #233); repeat ledger failures collapse to incident rows; chat lifecycle chatter collapses to activity rows; recovery text appears only on failures.
- F4. History rows show real timestamps (ledger `ts`), newest first; raw stderr is never rendered.
- F5. Report-bug exists exactly once (Now header), files a GitHub issue via `POST /api/create-issue` with context auto-attached (repo, view, recent errors).
- F8. Multi-repo audit runs across all discovered checkouts from Jobs; output tail live, full report in History.
- F6. Empty states answer "do I need to do anything?" (all-clear), errors name the problem + recovery; loading states resolve or error visibly (no eternal spinners).
- F7. Missing agent/tool capabilities are mocked behind injectable seams and tracked in wishlist issue arii/remote-server#225; no workarounds in product code.

## 6. API contract (owner: Vite side; agent team builds against it)

Committed shapes: `GET /api/attention?repo=` → `{updated_at, items[]}` (epoch floats; severity `crit|warn|info`); destructive POSTs require `{intent, confirmed: true}`; `GET /api/repos` → `{repos: [{name, full, branch, clean}]}`; M4/M5 shapes as implemented. `/api/agents/runs` → `{runs: [{id, script, model, thread, ok, duration_s, tools, tokens, preview}]}`; `/api/agents/runs/{id}` → `{id, entry, trace}`; Pi-shaped `/api/agents/sessions` retired.

## 7. UX / design requirements

- Dark ops console evolving the buttons token world (`--bg-app #0b1120`, `--bg-card #131d31`, `--border #24344d`, `--primary #3b82f6`, red/green/amber statuses, system stack). One h1 per view; human meta ("Failing run · Critical"); severity accents, no API-URL links (in-app navigation only).
- Responsive: 390px phone + desktop; touch targets, visible focus, no color-only status, themed selection/scrollbars/focus rings.
- Anti-slop: no gradients-as-decoration, glass, emoji icons, purple/indigo, bento-everything, kickers; fallow gate + kill-ai-slop audit must pass per PR.
- Bar: re-run critique must clear 11/40 into Good (14+) before cutover.

## 8. Cutover, rollback, deslop

- Rollback first: pin current buttons image, drill down/up/verify, then forward cut.
- Cut: Caddy `/buttons*` → new stack (strip_prefix + image swap); phone + webhook smoke post-cut.
- Deslop after green: delete legacy template UI from `buttons/server.py`, dead scripts/fixtures, killed surfaces; reconcile `main` ↔ `muse-set-up` into one line.

## 9. Milestones

- M0 (done): backend routers M1–M5, attention API, automation, Now+History union — all on `main`.
- M1: repos API + Review + Jobs/Chat views landed; nav complete; :5173 fully live.
- M2: design pass to §7; critique re-run clears Good; Stack section + shared store.
- M3: cutover + rollback drill + deslop + branch reconcile. Done.

## 10. Open questions

- O1. NTFY UI: return when configured, or kill permanently? (Default: kill UI, keep backend.)
- O2. Light theme ever? (Default: no.)
- O3. Blush merge sequencing for release PR #236 (muse-set-up → main, conflict-free, CI running) — merge before or after M1?

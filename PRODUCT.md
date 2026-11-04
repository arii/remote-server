# Product

<!-- impeccable:product-schema 1 -->

## Platform

web (phone-first + laptop; same responsive SPA, no native wrapper)

## Users

Ari, the single operator. Phone at night for triage and approvals; laptop for longer review sessions. No other audiences.

## Product Purpose

Operate the S5 home-server stack from one console: see what needs human input, act on it, and observe everything else. Success = open the app and know within seconds whether anything needs you; act with minimal taps; never wonder what the automation did.

## Positioning

A human-gated operate console: only decisions requiring a human are surfaced; all machine work (briefing, warm-context, preflight, digests, refreshes) runs automatically and is observable in History. Fixed actions only — no free-text commands.

## Capabilities

- Now: attention queue (failing runs, failed jobs/webhooks, approvals), calm all-clear state, single Report-bug.
- Review: PR pick/review/merge/update + issues list/assign/create/draft.
- Jobs: approved-job dispatch + live progress + repair worker + stack actions (deploy/recreate/merge-approved), all gated.
- Chat: agent chat stream + abort + ask/suggest.
- History: unified jobs + ledger + system observability (health, auto-run traces).
- Global repo picker driving every view; destructive actions require hold-to-confirm + server-side `{intent, confirmed: true}`.

## Constraints

- No backwards compatibility, no legacy preservation, no compat shims; delete, don't deprecate.
- No new infrastructure for automation (reuse webhook worker / existing scheduling).
- No mock, fixture, or stub may leak into live paths; missing agent capabilities are mocked behind seams + tracked in wishlist issue arii/remote-server#225.
- External-agent session UI is out (service dead; sessions cancelled, #102); Pi session browser is out (deep-agent APIs replace it).
- Preflight is editor/API-only; briefing/warm-context have zero human UI.

## Terminology

Now (attention queue), Review, Jobs, Chat, History; holding-confirm = press-hold to arm, tap to fire; incident = collapsed repeat failures; routine = chat lifecycle chatter.

## Evidence

- Critique baseline: `.impeccable/critique/2026-11-02T20-31-44Z__dev-onasafari-ddns-net-buttons.md` (11/40; re-run must clear into Good).
- API contract: defined by the Vite-side owner in PR descriptions + router code; agent execution builds against it.

## Accessibility

Phone touch targets, visible focus, no color-only status, reduced-motion path for hold-to-confirm, keyboard-operable triage flow.

## Open Decisions

- NTFY UI: return when configured, or kill permanently (default: kill UI, keep backend).
- `/api/agents*` runs-based shape: owner defines next; deep-agent team builds against it.

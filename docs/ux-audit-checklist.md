# UX Audit Checklist (loop artifact)

How to run: open the dev build in paired Chrome, walk every view live (not mock), answer each item Pass/Fail with one line of evidence. Fix failures, re-run. Source of truth for "done": PRD.md + /buttons parity.

## A. Feature parity (buttons → SPA)

- [ ] A1. Repo picker lists all checkouts with branch + clean state; selection drives every view.
- [ ] A2. Briefing content renders (attention, PRs, reviews, board, conflicts, failing runs) with errors shown, not blank.
- [ ] A3. PR list → details (file stats, prompts) → merge/update-branch works with hold-to-confirm.
- [ ] A4. Issues list/assign/create/draft works.
- [ ] A5. Approved jobs dispatch + live progress tail works.
- [ ] A6. Repair worker: issue input + dispatch + status/progress works.
- [ ] A7. Agent commands + known audits runnable (single + multi-repo) with output.
- [ ] A8. Stack: per-container status + redeploy / recreate / merge-approved, all confirmed.
- [ ] A9. Chat: session list + prompt + live token stream + abort works.
- [ ] A10. History: jobs + ledger unified, plain language, incident collapse, recovery links only on failures, real timestamps.
- [ ] A11. Observability: stack health + auto-run traces visible.
- [ ] A12. Terminal + Code tool links present in header.
- [ ] A13. Report-bug exists exactly once and files with context (repo, view, recent errors).
- [ ] A14. Machine work (briefing refresh, warm-context, preflight, digests) has zero UI and is visible in History.

## B. Usability (can Ari get work done faster?)

- [ ] B1. Cold open answers "does anything need me?" in under 60 seconds.
- [ ] B2. Triage-to-action: any Now item reaches its fix/merge/approve in ≤2 taps from the queue.
- [ ] B3. Every view has exactly one primary action; no walls of equal-weight buttons.
- [ ] B4. Destructive actions need hold-to-confirm AND fail closed server-side without `{intent, confirmed: true}`.
- [ ] B5. Loading states always resolve (data, empty, or named error) — no eternal spinners.
- [ ] B6. Empty states are calm and answer "anything to do?" (no blank pages).
- [ ] B7. Errors name the problem + recovery; raw stderr/JSON never rendered as UI copy.
- [ ] B8. Phone 390px: no horizontal scroll, tap targets usable, drawers don't occlude content.
- [ ] B9. Zero console errors on every view, live backend.
- [ ] B10. No mock/fixture content leaks into live paths.

## C. Vision alignment (remote-server)

- [ ] C1. Only human-gated decisions are surfaced; everything else automated.
- [ ] C2. Fixed actions only — no free-text command execution.
- [ ] C3. Dark ops console look; one h1 per view; human meta ("Failing run · Critical"); no API-URL links.
- [ ] C4. No Jules UI, no Pi browser, no dead controls, no backcompat shims.
- [ ] C5. Missing agent capabilities mocked behind seams + tracked in wishlist #225.

## D. Loop record

| Date | Loop | Pass / Total | Notes |
|---|---|---|---|
| 2026-11-02 | 1 | _/38 | first run vs main + ux3-parity WIP |

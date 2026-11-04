# Autonomous Multi-Repo Triage Runbook (RETIRED)

**Status: retired Nov 2026 (#102).** The 20-minute triage loop this
runbook specified ran on an agent backend that has since shut down:
session inspection, label dispatch, and the `sync-agents` /
`nudge-agents` paths no longer exist. The protocol below was
removed rather than left to rot; git history preserves the last
full version (`git log -- docs/autonomous-triage-runbook.md`).

What to use instead:

- Execution backend: the repair worker — `POST
  /api/repair/dispatch` with `{"issue": N, "repo": R,
  "intent": "repair-dispatch", "confirmed": true}` (#103).
  Dispatch from the Jobs view; poll `GET /api/repair/status`.
- Repo state: `GET /api/briefing?repo=` for the needs-attention
  list, PR rows, CI health, and conflicts.
- Merges: `gh pr merge` after the CI rollup is green (no
  auto-merge daemon remains on this stack).

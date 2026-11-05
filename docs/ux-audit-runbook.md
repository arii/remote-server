# UX Audit Agent Runbook (dispatched, impeccable method, zero Gemini)

One-tap whole-site UX audits from the buttons console: pick a site,
the agent crawls pages (sitemap-seeded + link-following within
scope), applies the impeccable rubric (Nielsen heuristics +
a11y/perf/theming/responsive/integrity), files ONE comprehensive
findings issue to the site's repo; findings convert to
agent-assigned tasks one tap per group; fixes flow through the
existing Review/merge pane.

Audit-only: audits perform HTTP GETs and file an issue. They never
modify the audited sites.

## Sites

| Console site     | Start URL              | Findings repo      |
|------------------|------------------------|--------------------|
| arii.github.io   | https://arii.github.io/ | arii/portfolio     |
| boomtick.blog    | https://boomtick.blog/  | arii/tech-dancer   |

The registry lives in `deep-agent/ux_audit.py` (`SITES`); the jobs
router and the console read it through that module. Findings file
to the site *sources*: `arii.github.io` is build output deployed
from `arii/portfolio`, and `boomtick.blog` is served from
`arii/tech-dancer` (#886).

## The flow (console)

1. Jobs view → UX audit → pick a site, set the page cap, Run UX
   audit. Live output streams; the tail ends in
   `AUDIT_DONE <repo>#<num> <url>`.
2. Paste the issue number into Findings issue, then Assign group
   per group — one repair-worker session each, scoped to the
   site's repo with the group task spec as focus.
3. Fixes land as PRs; review + merge in the Review pane as usual
   (merge-block reasons enforced there; gaps ride #848, not here).
4. Re-run the audit to watch the scores improve.

## The flow (CLI)

From the repo root (`python3` + `deep-agent/` on the path; `--file`
needs `gh` authed for the site repos):

```sh
# Registry + dry crawl (no filing).
python3 deep-agent/ux_audit.py sites
python3 deep-agent/ux_audit.py audit arii.github.io --max-pages 10 \
  --state-dir /tmp/ux-audit --out /tmp/ux-audit/report.json

# Full one-tap equivalent: audit + file ONE issue.
python3 deep-agent/ux_audit.py audit boomtick.blog --file \
  --state-dir /tmp/ux-audit

# Assign payloads from the latest report.
python3 deep-agent/ux_audit.py groups boomtick.blog \
  --state-dir /tmp/ux-audit

# Model-refined rerun (venv + Ollama/Nebius; deterministic
# findings stay, the model only adds + re-scores n/a heuristics).
deep-agent/.venv/bin/python deep-agent/ux_audit.py audit arii.github.io \
  --model --file
```

Reports persist to `$BUTTONS_UX_AUDIT_DIR/<site>.json`
(`/var/lib/buttons/ux-audit/` on the console volume); the groups
endpoint serves the latest report per site.

## What the static pass can and cannot see

Deterministic checks run on fetched HTML (no browser): missing
titles/alt/labels/lang/viewport/landmarks, heading structure,
empty accessible names, placeholder links, duplicate ids, inline-
style density, render-blocking resource counts, dead in-scope
links, nav-structure consistency across pages.

Honest caps (each score carries its basis): H1/H8/H9 score n/a
statically (feedback, aesthetics, and recovery need a running
page); theming caps at 2 (tokens need a browser); responsive caps
at 3 (touch targets need layout). The `--model` pass refines with
the local impeccable skill text as its rubric
(`.agents/skills/impeccable/reference/` under the checkout root,
`UX_AUDIT_SKILL_ROOT` override, embedded compact rubric when
absent); it never removes deterministic findings.

Crawl politeness: sequential GETs, 25-page default cap (200 max),
asset + sitemap/feed/manifest extensions excluded (auditing a
sitemap as HTML would file false positives), dead-link probes
capped at the page cap. First runs go from the terminal container
(working DNS).

## Assign prerequisites

- The findings issue carries Target Files (URL-derived guesses —
  confirm in the checkout) + Verification Command sections, so it
  parses under `backend.parse_task_spec` and passes the repair
  verify gate.
- Verification is `python3 -m unittest` (tree-smoke placeholder:
  static site repos ship no pytest/vitest suite; unittest with
  zero tests exits 0). Tradeoff, documented here: the gate stays
  meaningful (allowlisted verb, runs in the worktree) without
  inventing a suite the repo lacks.
- The repair worker needs a checkout/worktree path for the site
  repo (`arii.github.io`, `boomtick` under the repos root, or a
  fresh clone); without one the assign session fails fast with
  the worker's own error, never silently.
- One tap = one group = one worker session (`--body` focus
  override); the worker fixes that group only.

## Verification

- `python3 -m unittest discover -s tests -p 'test_ux_audit.py'`
  (crawl/checks/scores/groups/issue-render hermetic suite).
- `python3 -m unittest discover -s backend/tests` (engine steps,
  dispatch + groups endpoints, focused dispatch).
- `pnpm --filter frontend test` + `tsc` (audit section, helpers).

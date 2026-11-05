# Visual Impact Pass Runbook (deep agents, zero Gemini)

Run a UI impact pass on PR X: capture base, capture head, diff,
question, verdict. Capture reuses `td ux` (Playwright stays there);
questioning runs on local models (Ollama first, Nebius fallback).
No Gemini key is needed at any step.

## Prereqs

- Target checkout carries `scripts/ux-*.ts` plus
  `scripts/config/ux-audit.config.json` (baseUrl + viewports).
  `td ux` shells to those scripts; without them capture fails fast
  with `CAPTURE_ERROR`.
- The baseUrl app is serving (local dev server or deployed env).
- Ollama reachable (`curl -s http://ollama:11434/api/tags`), or
  `NEBIUS_API_KEY` set for the fallback. Verify Gemini stays out:
  `env -u GEMINI_API_KEY` around the pass.

## The pass (CLI)

From the repo root (`python3` + `deep-agent/` on the path; the
`ask` step needs `deep-agent/.venv`):

```sh
# 1. Capture base (on the base ref).
python3 deep-agent/impact.py capture /srv/repos/<name> --route / --desktop
python3 deep-agent/impact.py snapshot /srv/repos/<name> base

# 2. Deploy / check out the head, capture again.
python3 deep-agent/impact.py capture /srv/repos/<name> --route / --desktop
python3 deep-agent/impact.py snapshot /srv/repos/<name> head

# 3. Diff: severity-ranked findings + evidence refs.
python3 deep-agent/impact.py diff \
  /srv/repos/<name>/artifacts/ux-impact/base \
  /srv/repos/<name>/artifacts/ux-impact/head > /tmp/impact.json

# 4. Question the diff (local model, cropped regions only).
deep-agent/.venv/bin/python deep-agent/impact.py ask \
  "what visually changed between base and head?" /tmp/impact.json

# 5. Lighthouse side-read (when the capture ran it).
python3 deep-agent/impact.py lighthouse \
  /srv/repos/<name>/artifacts/ux-impact/head
```

## The pass (agents)

- Chat (`deep_chat`, impact tools on): "capture /hrm on desktop,
  snapshot it as base, then diff base against the current capture
  and tell me what visually changed."
- Ask (`ask_investigator`): same tool names — `run_ux_capture`,
  `diff_versions`, `ask_diff`, `lighthouse_read`.

Snapshot dirs live at `artifacts/ux-impact/<label>` inside the
target checkout; `diff_versions`/`ask_diff` also accept tmp dirs.

## Reading the verdict

Findings are severity-ranked (`critical/high/medium/low/info`):

| Severity | Meaning |
|---|---|
| high | New layout overflow, new serious axe violation, big lighthouse drop (>= 0.25) |
| medium | New image without alt, new small tap target, missing route, lighthouse drop (>= 0.10) |
| low | Screenshot-region change (needs `ask_diff` judgement), oversized image, above-the-fold shift |
| info | Route captured only on one side, unreadable screenshot |

`ask_diff` ships the ranked findings plus downscaled crops of the
changed regions (never full-res dumps) to the model turn. Local
qwen3 is text-only, so pixel verdicts ride the text evidence;
image parts are there for vision-capable models (e.g. a Nebius
vision model via `NEBIUS_MODEL`).

## Findings shape (coordinate with #845)

`diff_versions` returns `{"findings": [...], "summary": {...},
"compared": n, "added": [...], "removed": [...]}` with findings of
`{"severity", "title", "route", "viewport", "evidence"}`. The #845
`backend/review.py` synthesis owns posting-shaped output; this tool
returns data, consumers post. Keep the shapes aligned, don't merge
them.

## Scheduled passes (`impact-pass`, #869)

The engine's non-destructive `impact-pass` action fires the same
capture/diff stack on a schedule — no API change, the generic
run/schedule surfaces carry it:

```sh
# Opt in: weekly console self-regression (default routes: / desktop+mobile).
curl -s -X POST localhost:8000/api/jobs/schedule \
  -H 'content-type: application/json' \
  -d '{"action": "impact-pass", "cadence": "weekly"}'
```

- Snapshots live at `/var/lib/buttons/impact-pass/<route>-<viewport>/`
  (absolute paths, restart-safe; `IMPACT_PASS_STATE_DIR` overrides).
- Rolling baselines: the first fire seeds baselines without
  findings; each later fire diffs against the last snapshot, then
  the fresh capture becomes the next baseline. No-change fires are
  silent (no ledger rows, no notify).
- New findings ledger as `kind: ux-finding` with severity, and the
  count lands in the run output (`impact-pass routes=2 findings=3`).
- Findings are data, not failures: the run exits 0 when findings
  land. Capture errors (target down, `td ux` broken) exit 1 so the
  run fails and the `job_fail` push fires.
- More routes = config, not code: `IMPACT_PASS_ROUTES` takes
  `route:viewport` pairs (`/:desktop,/:mobile,/jobs:desktop`).
  Keep the list small — the step watchdog is 1200s and each
  capture can take up to 600s.
- Capture target: `scripts/config/ux-audit.config.json` points at
  the deployed console (Caddy `/buttons`); `UX_BASE_URL` overrides
  it per fire (dev: the vite server). The checkout's `scripts/ux-*`
  + root tsx/playwright deps are the `td ux` contract (#880); CI
  guards it (`tests/test_ux_scripts_contract.py`).
- Where it runs (#881): the step executes INSIDE the buttons image
  (local `-m`, same snapshot + ledger state the API serves), and
  the image bakes the toolchain (node 22, pnpm, root ux deps,
  chromium). `python -m backend.impact_pass --check` reports the
  probe without capturing; the pass itself preflights it and fails
  with a named missing dep instead of a cryptic td error. The
  image build runs `--check` as a smoke, so a regressed dep fails
  the build, not a scheduled fire.

## Troubleshooting

- `CAPTURE_ERROR: ... ux-discover-routes ...` — target checkout
  lacks the ux scripts; impact passes only run where `td ux` runs.
- `capture wrote no results` — the console was not serving at the
  baseUrl (`UX_BASE_URL` wrong or target down). The run fails
  instead of seeding an empty baseline.
- `ASK_ERROR: no model available` — Ollama down and no Nebius key;
  check `OLLAMA_URL`, or set `NEBIUS_API_KEY`.
- Empty diff on a changed UI — capture twice without deploying in
  between, or the route list drifted (see `added`/`removed`).
- Screenshot-only `low` findings — expected for intentional
  restyles; `ask_diff` judges them against the PR description.

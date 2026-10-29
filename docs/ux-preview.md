# Buttons UX preview

Preview the buttons page locally and screenshot every tab at
phone + desktop widths — no S5, no rebuild, no real repos.

## One-time setup

Playwright is already installed via `~/workspace/portfolio-src`
(node_modules there); `snap.mjs` resolves it from that path and
launches the pinned Chromium with `--no-sandbox`. Nothing to
install.

## Use

```bash
# Terminal 1: start the preview server (fake repos + throwaway
# ledger, port 18099 by default)
./buttons/ux/serve.sh

# Terminal 2: screenshot all six tabs at both widths
node buttons/ux/snap.mjs
# -> /tmp/buttons-ux/phone-*.png, desktop-*.png
```

`serve.sh` takes an optional port; `snap.mjs` takes an optional
base URL and output dir:

```bash
./buttons/ux/serve.sh 18101
node buttons/ux/snap.mjs http://localhost:18101/buttons /tmp/my-shots
```

## What the preview covers

- All six tabs (Actions, Briefing, Pi agent, Project, Jobs,
  Ledger) with populated states: two fake repos, a few ledger
  entries (one failed run, so the Report-bug path is visible).
- `snap.mjs` also reports any JS page errors — treat a non-empty
  list as a failed check.

## What it does not cover

Docker-dependent endpoints (briefing, td runs, Pi sessions)
return their degraded/error states locally — that is expected
and is itself part of the audit (see issue #14). For the real
data, preview on the S5 after pull + rebuild.

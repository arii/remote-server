# Release train (issue #1081)

One rollup PR per upstream boomtick release — never a PR per commit,
per file, or per failing check.

## What the rollup pins

- `Dockerfile` + `terminal/Dockerfile`: `ARG BOOMTICK_CLI_REF` floats
  on `main` (latest-only; the per-release pin was retired).
- `.github/workflows/e2e.yml`: the `install boomtick td CLI` step pins
  the same tag.
- `skills-lock.json`: repinned via `bash scripts/verify-skills.sh
  --repin`, then verified with the CI skills-lock job.
- `deep-agent/requirements.txt`: `==` pins stay exact; LangChain on
  the 1.0 LTS line or newer with `langchain-core` pinned explicitly.
  Bumps arrive as reviewed Dependabot diffs (grouped per cycle).

## Failure flow

A failing PR yields a comment on the PR (the CI `report` job posts
and updates one marker comment). Never file a follow-up issue per
failing PR.

## After the bump

Run `td doctor` on the console and confirm green before merge. The
release is consumed when the tag above, the lockfile, and doctor all
agree.

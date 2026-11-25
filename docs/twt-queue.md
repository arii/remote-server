# Durable TwT issue queue

`scripts/queue_runner.py` supervises the existing
`python3 -m backend.shared.cli run-pipeline --submit-pr --keep-worktree` path, one issue at a
time. It requires the merged bounded-receipt contract in `twt_receipts.py` and a
TwT gateway that emits `cost_basis: upper_bound`. The supervisor uses Python's
standard library and `gh`; the worker needs the normal deep-agent dependencies,
canonical `td`, GitHub authentication and TwT access in its own runtime.

## Configuration

Copy `scripts/config/twt-queue.example.json` to an operator-owned JSON configuration
outside the checkout. Its `_comment` records why the operator holds are global.
Replace these
example absolute paths with persistent paths in your deployment:

```json
{
  "issues_file": "/absolute/remote-server/scripts/config/twt-queue-issues.json",
  "state_dir": "/absolute/queue-state",
  "runtime_state_dir": "/absolute/queue-state",
  "pipeline_checkout": "/absolute/remote-server",
  "repo_paths": {
    "arii/remote-server": "/absolute/remote-server",
    "arii/boomtick": "/absolute/boomtick"
  },
  "discover_repos": ["arii/remote-server", "arii/boomtick"],
  "runtime_command": [],
  "worker_python": "/opt/venvs/td/bin/python",
  "cap_usd": 1.0,
  "max_common_failures": 3,
  "strong_after_failures": 2,
  "inter_issue_delay_seconds": 60,
  "environment": {
    "TWT_BASE_URL": "http://127.0.0.1:8000/v1",
    "CODER_STRONG_MODEL": "route:strong"
  }
}
```

Run `python3 scripts/queue_runner.py --config /absolute/queue-config.json`, or set
`QUEUE_CONFIG` instead of passing `--config`. No runtime path is inferred from a
Devin session. `pipeline_checkout` and `repo_paths` are paths **inside the worker**;
`state_dir` is the supervisor's path and `runtime_state_dir` is the worker's path
to the **same persistent directory**. The worker source travels over stdin. The
launcher declares the configured checkout and its `deep-agent` directory in
`PYTHONPATH` before starting `worker_python`. Python's `-P` prevents the target
repository's working directory from taking precedence over those import paths.

`worker_python` is the container interpreter `/opt/venvs/td/bin/python` (the `td`
venv that both Dockerfiles build). The old `deep-agent/.venv` no longer exists, so
a config that still names it fails at launch. Prerequisite: the interpreter must be
able to import `backend.shared` (for example `backend.shared.twt_receipts`). The
launcher runs `env PYTHONPATH=<pipeline_checkout>/deep-agent:<pipeline_checkout>
<worker_python> -P - <mode> <dir>`, so the checkout root is on `PYTHONPATH` and
no working-directory setting is needed; `pipeline_checkout` must be the repo root.
Check with `<runtime_command> env PYTHONPATH=<checkout>/deep-agent:<checkout>
/opt/venvs/td/bin/python -P -c "from backend.shared import model_router; import
observe; print(model_router.__file__, observe.__file__)"`. Receipt reconciliation
remains fail-closed and completed-issue state is preserved.

For a container, `runtime_command` can be
`["docker", "exec", "-i", "CONTAINER"]`, with `worker_python` set to the
interpreter path inside that container. This is an argv prefix, **not** the old
external runner's complete Python command; omit its trailing Python/`-` when migrating.
The checkout and state directory must be mounted there. Optional `gh_command`
(default `["gh"]`) configures the supervisor's GitHub CLI. Put only non-secret
runtime settings in `environment`; provider credentials belong in the TwT service,
not manifests, job files or this configuration. Set `TWT_UDS` there if required.

The committed manifest is a triaged starting snapshot, not a live issue crawler.
Copy and review it before operating a queue. Entries require `repo` and `number`;
`group: "code-reaper"` sorts ahead of other entries, oldest `created_at` first.
Ties retain manifest order. Supply timestamps to ensure chronological ordering.
Every issue is refreshed through GitHub before dispatch; closed issues are skipped.
Explicit `skip_reason`, `merged: true`, `fixed: true`, or `requires_human: true`
also skip an entry, including fixed issues that remain open. Holds #2/#23/#26
cannot dispatch in any repo: the operator did not name a repository for them.
Human-decision #1377 is held only in `arii/remote-server`; the same number in
another repository is not held. remote-server #1359 is always skipped because
its change merged via #1381. The snapshot preserves other recorded human/operator
holds; update the manifest deliberately rather than guessing whether an issue is fixed.

### Polling for `ready` issues

At each supervisor start, for every repo in `discover_repos` the queue runs
`gh issue list --repo R --state open --label ready --json number,title,createdAt,labels --limit 100`
and appends issues missing from the manifest after all manifest entries, oldest
`createdAt` first. They then run sequentially under the usual rules. Held
(`skip_reason`), completed and closed issues are not added, and each is still
refreshed through GitHub before dispatch. The merge is in memory only: the manifest
stays the human-edited file and is never rewritten, and the queue never adds or
removes labels. Every `discover_repos` entry must also be in `repo_paths`
(startup fails otherwise). If the key is absent or empty, nothing is discovered.
If `gh` fails for a repo, a `DISCOVERY` line is logged and that repo is skipped
for this refresh. If a repo returns 100 issues, the log warns that older ones
may be missing.
### One issue only

To run exactly one issue, set `"only_issues": ["owner/repo#N"]` (exactly one entry) in the
queue config. The issue must already be in `issues_file` or found by ready discovery, with
the same `owner/repo` spelling. An unmatched key is logged as `ONLY ... nothing to run`.
The pass dispatches only that issue, which still goes through the `ready`, ledger and
budget checks. The pass then ends with no inter-issue delay. A malformed value fails at
startup, before the lock or any GitHub call. If another issue is claimed
(`status.current`), the pass halts instead of reconciling it. The stop is terminal: once the pass holds the queue lock, however it ends (done, halt or
error), it writes the `STOP` sentinel in `state_dir`, so a later start dispatches nothing.
A second start that loses the lock race writes nothing. SIGKILL or power loss skip the
sentinel; touch `STOP` by hand after an unclean exit. To run again, remove the key and delete `STOP` (or use `--clear-halt`).

## Dispatch gate

The queue dispatches an issue only if, when it is refreshed just before dispatch,
it is open and carries the `ready` label. Only Ariel adds `ready`; the queue never
adds or removes `ready` or `ledger-intake`. A listed issue without `ready` logs
`SKIP owner/repo#N not ready`. It is not marked completed, skipped or failed, and no
failure counter changes, so the next queue run checks it again and runs it once
`ready` is added. If the refresh call fails or returns unusable data, the queue fails
closed: it logs `SKIP owner/repo#N refresh failed: <reason>` and does not dispatch.
Closed issues are marked completed without dispatch.

## Accounting and recovery

- Every issue has isolated `job.json`, `run.log`, `stages.json`, `ledger.jsonl`,
  receipt journals, automatic settlement, pipeline result and `summary.json`.
- The guard refreshes existing ledger owners from fresh receipts **before**
  checking unknown spend, at model construction as well as dispatch. Live receipts
  not yet owned by a completed turn count
  toward the budget without creating premature or duplicate owner rows.
- An incomplete, valid `upper_bound` receipt reserves that amount. It counts
  against the cap and passes reconciliation without being called an actual bill.
  Missing hosted receipts and unbounded or malformed costs still halt the queue.
- The cap defaults to **$1.00 per issue** and must be positive and at most $1.00.
  Checks happen before each subsequent model call: the last dispatched call can
  cross the cap. This is a
  recorded-spend gate, not a prediction of the next call's eventual charge.
- Shutdown requires automatic settlement, exactly one owner per receipt, and
  matching ledger/receipt totals. A worker exit inconsistent with its summary halts.
- Known-cost ordinary failures continue to the next issue; three consecutive
  failures with identical status/detail stop by default (`max_common_failures`).

`status.json` persists completed/skipped issues, the current issue and halt reason.
A global file lock prevents concurrent supervisors. Restarting skips completed
issues but never blindly retries an existing unfinished issue directory. Startup
first reconciles the saved `current` issue in its configured runtime, even if it
has since closed or disappeared from the manifest. An active worker holds an
issue lock, so recovery cannot race its writes. Failed recovery halts before any
GitHub lookup or new dispatch; successful recovery records the attempt as completed
without rerunning its pipeline. An existing explicit halt still requires operator
clearance. Settlement snapshots are merged and replaced atomically. Fully reported
saved receipts can reconcile offline; provisional reservations remain eligible for
later actual charges without discarding their existing snapshots. An actual,
reconciled ledger charge supersedes a stale reservation snapshot. Aggregate actual
rows retain per-trace costs in ledger usage so that recovery can repair snapshots
offline after an interrupted replacement. Legacy aggregate rows without that
breakdown refetch receipts rather than inventing a per-trace allocation. Stopping
creates a persistent `STOP` sentinel, also checked before model dispatch. After a
halt, inspect the issue, journal/settlement and ledger using supported receipt
recovery tooling. Successful reconciliation does not clear a saved worker's
`halt_queue` or an explicit supervisor halt. After inspecting and resolving the
blocker, an operator can acknowledge the saved halt and resume with
`python3 scripts/queue_runner.py --config /absolute/queue-config.json --clear-halt`.
This first reconciles the current issue, preserves the acknowledged halt reasons,
then clears the halt and `STOP`. Failed reconciliation cannot be overridden by
this flag. Do not delete an unfinished directory to force a retry or
edit ledger costs by hand. Keep state on persistent storage, not `/tmp`.

### Check runner status

`python3 scripts/queue_runner.py --config /absolute/queue-config.json --status`
prints one state and its next action: `IDLE` (supervisor running, nothing
claimed) -> Ready; `STALLED` (claimed, `run.log` quiet past `stall_seconds`,
default 1800; a fresh claim without a log yet counts from its recorded
start) -> Investigate/Restart; `DOWN` (supervisor not holding `queue.lock`)
-> Restart Service; `BUSY` (`queue.lock` held by a non-supervisor
process such as `--retry`) -> wait for it to finish.
A `STOP` marker reports `STOPPED`, and a saved halt reports `HALTED` with its
reason -> inspect and reconcile first, then restart with `--clear-halt`, before
heartbeat and lock checks. Neither status recommends a plain restart: inspect
and reconcile the saved run before the explicit recovery procedure above. Status
checks leave both safety gates in place. Saved `stopped` and `done` flags
describe earlier supervisor exits; they do not override the marker, halt, lock,
or current activity checks.

Set `ledger_stamp_heartbeat` in the queue config to the stamp daemon's heartbeat
file (the daemon writes `~/twt/queue/ledger-stamp.heartbeat` by default). The status
check is off until the key is set. Once it is, the line also ends
with `| LEDGER-STAMP DOWN` (no heartbeat) or `| LEDGER-STAMP STALE` (no good
stamp pass for `ledger_stamp_stale_seconds`, default 1200) -> check
`twt-ledger-stamp.service`. Without fresh stamps the kiro gate reports every ready
issue as `not ready`.

### Retry a transiently blocked issue

With the queue service idle, requeue one finished, failed issue (the plain service
start below assumes no STOP marker or saved halt):

```sh
python3 scripts/queue_runner.py --config /absolute/queue-config.json --retry 'arii/remote-server#1537'
systemctl --user start twt-queue.service
```

`--retry` is a state-only operator command: it never launches a pipeline. It takes
the supervisor lock, requires exactly one eligible manifest entry and a failed
summary without a saved worker halt, and runs canonical receipt reconciliation
before moving the old run directory to `state_dir/archive/ISSUE-TIMESTAMP`.
The runtime also moves a registered `.worktrees/issue-N` into the same repo's
`.worktrees/.queue-retry/ISSUE-TIMESTAMP`, renaming its branch to
`queue-retry/ISSUE-TIMESTAMP`. Dirty and untracked files survive. The archived
run's `worktree-archive.json` links the old path, preserved branch/HEAD and new
path. Both archive operations resume from durable intent after interruption;
the retry is not eligible until both complete. No worktree is deleted.
Logs, journals, ledger and summary are preserved after reconciliation (journal ledger paths
still describe the original run location). It removes only that issue from
`completed`, records the archive in `status.json`, and prioritizes its next attempt
ahead of the normal manifest order. No manifest edit is needed. A live supervisor,
an unfinished/current run, a successful run, or unresolved receipt costs prevent
retry. Only one retry can be pending at a time.

Prior reconciled spend, including reservations, reduces the next attempt's budget;
retry does not grant a fresh allowance. Exhausted budgets cannot be retried.
Initial implementation keeps its 20-model/32-tool limit. If the TwT default
route exhausts the model limit, the coder makes at most one additional attempt
with a fresh 20-model/32-tool budget and checkpoint thread, preserving prior
edits. `CODER_FALLBACK_MODEL` selects its TwT model ID (default
`nebius/zai-org/GLM-5.3-Flash`); an empty value disables fallback. The explicit
model is sent without stage-specific provider maps, so it does not route back
through the free default waterfall. Both attempts share receipt accounting and
the remaining issue budget; TwT still enforces its configured $5 daily budget.
Repair, local-only execution and errors other than initial model-cap exhaustion
do not trigger this paid attempt.
The command does not clear STOP, explicit supervisor halts, or saved worker halts.
If shutdown left STOP, or a supervisor halt remains, explicitly acknowledge it
with the existing `--clear-halt` recovery procedure before resuming; a plain service
start stays stopped. The usual held/closed issue checks still apply. Do not remove a completed
entry or delete a run directory by hand to force a retry.

## Slop review gate

Each queue PR gets a hostile slop review (#1552) before it can reach review.
`scripts/slop_review.py` wraps `Pipeline.stage_submit` in `queue_runner.worker()`,
without editing `deep-agent/pipeline.py`. After `td agent submit-pr` opens the PR,
the wrapper diffs `origin/main...HEAD` in the worktree, runs the review with the
`.agents/skills/hostile-anti-slop-audit/SKILL.md` text read from `S5_CHECKOUT`
(`arii/skills` stays the source of truth), and always posts one PR comment
(`gh pr comment --body-file -`, capped at 3000 characters) before returning.

The model is the `audit` stage model chosen by the router (local first, no spec
override), limited to 2000 output tokens, under the same budget guard and
`NEBIUS_SPEND_CAP_USD` as the rest of the run. Each model call writes one
ledger row with script `slop_review.py`. The reply must be the strict verdict
JSON from `deep-agent/verdicts.py`. `READY` and `APPROVE` pass; `NEEDS-WORK` and
`BLOCK` fail. The review reports only and never edits the PR.

The diff is wrapped in a per-call random `<diff-TOKEN>` ... `</diff-TOKEN>` marker pair (`secrets.token_hex(16)`), and the prompt tells the model to ignore instructions inside it. A diff that contains the token fails closed with no model call.

Results appear in the PR comment and in the `status` of `pipeline-result.json`
and `summary.json`:

| Status | Cause |
| --- | --- |
| `SLOP_REVIEW_FAILED` | Failing verdict, malformed or missing model output, model or git error, comment not posted, or diff over 40000 characters |
| `SLOP_REVIEW_CONFIG` | The skill file is missing or empty |

Both fail closed: the wrapper returns a failed submit, so the pipeline result is
never `APPROVED` and the PR is not handed to review. A diff over the limit is not
partly reviewed. It fails with detail `diff too large` and makes no model call.
These statuses are set after the repair loop and do not feed model tier
escalation.

Ordering with #1578: its `claims_pr_valid` is a deterministic check of the PR
body and files that is not wired in yet. The two mechanisms share no state and
both must pass. If #1578 wires its check into the same submit wrapper, it runs
first because it is free, then the slop review.

Tests use a fake model and fake `gh` (`tests/test_slop_review.py`). A live
review of a real PR needs Ari.

## Agent Ledger rows

### Post-run S5 failure audit

The optional audit runs on the supervisor after each worker exits, including
failed or interrupted runs recovered on startup. It makes no model calls. It
reads the TwT SQLite audit tables in read-only mode and counts failed upstream
attempts by provider and HTTP status or known error reason, plus exhausted
waterfalls. It checks retained coder traces in current and archived jobs via
the configured worker runtime so root-owned traces stay in the container.
Counts are per provider attempt, exhausted request, or affected coder trace.
Only the fixed cause signatures leave the audit, not provider payloads or
individual trace contents.

On S5, dry-run from the host using the active queue's config:

```sh
python3 scripts/twt_failure_audit.py --config ~/twt/queue/config-main.json \
  --twt-db ~/twt/catalog.sqlite3
```

This prints grouped counts, seen signatures, and the causes eligible to post
today without writing to the Agent Ledger or its audit state. The first scan
includes historical causes. Before enabling filing on S5, record causes
already tracked in GitHub (one repeatable `--mark-known SIGNATURE=URL` per
signature, using the name from the dry-run). This writes only to the queue
state, not GitHub or the Form. For the initial S5 backfill:

```sh
python3 scripts/twt_failure_audit.py --config ~/twt/queue/config-main.json \
  --mark-known 'TwT nvidia HTTP 404=https://github.com/arii/boomtick/issues/1419' \
  --mark-known 'TwT cloudflare HTTP 410=https://github.com/arii/boomtick/pull/1421' \
  --mark-known 'TwT sambanova HTTP 402=https://github.com/arii/boomtick/pull/1421' \
  --mark-known 'TwT nvidia timeout=https://github.com/arii/boomtick/issues/1301' \
  --mark-known 'TwT nebius HTTP 529=https://github.com/arii/boomtick/issues/1305' \
  --mark-known 'Coder repeat loop=https://github.com/arii/remote-server/issues/1627' \
  --mark-known 'Coder read-only run=https://github.com/arii/remote-server/issues/1633'
```

The repeat-loop group also covers the repeated read in #1630. The caller-side
model configuration error in #1635 is not an upstream attempt or a coder
guard trip, so there is no signature to mark for it.

Run the dry-run again after marking known causes. On the next approved queue
start, point `QUEUE_RUNNER` at the updated checkout (with
`twt_failure_audit.py` and `ledger_post.py` alongside it) and add
`"failure_audit_db": "/home/ari/twt/catalog.sqlite3"` to the host queue config.
No separate service or TwT configuration change is needed. Omit the setting
to leave filing disabled.

For each new signature, the audit uses the existing Form writer as `S5-Local`
with status `Act` (TwT tasks go to `boomtick`, coder tasks to `remote-server`).
The Agent Ledger hook must allow that source and both repos. The supervisor
records seen signatures, their tracker URLs when known, the pending backlog
and the UTC-day post count in `failure-audit-seen.json` under `state_dir`.
Keep this file across restarts. The audit accepts an optional positive integer
`failure_audit_daily_post_cap` in the host config (default 3, below the hook's
shared cap of 5). Higher-count causes are submitted first, with ties sorted
by signature; excess causes remain pending for the next run with UTC-day
capacity. Only successful Form submissions consume the local cap.
Submissions use stable notes so the hook's replay deduplication also applies.
Other Form sources share the hook's capacity; a Form success does not confirm
issue creation, and the audit does not resubmit a row the Form already accepted.
Check the hook's outcome rows after enabling.
The audit never sets `ready`. Audit errors are logged but do not change the
queue's run status; a failed Form submission remains eligible on the next run.

### Ledger intake review

`python3 scripts/ledger_intake_review.py OWNER/REPO NUMBER` audits one new `ledger-intake` issue through local Ollama/TwT and may post one finding comment. It checks the issue body for the canonical `problem`, `scope` and `done_when` fields (the hook's `Problem`, `Out of scope` and `Done when` sections; `title` and `repo` come from the issue itself) and reports gaps by those names. It never changes labels or issue state.

### Missing routing fields (#1585)

#### Problem

Intake rows can arrive without a repo, scope or done-when. Guessing them would
route work to the wrong place.

#### Done when

- A row missing `repo`, `scope` or `done_when` is `NEEDS_INFO`, with the
  canonical names in `missing_fields`. Blank values stay blank.
- The reviewer only reports field names. Any value it returns is ignored.
- `POST /api/ledger-intake/{id}/approve` returns 400 until all five required
  fields (`title`, `repo`, `problem`, `scope`, `done_when`) are present, from
  the row or from the human's input.
- Dispatch still needs Ariel's `ready` label. Nothing retries automatically.

#### Out of scope

Any new required field. `status_for` never derives `READY` or `DISPATCHED` (#1613).

The supervisor reads the response Sheet via `scripts/ledger_read.py` and posts
claims and results as `S5-Queue` via `scripts/ledger_post.py`. The only agent
writers are this script and BoomTick's `scripts/agent_ledger.py`. Ledger rows
are data, not a source of work. Ariel dispatches Kiro herself per issue; Devin
never starts Kiro. Kiro uses the BoomTick CLI to post its own claim and result.

Key: the `Link` column is always the issue URL
(`https://github.com/<repo>/issues/<n>`), because `ledger_read.py` folds the
latest row per `Link`. The PR URL goes in the note, not in `Link`, so the
folded row for the issue stays the latest state.

| When | Status | Note |
| --- | --- | --- |
| Before dispatch | `Act` | `claimed by queue run <run-id>` |
| PR opened | `Act` | PR URL, TwT cost total with its `cost_basis`, `tests pass` |
| Failed, blocked, slop review failed | `Later` | the pipeline status code, or `max_common_failures` |
| Queue halted on the issue | `Later` | `queue_halted` |
| Spend cap reached | `Needs Okay` | `spend cap reached` |
| PR seen merged at a later run | `Done` | `merged <sha> <PR URL>` |

Before dispatching, the supervisor reads the latest row for the issue URL. A
row from another source with status `Act` newer than `ledger_stale_hours`
(config, default `6`) makes the supervisor skip the issue, log
`LEDGER skip ...`, and record it in `status.json` `ledger_skipped`. The issue is
not marked completed, so the next run checks again. Rows from `S5-Queue`, older
claims, and other statuses do not block. The supervisor reads the response
Sheet through Drive CSV export, not the hourly JSONL snapshot. It interprets
timestamps as UTC.

If the ledger cannot be read, the check fails closed: the issue is skipped and
recorded in `ledger_skipped` as `ledger unreadable (<ErrorType>), failing closed`.
The next run checks it again. The reader is standard library only, so the
service's `/usr/bin/python3` can run it. On the S5 host set `GOOGLE_TOKEN_FILE` in
`~/.config/twt-queue/environment` to the host path of
`gdocs-access/token.json`, because the default is the container path.
[PR #1619](https://github.com/arii/remote-server/pull/1619) merged and was
deployed on S5: the queue worktree was on main at `3d6c58c` with
`GOOGLE_TOKEN_FILE` set. A live claim check read 215 ledger rows and skipped a
claimed issue. This proves the live claim check, not the full claim-to-result
handoff tracked in [#1537](https://github.com/arii/remote-server/issues/1537).

Nov 23 status: `twt-queue.service` failed at 04:10 and a restart was in progress
when reported. Separately, main hit a test-only lint failure fixed by merged
[PR #1621](https://github.com/arii/remote-server/pull/1621); two root test
failures on main are tracked in [#1623](https://github.com/arii/remote-server/issues/1623).

A failed post never fails the run. The row is saved in `status.json`
`ledger_pending`, retried once at the next boundary (the start of the next
`supervise` run or the next dispatch), and dropped with a log line if that retry
fails too. Merge confirmation also happens at the start of each run: for PRs in
`status.json` `ledger_open_prs`, the supervisor runs `gh pr view` and posts
`Done` once when the PR is merged. Closed PRs are forgotten.

Notes hold only fixed text, the run id, a status code, the PR URL, and the cost
figure. They never include pipeline detail, repo content, or environment values,
because the Form is public.

Not posted: a `Needs Okay` row for login or CAPTCHA walls (the queue cannot
detect them), a pass/fail result against the issue's "done when" check, and an
issue comment per result. See [#1537](https://github.com/arii/remote-server/issues/1537)
for the remaining result requirements and live claim-to-result proof.

Root tests run behind a network guard for `docs.google.com`
([#1617](https://github.com/arii/remote-server/pull/1617)). Stub ledger reads
and writes in queue tests; never point a test at the live Form.

### Human confirm for intake rows (#1584)

Agents flag intake rows; a human decides. Duplicate detection (exact hash or fuzzy title match) and `ledger_intake_review.py` only flag. They never link, merge, reset or delete a row.

Gated actions on the Ledger triage screen, each a two-step tap and each rejected with 400 by the backend unless the body carries `confirmed: true` and the matching intent:

| Action | Endpoint | Intent |
| --- | --- | --- |
| Merge duplicate (links `canonical_intake_id`, appends notes) | `POST /api/ledger-intake/{id}/merge` | `ledger-intake-merge` |
| Not a duplicate (resets to `NEW`, never `READY`, drops the link, remembers the pair so it is not flagged again) | `POST /api/ledger-intake/{id}/distinct` | `ledger-intake-distinct` |

Merge always needs the confirm, for exact-hash and fuzzy matches alike. This overrides the one-tap Merge in #1583. The hook may still set `DUPLICATE` on an exact dedupe hash at submit, because that is a status and not a link. Those rows stay in triage until a human merges or marks them distinct, and `approve` rejects them with 400 until then. Merge and distinct both only accept the candidate the detector flags.

The intake code and routes contain no delete path. The only path that removes them is `POST /api/ledger/clear` (intent `ledger-clear`), which truncates the whole ledger including intake rows. `backend/tests/test_briefing_ledger.py` guards this in `test_ledger_intake_no_delete_path`, a tripwire on call names and direct file writes in `scripts/ledger_intake*.py` and the intake routes, not a proof. Only Ari adds `ready` to a `ledger-intake` issue.

### High-blast-radius lock (#1586)

A schema-complete intake row that matches a high-blast-radius category gets
status `NEEDS_APPROVAL` instead of `NEW`. Detection is deterministic keyword
matching (no model) over `title`, `problem`, `scope`, `done_when` and `notes`,
case-insensitive, at word starts. False positives are expected and cost one
confirm. False negatives are possible for phrasings the lists do not cover
(the row then stays `NEW` and still needs the normal human approve); extend the lists when you see one. The
keyword constants live in `backend/core/intake_risk.py`.

| Category | Keywords |
| --- | --- |
| `production_mutation` | deploy, redeploy, roll out/rollout/rollback, force push, push --force, migrate, migration, delete/drop/wipe ... prod(uction), production data, drop table/database/schema/column/index, truncate table, rm -rf, compose up, restart service(s) |
| `external_comms` | email, e-mail, tweet, post to, post a, slack, ntfy (whole word), publish, comment on, send a message, announce |
| `billing_permission` | billing, payment, spend cap, quota, permission, iam (whole word), rotate, api key, token scope, branch protection, secret, admin |

Status precedence: `DUPLICATE`, then `NEEDS_INFO`, then
`NEEDS_APPROVAL`, then `NEW`. The intake script writes only `NEW`, `NEEDS_INFO`,
`DUPLICATE` and `NEEDS_APPROVAL`, never `READY` or `DISPATCHED` (#1613). `READY` is set
only by a human triage approve. `scripts/ledger_intake.py` `build_row` (which imports `backend.core`, so it
needs the backend dependencies installed) writes
`risk_categories`. The boomtick hook still writes `NEW`, so
`GET /api/ledger-intake` re-derives the categories server side and lists
`NEW`, `NEEDS_INFO`, `NEEDS_APPROVAL` and suspected duplicates.

Nothing promotes `NEEDS_APPROVAL` automatically. A human approves it in the
Ledger triage view, which names the categories and asks inline before sending
`POST /api/ledger-intake/{id}/approve` with
`{"intent": "ledger-intake-approve-high-risk", "confirmed": true}`. Without
that confirm the route returns 400 (`confirmation required: high blast radius (...)`),
including when the risk comes from a field typed at approval time; the view then
switches that row to the inline confirm. The five required fields (`title`, `repo`,
`problem`, `scope`, `done_when`) must still be non-blank. The queue dispatches
only on the GitHub `ready` label; no intake status maps to that label, so a
`NEEDS_APPROVAL` row cannot be claimed.

## Strong-tier routing

The worker always runs with `NEBIUS_ONLY=1` and no `CODER_MODEL`, so an ordinary
attempt uses TwT model `default`, which TwT routes local-first and can burn its
whole timeout on a hard issue. A **strong** attempt instead sends the explicit
`CODER_STRONG_MODEL` to TwT from the first coder call. With the configured
`route:strong` profile, TwT tries the local strong model before eligible free
providers and Nebius last. The coder's ordinary `default` route is not used,
so the bounded fallback above (it fires only for `default`) cannot
double-escalate a strong attempt. That
fallback stays as the in-attempt safety net for ordinary attempts. The tier chosen
here is only the starting tier; once strong, pipeline repair cycles stay strong.

Starting tier is decided per attempt by the supervisor:

1. The GitHub issue has the label `model:strong` (read from the live issue
   payload at dispatch): reason `label:model:strong`. Add the label to route an
   expected-hard issue straight to strong; remove it to stop.
2. Otherwise, once the issue has `strong_after_failures` (default 2) counted
   failures, reason `auto-escalation:<count>`.
3. Otherwise `default`.

Counted failures are finished runs with a nonzero exit, no halt, no budget stop,
and result status `BLOCKED_DETERMINISTIC`, `CHANGES_REQUESTED` or `BUDGET_EXHAUSTED` (coder call cap). Not counted: `BUDGET_UNSPLITTABLE`,
`CONFIG_ERROR`, `STRONG_BUDGET_INSUFFICIENT`, `PAUSED_BY_USER`, `RUNNER_ERROR`,
worker halts, budget or STOP interruptions. A successful run clears the counter;
uncounted outcomes leave it unchanged. The counter is `status.json`
`tier_failures["owner/repo#N"]`, so it survives restarts and `--retry`.

### Deterministic task routing (`backend/shared/task_router.py`)

At dispatch the supervisor also runs the task router on the issue's title and
body (stdlib only; reads the live TwT catalog at `TWT_CATALOG_DB`, default
`~/twt/catalog.sqlite3`, honoring `TWT_DISABLED_PROVIDERS` and retired models).
It emits a decision into `job.json["routing"]`:

- `tier`: a task-based start-tier suggestion. `strong` when the issue names
  `STRONG_FILE_COUNT` (3) or more file paths (`task:many_files`) or the
  estimated prompt crosses `STRONG_INPUT_TOKENS` (`task:large_spec`). The
  supervisor uses the stronger of this and the escalation/label tier — the
  router can only *raise* a start tier, never lower it — and appends the rule
  to `tier_reason` (e.g. `default+task:many_files`).
- `picks.fast` / `picks.strong`: one concrete `provider/model` pin per tier
  chosen from catalog rows that are chat- and tool-capable, within the
  context window the task needs, on enabled providers, and affordable under
  the remaining issue cap (paid calls must fit `PAID_BUDGET_SHARE` of it;
  unrated paid models are never picked). `fast` ranks free models by context
  window; `strong` ranks every affordable model by input price descending —
  the strongest model the budget supports across the whole catalog, not one
  env-pinned endpoint.

The worker applies the picks after clearing inherited `CODER_*` env vars:
`picks.fast.model` → `CODER_MODEL`, `picks.strong.model` →
`CODER_STRONG_MODEL`, so both tiers resolve to a `provider/id` pin TwT serves
directly instead of the `default`/`route:strong` waterfalls. When the router
cannot pick (catalog unreachable, no eligible model) `routing` carries an
`error` and the run keeps the configured environment unchanged.

Every decision is logged to the run ledger as a `routing-decision` event row
(features, rules, candidate counts, picks, estimated call cost) via
`log_decision`, so picks join with traced cost and run outcomes for later
auditing and a learned router.

Standalone check: `python3 -m backend.shared.task_router --issue-file
<job-or-issue.json> --cap-usd 1.0` prints the decision JSON.

Spend: strong attempts use the same per-issue cap (`NEBIUS_SPEND_CAP_USD` is set
from the remaining issue budget) and the same `install_budget_guard`; there is
no second cap. Before the pipeline starts, the worker reads recorded spend and
refuses the attempt with `STRONG_BUDGET_INSUFFICIENT` if less than $0.05 is left.
That attempt makes no model call, does not fall back to local, and does not
count toward escalation.

A strong attempt without `CODER_STRONG_MODEL` in the config `environment` fails
before any model call with `CONFIG_ERROR` and **halts the queue** (every later
strong issue would fail the same way). Set the variable, then use `--clear-halt`.

Where to read the tier: the supervisor prints
`START owner/repo#N tier=strong reason=label:model:strong`; the worker logs
`tier=... reason=... model=...` to `run.log`, followed by the coder's
`coder via twt:<model> (tier=strong)`; `job.json` has `model_tier` and
`tier_reason`; `summary.json` has `tier`, `tier_reason`, `strong_model`; and
`status.json` `attempts["owner/repo#N"]` lists tier, reason, status and exit code
per attempt. Known gaps: the plan and resume coder paths still call
`build_model(CODER_MODEL)` without a tier, and ledger rows carry no tier tag.

## Coder call budget, splitting and resume

The per-turn coder limits (`INITIAL_MODEL_LIMIT` 20, `REPAIR_MODEL_LIMIT` 12) are not raised. Instead the pipeline fits work to the budget and bounds recovery.

**Size budget and split rule** (`deep-agent/pipeline.py`). A brief over `BRIEF_LINE_BUDGET` (60 non-blank lines) or `BRIEF_FILE_BUDGET` (4 distinct files, an unvalidated guess) is split deterministically (no model call) from its acceptance / done-when items into at most `SPLIT_MAX_PARTS` (3) sequential sub-briefs, each with its own acceptance list. Sub-briefs run in the same worktree. Each must be coder-verified and pass `pre_audit_check` before the next starts. One audit and one PR cover the whole diff, only when every sub-brief is done. An over-budget brief with fewer than 2 acceptance items ends as `BUDGET_UNSPLITTABLE` with no PR and no model call. Like exhaustion it posts a `Needs Okay` ledger row (`Needs Okay: BUDGET_UNSPLITTABLE, queue run <id>`): add explicit done-when items or split the issue by hand.

**Shared state.** The worker sets `S5_BUDGET_STATE=<job_dir>/budget.json`. Shape: `{parent:{calls_used,max_calls,exhausted,verified}, sub_briefs:[{id,title,acceptance,status,calls,continuations,verified,verification}]}`. It is written atomically after each sub-brief transition and copied into `summary.json["budget"]`. Small briefs get a one-entry record. Small briefs also get one continuation, so a capped turn resumes once before the TwT fallback (up to 20+20 calls before the fallback, still under the 60 total). The `code` entry in `stages.json` carries `calls` (parent total).

**Resume and caps.** When a coder turn hits its call limit, it continues once on the same checkpointed thread with a fresh per-turn budget (`MAX_CONTINUATIONS_PER_SUBBRIEF` = 1). Total model calls per parent task stop at `MAX_CALLS_PER_PARENT` (60 = 3 x 20, above the 21-40 bucket on purpose). The cap is checked before every new model-call turn. Continuation is never unbounded.

**Exhaustion.** The run ends `BUDGET_EXHAUSTED` (exit nonzero, no PR, worktree kept, `result["partial"]` lists done and not_done acceptance items). It is in `COUNTED_STATUSES` (#1638): it bumps `tier_failures`, so once the count reaches `strong_after_failures` the next attempt starts on the strong tier (`MAX_CALLS_PER_PARENT` stays absolute, so there is no in-run escalation). A strong attempt that also exhausts its cap does not escalate again and nothing retries by itself. `BUDGET_UNSPLITTABLE` does not count: a stronger model does not fix a brief that needs scoping. The ledger gets `Needs Okay: BUDGET_EXHAUSTED, queue run <id>` with state `Needs Okay`. The supervisor does not requeue it. Operator options:

- split the issue by hand into smaller issues with explicit done-when items;
- `--retry owner/repo#N`, which is the explicit approval and starts a fresh parent budget. The old job dir, including `budget.json`, is archived and done sub-briefs are not reused: the retry re-runs every sub-brief from the start (`budget.json` is a record, not resume input);
- add the `model:strong` label to start on the strong tier.

**Reading `--budget-report`.** `python3 scripts/queue_runner.py --config <config> --budget-report` reads every job's `summary.json["budget"]` under `state_dir` and prints, offline (runs with 0 calls are skipped), runs and verified completion rate per call bucket, calls per verified completion, and the budget-exhaustion rate. Sample from fixtures:

```
bucket  runs  verified  rate
1-5        1         1  100%
6-10       1         1  100%
11-20      1         0  0%
21-40      1         1  100%
>40        1         0  0%
runs=5 verified=3 calls_per_verified=13.7
budget_exhaustion_rate=20% (1/5)
```

## User service

Install `scripts/twt-queue.service` in `~/.config/systemd/user/`. Create a mode-0600
`~/.config/twt-queue/environment` containing:

```text
QUEUE_RUNNER=/absolute/remote-server/scripts/queue_runner.py
QUEUE_CONFIG=/absolute/queue-config.json
```

Then run `systemctl --user daemon-reload` and
`systemctl --user start twt-queue.service`. Enable it only if the reviewed manifest
should start at login. Configure user lingering separately if needed. The service
deliberately has `Restart=no`; failures require inspection. Monitor
`journalctl --user -u twt-queue.service` and the persistent issue directories.

### Ledger stamp daemon

`scripts/ledger_stamp.py` writes the `~/twt/queue/ledger-ok/<owner>_<repo>_<N>` stamps
the kiro gate needs before a `ready` issue goes through. Every 5 minutes it reads the
Agent Ledger and lists `ready` issues in each `discover_repos` entry. A stamp is
removed while another agent holds a fresh `Act` claim. If the ledger can't be read, no
stamps are written. If one repo's `gh issue list` fails, that repo is skipped for the
cycle and its stamps are left alone. No error ends the process. After each pass that
read the ledger and every repo, it touches its heartbeat file.

Install `scripts/twt-ledger-stamp.service` in `~/.config/systemd/user/` and add
`LEDGER_STAMP=/absolute/remote-server/scripts/ledger_stamp.py` to
`~/.config/twt-queue/environment`. It must sit in the same `scripts/` directory as
`QUEUE_RUNNER`. If it doesn't, the daemon refuses to start, because queue_runner's
sibling ledger scripts would not import. If `LEDGER_STAMP` is unset or wrong, the unit
fails at `ExecStartPre` instead of exiting quietly. Unlike the queue, this service has
`Restart=always`. A stamp left in place after a failed listing is bounded by the gate's
own freshness window. Optional overrides: `LEDGER_STAMP_DIR`,
`LEDGER_STAMP_STAMPS`, `LEDGER_STAMP_HEARTBEAT`, `LEDGER_STAMP_LOG`. Log lines
start with `STAMP` in `~/twt/queue/kiro-gate.log`.

## Checks

With the existing deep-agent verification environment active:

```sh
PYTHONPATH=deep-agent:tests:. python -m unittest test_queue_runner test_twt_receipts
pnpm run lint
systemd-analyze --user verify scripts/twt-queue.service
```

Tests mock GitHub, model receipts and worker processes; they do not run inference,
open PRs, access S5 or change a live ledger.

## Out-of-scope findings

Queue PRs must pass `claims_pr_valid` (a `Closes #N` reference plus at least one
test file). Out-of-scope findings are never shipped as code: submit them via
`scripts/ledger_post.py` as task-shaped rows (`format_out_of_scope_finding`
returns `{"issue", "title", "problem"}`), posted to the Agent Ledger.

### Strong routing profile prerequisite

The example above uses `CODER_STRONG_MODEL=route:strong`. It requires a TwT
gateway version with the strong profile (Boomtick PR1407). Configure stronger
provider model IDs using the gateway's `TWT_STRONG_MODELS` JSON map and an
installed local model using `TWT_OLLAMA_STRONG_MODEL`, then sync its catalog.
The profile tries local Ollama first, eligible free providers next and Nebius
last, with the existing daily budget and receipts. Keep the gateway's
`TWT_DAILY_BUDGET_USD=5` cap; this example does not change the cap or authorize
extra provider calls. Do not use this value against an older gateway that
only accepts default routes and exact model IDs. This is documentation, not
a change to a running coder or host configuration.

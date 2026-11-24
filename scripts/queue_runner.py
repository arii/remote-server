#!/usr/bin/env python3
"""Sequential, restart-safe supervisor for the existing deep-agent CLI.

Host mode reads --config. Worker mode runs in the configured Python runtime,
which may be reached through docker exec -i with this source on stdin.
No provider credentials belong in configuration or issue manifests.
"""

import argparse
import collections
import datetime
import fcntl
import functools
import importlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
import zoneinfo


ISSUE_CAP_USD = 1.0


class SafetyStop(RuntimeError):
    llm_not_dispatched = True


class StrongRefused(RuntimeError):
    """A strong-tier attempt that must not start; carries the result status."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


class BudgetStop(RuntimeError):
    llm_not_dispatched = True


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value, owner=None):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, default=str, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if owner is not None and os.geteuid() == 0:
        os.chown(temporary, *owner)
    os.replace(temporary, path)


def claims_pr_valid(pr):
    """Return (ok, reason) for a PR: must close an issue and touch a test file."""
    body = (pr.get("body") or "") if isinstance(pr, dict) else ""
    if not re.search(r"Closes\s+#\d+", body):
        return False, "PR body lacks 'Closes #<n>'"
    files = pr.get("files") or []
    if not any("test" in str(f) for f in files):
        return False, "PR diff touches no test file"
    return True, ""


def receipt_total(ids, local_ids, receipts):
    from backend.shared.twt_receipts import receipt_cost

    expected = set(ids)
    found = {}
    for row in receipts:
        if not isinstance(row, dict):
            raise SafetyStop("malformed TwT receipt")
        trace = row.get("trace_id")
        if trace not in expected or trace in found:
            raise SafetyStop("unexpected or duplicate TwT receipt")
        found[trace] = row
    total = 0.0
    for trace in expected:
        row = found.get(trace)
        if row is None and trace in local_ids:
            continue
        if row is None:
            raise SafetyStop("missing hosted TwT receipt: " + trace)
        cost = receipt_cost(row)
        if cost is None:
            raise SafetyStop("unknown or incomplete TwT cost: " + trace)
        if trace in local_ids and cost != 0:
            raise SafetyStop("local TwT receipt has paid cost: " + trace)
        total += cost
    return total


def refresh_spend(scope):
    """Refresh existing owners, reserving unowned live receipts without writing them."""
    from backend.shared import ledger
    from backend.shared import runs
    from backend.shared import twt_receipts

    unowned_cost = 0.0
    if scope is not None and scope.ids:
        ids, local = set(scope.ids), set(scope.local_ids)
        receipts = twt_receipts.fetch(scope.base_url, sorted(ids), uds=scope.uds)
        receipt_total(ids, local, receipts)
        represented = set()
        path = Path(ledger.LEDGER)
        with ledger.locked(path):
            rows = (
                [json.loads(line) for line in path.read_text().splitlines() if line]
                if path.exists()
                else []
            )
        for row in rows:
            if not twt_receipts._owns_receipts(row):
                continue
            owned = set(row["usage"]["twt_trace_ids"])
            if not owned.intersection(ids):
                continue
            if not owned.issubset(ids) or represented.intersection(owned):
                raise SafetyStop("receipt ownership overlaps rows or runs")
            represented.update(owned)
        if represented:
            missing = twt_receipts.merge_receipts(
                represented,
                {row["trace_id"]: row for row in receipts},
                scope.run_id,
                scope.started,
                local_ids=local,
            )
            if missing:
                raise SafetyStop("unsettled owned TwT receipts")
        unowned = ids - represented
        unowned_cost = receipt_total(
            unowned, local, [row for row in receipts if row["trace_id"] in unowned]
        )
    total = runs.total_metered_spend()
    if total["unknown"]:
        raise SafetyStop("unknown recorded spend before dispatch")
    return total["spent"] + unowned_cost


STRONG_LABEL = "model:strong"
STRONG_MIN_BUDGET_USD = 0.05
# Result statuses that count as a coder failure toward auto-escalation.
# Everything else (CONFIG_ERROR, STRONG_BUDGET_INSUFFICIENT, PAUSED_BY_USER,
# RUNNER_ERROR, halts, budget stops) is an operator or infrastructure event.
# BUDGET_EXHAUSTED (coder call cap hit after continuations, #1638) counts: a
# stronger model may finish within the cap. BUDGET_UNSPLITTABLE does not: it is
# a scoping problem that a stronger model would not fix.
COUNTED_STATUSES = ("BLOCKED_DETERMINISTIC", "CHANGES_REQUESTED", "BUDGET_EXHAUSTED")


def start_tier(fresh, failures, threshold):
    """Starting model tier and reason for one attempt: label, then failures."""
    labels = {
        label.get("name") if isinstance(label, dict) else label
        for label in fresh.get("labels", [])
    }
    if STRONG_LABEL in labels:
        return "strong", "label:" + STRONG_LABEL
    if failures >= threshold:
        return "strong", f"auto-escalation:{failures}"
    return "default", "default"


def counts_toward_escalation(summary):
    return (
        summary["exit_code"] != 0
        and not summary["halt_queue"]
        and not summary.get("budget_stop")
        and summary["result"].get("status") in COUNTED_STATUSES
    )


def check_strong_start(cap, fatal):
    """Refuse a strong attempt without CODER_STRONG_MODEL or enough budget."""
    strong = (os.environ.get("CODER_STRONG_MODEL") or "").strip()
    if not strong:
        detail = "tier strong requires CODER_STRONG_MODEL in the queue environment"
        fatal.append(detail)
        raise StrongRefused("CONFIG_ERROR", detail)
    try:
        remaining = cap - check_budget(None, cap)
    except BudgetStop as error:
        raise StrongRefused("STRONG_BUDGET_INSUFFICIENT", str(error)) from error
    if remaining < STRONG_MIN_BUDGET_USD:
        raise StrongRefused(
            "STRONG_BUDGET_INSUFFICIENT",
            f"${remaining:.4f} left under the ${cap:.2f} cap; "
            f"strong tier needs ${STRONG_MIN_BUDGET_USD:.2f}",
        )
    return strong


def check_budget(scope, cap):
    paid = refresh_spend(scope)
    if paid >= checked_cap(cap):
        raise BudgetStop(f"recorded spend cap reached: ${paid:.8f} / ${cap:.2f}")
    return paid


def install_budget_guard(cap, directory, fatal, stops=None):
    from backend.shared import model_router
    from backend.shared import twt_receipts

    def guard():
        if (directory.parent / "STOP").exists():
            if stops is not None:
                stops.append("stop")
            raise BudgetStop("queue stop requested before dispatch")
        if fatal:
            raise SafetyStop(fatal[0])
        try:
            check_budget(twt_receipts.current.get(), cap)
        except BudgetStop:
            if stops is not None:
                stops.append("cap")
            raise
        except Exception as error:
            fatal.append(str(error))
            raise SafetyStop(str(error)) from error

    def guarded(original):
        @functools.wraps(original)
        def call(*args, **kwargs):
            guard()
            return original(*args, **kwargs)

        return call

    model_router._remote_spec = guarded(model_router._remote_spec)
    model_router._BoundedHistory._bounded_kwargs = guarded(
        model_router._BoundedHistory._bounded_kwargs
    )


def ordered_issues(issues):
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
                issue.get("repo", ""),
            )
            or type(issue.get("number")) is not int
            or issue["number"] <= 0
        ):
            raise ValueError("issues need an owner/repo and a positive integer number")
    return sorted(
        issues,
        key=lambda issue: (
            issue.get("group") != "code-reaper",
            issue.get("created_at", ""),
        ),
    )


DISCOVERY_LIMIT = 100


def discover_ready(config, issues, completed):
    """Return open `ready` issues from `discover_repos` that the manifest lacks.

    Read-only: lists with `gh`, never edits labels or the manifest. A repo whose
    listing fails is logged and skipped; nothing from it is dispatched.
    """
    known = {(issue["repo"], issue["number"]) for issue in issues}
    found = []
    for repo in config.get("discover_repos", []):
        try:
            listed = json.loads(
                subprocess.check_output(
                    config.get("gh_command", ["gh"])
                    + [
                        "issue",
                        "list",
                        "--repo",
                        repo,
                        "--state",
                        "open",
                        "--label",
                        "ready",
                        "--json",
                        "number,title,createdAt,labels",
                        "--limit",
                        str(DISCOVERY_LIMIT),
                    ],
                    text=True,
                )
            )
            if not isinstance(listed, list):
                raise ValueError("gh issue list did not return a list")
        except (subprocess.CalledProcessError, OSError, ValueError) as error:
            print(f"DISCOVERY {repo} skipped: {error}", flush=True)
            continue
        if len(listed) >= DISCOVERY_LIMIT:
            print(f"DISCOVERY {repo} hit the {DISCOVERY_LIMIT} issue limit", flush=True)
        for item in listed:
            if "ready" not in {label.get("name") for label in item.get("labels", [])}:
                continue
            issue = {
                "repo": repo,
                "number": item["number"],
                "created_at": item["createdAt"],
            }
            ordered_issues([issue])
            key = (repo, issue["number"])
            if (
                key in known
                or f"{repo}#{issue['number']}" in completed
                or skip_reason(issue)
            ):
                continue
            known.add(key)
            found.append(issue)
    return sorted(found, key=lambda issue: issue["created_at"])


def skip_reason(issue):
    if issue["number"] in (2, 23, 26):
        return "global operator hold"
    if (issue["repo"], issue["number"]) == ("arii/remote-server", 1377):
        return "requires a human decision"
    if issue["repo"] == "arii/remote-server" and issue["number"] == 1359:
        return "already merged via arii/remote-server#1381"
    if issue.get("merged") is True or issue.get("fixed") is True:
        return "already merged or fixed"
    if issue.get("requires_human") is True:
        return "requires a human decision"
    return issue.get("skip_reason")


def checked_cap(value):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 < value <= ISSUE_CAP_USD
    ):
        raise ValueError(
            f"cap_usd must be finite, positive, and at most ${ISSUE_CAP_USD:.2f}"
        )
    return float(value)


LEDGER_AGENT = "S5-Queue"
LEDGER_STALE_HOURS = 6
# Form timestamps are naive, in the Agent Ledger Sheet's timezone.
LEDGER_TIMEZONE = "America/Los_Angeles"
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
STATUS_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")


def ledger_module(name):
    """Import a sibling script, as a package module or as a top-level script."""
    return importlib.import_module(f"{__package__}.{name}" if __package__ else name)


def ledger_write(agent, note, status, link):
    return ledger_module("ledger_post").post_entry(
        agent, note, status=status, link=link
    )


def ledger_rows():
    reader = ledger_module("ledger_read")
    return reader.read_ledger(
        os.environ.get("AGENT_LEDGER_SHEET_ID", reader.DEFAULT_SHEET_ID),
        os.environ.get("GOOGLE_TOKEN_FILE", reader.DEFAULT_TOKEN_PATH),
    )


def audit_failures(config):
    if not (database := config.get("failure_audit_db")):
        return
    try:
        ledger_module("twt_failure_audit").audit(config, database, poster=ledger_write)
    except Exception as error:
        print(f"S5 failure audit failed: {type(error).__name__}: {error}", flush=True)


def ledger_log(message):
    print("LEDGER " + message, flush=True)


def ledger_age_hours(stamp, now):
    for form in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            then = datetime.datetime.strptime(stamp.strip(), form)
        except ValueError:
            continue
        zone = zoneinfo.ZoneInfo(LEDGER_TIMEZONE)
        return (now - then.replace(tzinfo=zone)).total_seconds() / 3600
    return None


def is_spend_cap(result):
    return "recorded spend cap reached" in str(result.get("detail") or "")


class QueueLedger:
    """Agent Ledger rows for queue runs. A failed post never fails the run; a
    failed read skips the issue, because an unchecked claim may be someone else's.

    Rows are keyed by Link = issue URL, because ledger_read folds the latest row
    per Link. The PR URL goes in the note so the key stays stable.
    """

    def __init__(self, config, status, status_path):
        self.stale_hours = config.get("ledger_stale_hours", LEDGER_STALE_HOURS)
        if not isinstance(self.stale_hours, (int, float)) or self.stale_hours <= 0:
            raise ValueError("ledger_stale_hours must be positive")
        self.gh = config.get("gh_command", ["gh"])
        self.status, self.status_path = status, status_path
        self.run_id = uuid.uuid4().hex[:12]

    @staticmethod
    def url(key):
        repo, number = key.rsplit("#", 1)
        return f"https://github.com/{repo}/issues/{number}"

    def post(self, note, state, link):
        try:
            ledger_write(LEDGER_AGENT, note, state, link)
        except Exception as error:
            self.defer({"note": note, "status": state, "link": link}, error)

    def defer(self, row, error):
        ledger_log(f"post failed, queued for one retry: {type(error).__name__}")
        self.status.setdefault("ledger_pending", []).append(row)
        write_json(self.status_path, self.status)

    def boundary(self, merges=False):
        """Retry queued posts once (a second failure is dropped), then confirm merges."""
        pending = self.status.pop("ledger_pending", [])
        if pending:
            write_json(self.status_path, self.status)
        for row in pending:
            try:
                if "claim" in row:
                    ledger_module("ledger_post").post_claim_row(
                        row["claim"], row["run_id"]
                    )
                else:
                    ledger_write(LEDGER_AGENT, row["note"], row["status"], row["link"])
            except Exception as error:
                ledger_log(f"retry failed, dropped: {type(error).__name__}")
        if not merges:
            return
        for key, entry in list(self.status.get("ledger_open_prs", {}).items()):
            try:
                view = json.loads(
                    subprocess.check_output(
                        self.gh
                        + ["pr", "view", entry["pr"], "--json", "state,mergeCommit"],
                        text=True,
                    )
                )
            except Exception as error:
                ledger_log(f"merge check failed for {key}: {type(error).__name__}")
                continue
            if view.get("state") == "MERGED":
                sha = str((view.get("mergeCommit") or {}).get("oid") or "")[:7]
                self.post(
                    f"merged {sha or 'unknown'} {entry['pr']}", "Done", entry["issue"]
                )
            if view.get("state") in ("MERGED", "CLOSED"):
                self.status["ledger_open_prs"].pop(key)
                write_json(self.status_path, self.status)

    def claimed_elsewhere(self, key):
        """Return a skip reason when another source holds a fresh Act claim or
        the ledger cannot be read."""
        link = self.url(key)
        try:
            rows = [r for r in ledger_rows() if (r.get("Link") or "").strip() == link]
        except Exception as error:
            ledger_log(f"read failed, skipping {key}: {type(error).__name__}")
            return f"ledger unreadable ({type(error).__name__}), failing closed"
        if not rows:
            return None
        latest = rows[-1]
        source = (latest.get("Agent / Source") or "").strip()
        if (latest.get("Status") or "").strip() != "Act" or source == LEDGER_AGENT:
            return None
        age = ledger_age_hours(
            latest.get("Timestamp") or "",
            datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc),
        )
        if age is not None and age >= self.stale_hours:
            return None
        shown = "unknown age" if age is None else f"{age:.1f}h old"
        return f"fresh Act claim by {source[:40]} ({shown})"

    def claim(self, key):
        link = self.url(key)
        try:
            ledger_module("ledger_post").post_claim_row(link, self.run_id)
        except Exception as error:
            self.defer({"claim": link, "run_id": self.run_id}, error)

    def pr_problem(self, pr):
        """Return why the PR claim cannot be shown valid, or "" when it can."""
        try:
            view = json.loads(
                subprocess.check_output(
                    self.gh + ["pr", "view", pr, "--json", "body,files"], text=True
                )
            )
            files = [f.get("path", "") for f in view.get("files") or []]
        except Exception as error:
            ledger_log(f"PR check failed: {type(error).__name__}")
            return "pr_unverifiable"
        ok, reason = claims_pr_valid({"body": view.get("body"), "files": files})
        return "" if ok else reason

    def result(self, key, summary, limit_hit=False):
        link = self.url(key)
        run = f"queue run {self.run_id}"
        if summary is None:
            return self.post(f"Later: queue_halted, {run}", "Later", link)
        result = summary.get("result") or {}
        code = str(result.get("status") or "UNKNOWN")
        code = code if STATUS_CODE.fullmatch(code) else "UNKNOWN"
        pr = str(result.get("submit_url") or "")
        recon = summary.get("reconciliation") or {}
        if not summary.get("exit_code") and code == "APPROVED" and PR_URL.fullmatch(pr):
            if problem := self.pr_problem(pr):
                return self.post(
                    f"Later: invalid PR claim ({problem}), {run}", "Later", link
                )
            cost = recon.get("cost_usd")
            spend = f"${cost:.4f}" if type(cost) in (int, float) else "unknown"
            basis = recon.get("cost_basis") or "unknown"
            basis = basis if basis in ("upper_bound", "reported") else "unknown"
            self.status.setdefault("ledger_open_prs", {})[key] = {
                "issue": link,
                "pr": pr,
            }
            write_json(self.status_path, self.status)
            return self.post(
                f"PR {pr} opened, {run}, cost {spend} ({basis}), tests pass",
                "Act",
                link,
            )
        if is_spend_cap(result):
            return self.post(
                f"Needs Okay: spend cap reached, {run}", "Needs Okay", link
            )
        if code in ("BUDGET_EXHAUSTED", "BUDGET_UNSPLITTABLE"):
            return self.post(f"Needs Okay: {code}, {run}", "Needs Okay", link)
        reason = "max_common_failures" if limit_hit else code
        self.post(f"Later: {reason}, {run}", "Later", link)


def coverage(state):
    from backend.shared import ledger
    from backend.shared import runs
    from backend.shared import twt_receipts

    expected, local, receipts = set(), set(), {}
    records, refresh = [], set()
    settled = twt_receipts._settled_trace_ids()
    sources = collections.defaultdict(set)
    for journal in (state / "receipts").glob("*.jsonl"):
        entries = [
            json.loads(line) for line in journal.read_text().splitlines() if line
        ]
        journal_ids = {row["trace_id"] for row in entries}
        if len(journal_ids) != len(entries) or expected.intersection(journal_ids):
            raise SafetyStop("duplicate receipt journal IDs")
        expected.update(journal_ids)
        for row in entries:
            sources[(row["base_url"], row.get("uds"))].add(row["trace_id"])
        local.update(
            row["trace_id"] for row in entries if row.get("local_only") is True
        )
        settlement = journal.with_suffix(".settlement.json")
        if not settlement.exists():
            raise SafetyStop("receipt journal has no automatic settlement")
        record = read_json(settlement)
        if record.get("error") or record.get("missing"):
            raise SafetyStop(
                "automatic reconciliation failed: "
                + str(record.get("error") or record["missing"])
            )
        saved = {}
        for row in record.get("receipts", []):
            if row["trace_id"] not in journal_ids:
                raise SafetyStop("settlement contains a foreign receipt")
            if row["trace_id"] in saved:
                raise SafetyStop("duplicate settlement receipt")
            saved[row["trace_id"]] = row
        promoted = twt_receipts.ledger_receipts(journal_ids, saved)
        receipts.update(promoted)
        refresh.update(
            trace
            for trace in journal_ids & settled
            if promoted.get(trace, {}).get("cost_complete") is not True
            or twt_receipts.receipt_cost(promoted.get(trace)) is None
        )
        records.append((settlement, record, journal_ids))
    missing_snapshots = (expected - local - receipts.keys()) | refresh
    for (base_url, uds), ids in sources.items():
        missing = ids & missing_snapshots
        if missing:
            fresh = twt_receipts.fetch(base_url, sorted(missing), uds=uds)
            receipt_total(missing, (), fresh)
            receipts.update((row["trace_id"], row) for row in fresh)
    rows = []
    if Path(ledger.LEDGER).exists():
        rows = [
            json.loads(line)
            for line in Path(ledger.LEDGER).read_text().splitlines()
            if line
        ]
    owners = collections.Counter(
        trace
        for row in rows
        if twt_receipts._owns_receipts(row)
        for trace in row.get("usage", {}).get("twt_trace_ids", [])
    )
    if any(owners[trace] != 1 for trace in expected) or set(owners) - expected:
        raise SafetyStop("receipt ownership does not match the issue ledger exactly")
    totals = runs.total_metered_spend()
    if totals["unknown"]:
        raise SafetyStop("issue ledger has unknown metered costs")
    exact = receipt_total(expected, local, list(receipts.values()))
    if abs(totals["spent"] - exact) > 0.000002:
        raise SafetyStop("TwT receipts and issue ledger costs differ")
    for settlement, record, ids in records:
        snapshots = [receipts[trace] for trace in sorted(ids) if trace in receipts]
        if snapshots != record.get("receipts", []):
            record["receipts"] = snapshots
            write_json(settlement, record)
    return {
        "reconciled": True,
        "trace_ids": sorted(expected),
        "missing": [],
        "unknown_costs": 0,
        "cost_usd": exact,
        "ledger_cost_usd": totals["spent"],
        "cost_basis": "upper_bound"
        if any(
            row.get("cost_complete") is not True
            and row.get("cost_basis") == "upper_bound"
            for row in receipts.values()
        )
        else "reported",
    }


def worker(job_dir):
    directory = Path(job_dir).resolve()
    job = read_json(directory / "job.json")
    cap = checked_cap(job["cap_usd"])
    owner = (directory.stat().st_uid, directory.stat().st_gid)
    repo = Path(job["checkout"]).resolve()
    state = directory / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    os.environ.update(job.get("environment", {}))
    os.environ.update(
        EVAL_LEDGER=str(directory / "ledger.jsonl"),
        DEEP_AGENT_STATE_DIR=str(state),
        S5_CHECKOUT=str(repo),
        NEBIUS_SPEND_CAP_USD=str(cap),
        NEBIUS_ONLY="1",
        S5_ISSUE_PROVIDER="twt",
        S5_LOCAL_ONLY="0",
        OLLAMA_STRICT="0",
        S5_BUDGET_STATE=str(directory / "budget.json"),
    )
    for key in ("CODER_MODEL", "S5_AUDIT_MODEL", "S5_START_MODEL_TIER"):
        os.environ.pop(key, None)
    routing = job.get("routing") or {}
    picks = routing.get("picks") or {}
    for pick_tier, env_key in (
        ("fast", "CODER_MODEL"),
        ("strong", "CODER_STRONG_MODEL"),
    ):
        pin = (picks.get(pick_tier) or {}).get("model")
        if pin:
            os.environ[env_key] = pin
    tier = job.get("model_tier", "default")
    if tier == "strong":
        os.environ["S5_START_MODEL_TIER"] = "strong"
    os.chdir(repo)
    result = {}
    stages = []
    fatal = []
    stops = []
    strong_model = None
    started = time.time()

    def save(name, value):
        write_json(directory / name, value, owner)

    try:
        from backend.shared import cli
        import pipeline
        from backend.shared import runs

        install_budget_guard(cap, directory, fatal, stops)
        if tier == "strong":
            strong_model = check_strong_start(cap, fatal)
        print(
            f"tier={tier} reason={job.get('tier_reason', 'default')}"
            + (f" model={strong_model}" if strong_model else ""),
            file=sys.stderr,
            flush=True,
        )
        if routing.get("picks"):
            try:
                from backend.shared import task_router

                task_router.log_decision(
                    routing,
                    issue_key=f"{job['issue']['repo']}#{job['issue']['number']}",
                )
            except Exception as error:
                print(f"routing audit skipped: {error}", file=sys.stderr, flush=True)
        try:
            from backend.shared import model_router

            preflight = getattr(model_router, "preflight_gateway", None)
        except ImportError:
            preflight = None
        if (
            os.environ.get("S5_ISSUE_PROVIDER") == "twt"
            and os.environ.get("TWT_PREFLIGHT_REQUIRED") == "1"
            and preflight is not None
        ):
            preflight(stage="coder")
        from scripts import slop_review

        slop = {}
        pipeline.Pipeline.stage_submit = slop_review.gated_submit(
            pipeline.Pipeline.stage_submit,
            slop,
            root=slop_review.checkout_root(),
            gh=["gh"],
            model_factory=lambda: slop_review.default_model(
                f"slop-{job['issue']['number']}"
            ),
        )
        for stage in ("init", "spec", "code", "audit", "submit", "cleanup"):
            original = getattr(pipeline.Pipeline, "stage_" + stage)

            def timed(original=original, stage=stage):
                @functools.wraps(original)
                def call(instance, *args, **kwargs):
                    mark = time.time()
                    entry = {"stage": stage, "started_at": mark}
                    try:
                        output = original(instance, *args, **kwargs)
                        entry["outcome"] = output[0]
                        if stage == "code" and (directory / "budget.json").exists():
                            entry["calls"] = read_json(directory / "budget.json")[
                                "parent"
                            ]["calls_used"]
                        if stage == "audit" and isinstance(output[1], dict):
                            entry["audit"] = {
                                key: output[1].get(key)
                                for key in ("valid", "verdict", "findings", "errors")
                            }
                        if stage == "submit":
                            entry["submit_url"] = output[1]
                        return output
                    except BaseException as error:
                        entry["error"] = f"{type(error).__name__}: {error}"
                        raise
                    finally:
                        entry["elapsed_s"] = round(time.time() - mark, 3)
                        entry["recorded_spend"] = runs.total_metered_spend()
                        stages.append(entry)
                        save("stages.json", stages)

                return call

            setattr(pipeline.Pipeline, "stage_" + stage, timed())

        original_run = pipeline.Pipeline.run

        @functools.wraps(original_run)
        def capture_result(instance):
            value = slop_review.override_result(original_run(instance), slop)
            result.update(value)
            save("pipeline-result.json", value)
            return value

        pipeline.Pipeline.run = capture_result
        issue = job["issue"]
        intent = f"Fix {issue['repo']}#{issue['number']}: {issue['title']}\n\n{issue['body']}"
        code = cli.main(
            [
                "run-pipeline",
                "--intent",
                intent,
                "--repo",
                str(repo),
                "--issue",
                str(issue["number"]),
                "--submit-pr",
                "--keep-worktree",
            ]
        )
    except StrongRefused as error:
        code = 1
        result.update(status=error.status, detail=str(error))
    except BaseException as error:
        code = 1
        result.update(status="RUNNER_ERROR", detail=f"{type(error).__name__}: {error}")
    try:
        reconciliation = coverage(state)
    except BaseException as error:
        fatal.append(f"{type(error).__name__}: {error}")
        reconciliation = {"reconciled": False, "error": fatal[-1]}
    summary = {
        "issue": job["issue"]["number"],
        "repo": job["issue"]["repo"],
        "exit_code": code,
        "tier": tier,
        "tier_reason": job.get("tier_reason", "default"),
        "strong_model": strong_model,
        "budget_stop": bool(stops),
        "result": result,
        "budget": result.get("budget"),
        "stages": stages,
        "reconciliation": reconciliation,
        "halt_queue": bool(fatal),
        "halt_reasons": fatal,
        "elapsed_s": round(time.time() - started, 3),
    }
    save("summary.json", summary)
    print(json.dumps(summary, default=str), flush=True)
    return 2 if fatal else code


def recover(job_dir):
    directory = Path(job_dir).resolve()
    job = read_json(directory / "job.json")
    state = directory / "state"
    os.environ.update(job.get("environment", {}))
    os.environ.update(
        EVAL_LEDGER=str(directory / "ledger.jsonl"), DEEP_AGENT_STATE_DIR=str(state)
    )
    owner = (directory.stat().st_uid, directory.stat().st_gid)
    try:
        from backend.shared import twt_receipts

        for journal in (state / "receipts").glob("*.jsonl"):
            entries = [
                json.loads(line) for line in journal.read_text().splitlines() if line
            ]
            if not entries or any(
                Path(entry["ledger_path"]).resolve() != directory / "ledger.jsonl"
                for entry in entries
            ):
                raise SafetyStop(
                    "receipt journal has no entries or names a foreign ledger"
                )
            first = entries[0]
            if any(
                (entry["run_id"], entry["base_url"], entry.get("uds"))
                != (first["run_id"], first["base_url"], first.get("uds"))
                for entry in entries
            ):
                raise SafetyStop("receipt journal mixes runs or endpoints")
            scope = twt_receipts.ReceiptRun(first["run_id"], first["base_url"])
            scope.path, scope.started, scope.uds = (
                journal,
                first["started"],
                first.get("uds"),
            )
            scope.ids = {entry["trace_id"] for entry in entries}
            scope.local_ids = {
                entry["trace_id"]
                for entry in entries
                if entry.get("local_only") is True
            }
            scope.reconcile()
            if scope.error:
                raise SafetyStop(scope.error)
        result = coverage(state)
    except Exception as error:
        result = {"reconciled": False, "error": f"{type(error).__name__}: {error}"}
    write_json(directory / "recovery.json", result, owner)
    return 0 if result["reconciled"] else 2


def worker_command(config, mode, directory):
    source = Path(config["pipeline_checkout"])
    return config.get("runtime_command", []) + [
        "env",
        "PYTHONPATH=" + os.pathsep.join((str(source / "deep-agent"), str(source))),
        config["worker_python"],
        "-P",
        "-",
        mode,
        str(directory),
    ]


def reconcile_job(config, source, directory, runtime_directory):
    recovery = directory / "recovery.json"
    recovery.unlink(missing_ok=True)
    with (directory / "recovery.log").open("a") as log:
        process = subprocess.Popen(
            worker_command(config, "--reconcile", runtime_directory),
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process.communicate(source)
    if process.returncode != 0 or read_json(recovery).get("reconciled") is not True:
        raise SafetyStop("startup receipt reconciliation failed")
    return read_json(recovery)


def archive_issue_worktree(directory):
    directory = Path(directory).resolve()
    job = read_json(directory / "job.json")
    repo = Path(job["checkout"]).resolve()
    source = repo / ".worktrees" / f"issue-{job['issue']['number']}"
    destination = source.parent / ".queue-retry" / directory.name
    manifest = directory / "worktree-archive.json"
    owner = (directory.stat().st_uid, directory.stat().st_gid)

    def git(*args, cwd=repo):
        return subprocess.check_output(
            ["git", "-C", str(cwd), *args], text=True
        ).strip()

    if manifest.exists():
        record = read_json(manifest)
        if record["source"] != str(source) or record["destination"] != str(destination):
            raise SafetyStop("retry worktree archive differs from saved intent")
    else:
        if destination.exists() or source.is_symlink():
            raise SafetyStop("unexpected retry worktree archive or symlink")
        record = {"source": str(source), "destination": str(destination)}
        if not source.exists():
            record["absent"] = True
        else:
            common = (
                source / git("rev-parse", "--git-common-dir", cwd=source)
            ).resolve()
            if common != (repo / git("rev-parse", "--git-common-dir")).resolve():
                raise SafetyStop("retry worktree belongs to another repository")
            if (
                Path(git("rev-parse", "--show-toplevel", cwd=source)).resolve()
                != source
            ):
                raise SafetyStop("retry source is not a worktree root")
            branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=source)
            if branch in ("main", "master"):
                raise SafetyStop("refusing to rename a primary branch during retry")
            record.update(
                branch=None if branch == "HEAD" else branch,
                archive_branch="queue-retry/" + directory.name,
                head=git("rev-parse", "HEAD", cwd=source),
            )
        write_json(manifest, record, owner)
    if record.get("absent"):
        if source.exists() or destination.exists():
            raise SafetyStop("worktree appeared after an absent archive intent")
        return 0
    if source.exists():
        if destination.exists() or source.is_symlink():
            raise SafetyStop("retry worktree source and archive conflict")
        branch = (
            git("symbolic-ref", "--short", "HEAD", cwd=source)
            if record["branch"]
            else "HEAD"
        )
        if record["branch"] and branch == record["branch"]:
            old_ref = "refs/heads/" + record["branch"]
            new_ref = "refs/heads/" + record["archive_branch"]
            refs = dict(
                line.split(" ", 1)
                for line in git(
                    "for-each-ref",
                    "--format=%(refname) %(objectname)",
                    old_ref,
                    new_ref,
                ).splitlines()
            )
            if old_ref not in refs:
                if refs.get(new_ref) != record["head"]:
                    raise SafetyStop("renamed retry branch differs from saved evidence")
                git("symbolic-ref", "HEAD", new_ref, cwd=source)
            else:
                git("branch", "-m", record["archive_branch"], cwd=source)
        elif branch != (record["archive_branch"] if record["branch"] else "HEAD"):
            raise SafetyStop("retry worktree branch changed during archival")
        destination.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "move", str(source), str(destination))
    if not destination.is_dir():
        raise SafetyStop("retry worktree source and archive missing")
    if destination.is_symlink():
        raise SafetyStop("retry worktree archive is a symlink")
    git("worktree", "repair", str(destination))
    if git("rev-parse", "HEAD", cwd=destination) != record["head"]:
        raise SafetyStop("archived worktree HEAD differs from saved evidence")
    if git("rev-parse", "--abbrev-ref", "HEAD", cwd=destination) != (
        record["archive_branch"] if record["branch"] else "HEAD"
    ):
        raise SafetyStop("archived worktree branch differs from saved evidence")
    record["complete"] = True
    write_json(manifest, record, owner)
    return 0


def archive_retry_worktree(config, root, archive):
    runtime = Path(config["runtime_state_dir"]) / archive.relative_to(root)
    with (archive / "worktree-archive.log").open("a") as log:
        subprocess.run(
            worker_command(config, "--archive-worktree", runtime),
            input=Path(__file__).read_text(),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )


def finish_retry(root, status_path, status, config):
    intent = status.get("retry_intent")
    if not intent:
        return
    key = intent["issue"]
    repo, number = key.rsplit("#", 1)
    issue = {"repo": repo, "number": int(number)}
    ordered_issues([issue])
    directory = root / (repo.replace("/", "_") + "-" + str(issue["number"]))
    archive = Path(intent["directory"])
    if directory.exists():
        if archive.exists():
            raise SafetyStop("retry source and archive both exist; inspect evidence")
        directory.rename(archive)
    elif not archive.is_dir():
        raise SafetyStop("retry source and archive missing; inspect evidence")
    archive_retry_worktree(config, root, archive)
    status["completed"] = [item for item in status["completed"] if item != key]
    status.get("skipped", {}).pop(key, None)
    status.update(retry=key, done=False)
    status.setdefault("retry_spend", {})[key] = intent["prior_spend_usd"]
    status.setdefault("retry_archives", []).append(
        {field: intent[field] for field in ("issue", "directory", "cost_usd")}
    )
    status.pop("retry_intent")
    write_json(status_path, status)


def retry_issue(config_path, key):
    repo, number = key.rsplit("#", 1)
    issue = {"repo": repo, "number": int(number)}
    ordered_issues([issue])
    config = read_json(config_path)
    cap = checked_cap(config.get("cap_usd", ISSUE_CAP_USD))
    issues = read_json(config["issues_file"])
    matches = [i for i in issues if (i["repo"], i["number"]) == (repo, issue["number"])]
    if len(matches) != 1 or skip_reason(matches[0]):
        raise SafetyStop("retry requires one eligible issue in the manifest")
    root = Path(config["state_dir"]).resolve()
    name = repo.replace("/", "_") + "-" + str(issue["number"])
    directory = root / name
    with (root / "queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status_path = root / "status.json"
        status = read_json(status_path)
        finish_retry(root, status_path, status, config)
        if status.get("current") or status.get("retry"):
            raise SafetyStop("resolve the current or pending retry before retrying")
        saved = read_json(directory / "job.json")["issue"]
        if (saved["repo"], saved["number"]) != (repo, issue["number"]):
            raise SafetyStop("retry issue differs from saved job")
        summary = read_json(directory / "summary.json")
        if (
            key not in status.get("completed", [])
            or type(summary.get("exit_code")) is not int
            or summary["exit_code"] == 0
            or summary.get("halt_queue") is not False
        ):
            raise SafetyStop(
                "retry requires a finished failed run without a saved halt"
            )
        runtime = Path(config["runtime_state_dir"])
        if not runtime.is_absolute():
            raise ValueError("runtime_state_dir must be absolute")
        recovery = reconcile_job(
            config, Path(__file__).read_text(), directory, runtime / name
        )
        prior = status.get("retry_spend", {}).get(key, 0)
        cost = recovery.get("cost_usd")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in (prior, cost)
        ):
            raise SafetyStop("retry requires known nonnegative prior spend")
        spent = prior + cost
        if spent >= cap:
            raise BudgetStop("issue budget exhausted; retry refused")
        archive_root = root / "archive"
        archive_root.mkdir(mode=0o700, exist_ok=True)
        archive = archive_root / f"{name}-{time.time_ns()}"
        status["retry_intent"] = {
            "issue": key,
            "directory": str(archive),
            "cost_usd": cost,
            "prior_spend_usd": spent,
        }
        write_json(status_path, status)
        finish_retry(root, status_path, status, config)
        print(f"REQUEUED {key} archive={archive} prior_spend_usd={spent:.8f}")
    return 0


def prune_landed_worktrees(config):
    """Remove landed agent worktrees (incl. .queue-retry) in every configured checkout.

    The script keeps unmerged, dirty and non-agent trees and the operator
    `queue-main` tree. A failure is logged and never stops the queue.
    """
    script = Path(config["pipeline_checkout"]) / "scripts" / "prune-worktrees.sh"
    for repo, checkout in config["repo_paths"].items():
        try:
            result = subprocess.run(
                ["bash", str(script), "--prune"],
                cwd=checkout,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as error:
            print(f"PRUNE {repo} failed: {error}", flush=True)
            continue
        if result.returncode != 0:
            print(f"PRUNE {repo} failed: {result.stderr.strip()}", flush=True)
        # Forward removals and the script's indented "(... failed/kept)" notes.
        for line in result.stdout.splitlines():
            if line.startswith(("PRUNE ", "      (")):
                print(f"PRUNE {repo} {line.strip()}", flush=True)


def supervise(config_path, *, clear_halt=False):
    config = read_json(config_path)
    cap = checked_cap(config.get("cap_usd", ISSUE_CAP_USD))
    failure_limit = config.get("max_common_failures", 3)
    if type(failure_limit) is not int or failure_limit < 1:
        raise ValueError("max_common_failures must be a positive integer")
    strong_after = config.get("strong_after_failures", 2)
    if type(strong_after) is not int or strong_after < 1:
        raise ValueError("strong_after_failures must be a positive integer")
    root = Path(config["state_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root = Path(config["runtime_state_dir"])
    if not runtime_root.is_absolute():
        raise ValueError("runtime_state_dir must be absolute")
    pipeline_checkout = Path(config["pipeline_checkout"])
    if not pipeline_checkout.is_absolute():
        raise ValueError("pipeline_checkout must be absolute")
    for repo in config.get("discover_repos", []):
        if repo not in config["repo_paths"]:
            raise ValueError("discover_repos entry has no repo_paths checkout: " + repo)
    source = Path(__file__).read_text()
    status_path = root / "status.json"
    with (root / "queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # queue_status reads this to tell a live supervisor from a --retry
        # holding the same lock while the service is stopped.
        (root / "supervisor.pid").write_text(str(os.getpid()))
        status = read_json(status_path) if status_path.exists() else {"completed": []}
        finish_retry(root, status_path, status, config)
        ledger = QueueLedger(config, status, status_path)
        ledger.boundary(merges=True)
        if status.get("current"):
            current = status["current"]
            try:
                repo, number = current.rsplit("#", 1)
                issue = {"repo": repo, "number": int(number)}
                ordered_issues([issue])
                name = repo.replace("/", "_") + "-" + str(issue["number"])
                directory = root / name
                saved = read_json(directory / "job.json")["issue"]
                if (saved["repo"], saved["number"]) != (repo, issue["number"]):
                    raise SafetyStop("current issue differs from saved job")
                try:
                    recovery = reconcile_job(
                        config, source, directory, runtime_root / name
                    )
                finally:
                    audit_failures(config)
                summary_path = directory / "summary.json"
                summary = read_json(summary_path) if summary_path.exists() else {}
                if summary.get("halt_queue") is True:
                    if not clear_halt:
                        status.update(
                            halted=True,
                            reason=summary.get("halt_reasons")
                            or "saved worker halt requires operator clearance",
                        )
                        write_json(status_path, status)
                        return 2
                    summary.update(
                        halt_queue=False,
                        acknowledged_halt_reasons=summary.get("halt_reasons", []),
                        halt_reasons=[],
                        halt_acknowledged_at=time.time(),
                    )
                    write_json(summary_path, summary)
                if current not in status["completed"]:
                    status["completed"].append(current)
                status.update(current=None, last_recovery=recovery)
                write_json(status_path, status)
            except Exception as error:
                status.update(halted=True, reason=f"startup reconciliation: {error}")
                write_json(status_path, status)
                return 2
        if status.get("halted") and not clear_halt:
            raise SafetyStop(
                "queue is halted; inspect status before an explicit recovery"
            )
        if clear_halt:
            status.update(halted=False, reason=None, halt_acknowledged_at=time.time())
            (root / "STOP").unlink(missing_ok=True)
            write_json(status_path, status)
        # Startup pass runs only after the claimed issue is reconciled, so
        # a crashed run's worktree is never swept before its recovery.
        prune_landed_worktrees(config)
        stopping = []

        def stop(_number, _frame):
            stopping.append(True)
            (root / "STOP").touch(mode=0o600)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        issues = ordered_issues(read_json(config["issues_file"]))
        issues += discover_ready(config, issues, status["completed"])
        issues.sort(
            key=lambda issue: (
                f"{issue['repo']}#{issue['number']}" != status.get("retry")
            )
        )
        for issue in issues:
            if stopping or (root / "STOP").exists():
                status.update(stopped=True, current=None)
                write_json(status_path, status)
                return 0
            key = f"{issue['repo']}#{issue['number']}"
            if key in status["completed"]:
                continue
            reason = skip_reason(issue)
            if reason:
                status.setdefault("skipped", {})[key] = reason
                if status.get("retry") == key:
                    status.pop("retry")
                write_json(status_path, status)
                continue
            if issue["repo"] not in config["repo_paths"]:
                raise ValueError("no configured checkout for " + issue["repo"])
            try:
                fresh = json.loads(
                    subprocess.check_output(
                        config.get("gh_command", ["gh"])
                        + ["api", f"repos/{issue['repo']}/issues/{issue['number']}"],
                        text=True,
                    )
                )
                state = fresh["state"]
                labels = {
                    label.get("name") if isinstance(label, dict) else label
                    for label in fresh.get("labels") or []
                }
            except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as e:
                print(f"SKIP {key} refresh failed: {e}", flush=True)
                continue
            if state != "open":
                status["completed"].append(key)
                if status.get("retry") == key:
                    status.pop("retry")
                write_json(status_path, status)
                continue
            if "ready" not in labels:
                print(f"SKIP {key} not ready", flush=True)
                continue
            name = issue["repo"].replace("/", "_") + "-" + str(issue["number"])
            directory = root / name
            if directory.exists():
                status.update(
                    halted=True,
                    reason="unfinished issue directory requires inspection",
                    current=key,
                )
                write_json(status_path, status)
                return 2
            try:
                remaining_cap = checked_cap(
                    cap - status.get("retry_spend", {}).get(key, 0)
                )
            except ValueError as error:
                status.update(
                    halted=True, reason=f"issue budget prevents dispatch: {error}"
                )
                write_json(status_path, status)
                return 2
            ledger.boundary()
            held = ledger.claimed_elsewhere(key)
            if held:
                ledger_log(f"skip {key}: {held}")
                status.setdefault("ledger_skipped", {})[key] = held
                write_json(status_path, status)
                continue
            if stopping or (root / "STOP").exists():
                status.update(stopped=True, current=None)
                write_json(status_path, status)
                return 0
            directory.mkdir(mode=0o700)
            runtime_directory = runtime_root / name
            tier, tier_reason = start_tier(
                fresh, status.get("tier_failures", {}).get(key, 0), strong_after
            )
            # Deterministic task routing: pick concrete provider/model
            # pins per tier from the live TwT catalog, and raise the
            # start tier when the task itself looks hard — never lowered
            # below the escalation/label tier. Router failure keeps the
            # configured environment untouched.
            routing = None
            try:
                from backend.shared import task_router

                routing = task_router.route_issue(fresh, remaining_cap)
                if routing["tier"] == "strong" and tier != "strong":
                    tier = "strong"
                    tier_reason = f"{tier_reason}+{routing['tier_rule']}"
            except Exception as error:
                routing = {"error": str(error)}
            job = {
                "issue": {
                    "repo": issue["repo"],
                    "number": issue["number"],
                    "title": fresh["title"],
                    "body": fresh.get("body") or "",
                },
                "cap_usd": remaining_cap,
                "checkout": config["repo_paths"][issue["repo"]],
                "pipeline_checkout": config["pipeline_checkout"],
                "environment": config.get("environment", {}),
                "model_tier": tier,
                "tier_reason": tier_reason,
                "routing": routing,
            }
            write_json(directory / "job.json", job)
            status.update(current=key, started_at=time.time())
            if status.get("retry") == key:
                status.pop("retry")
            write_json(status_path, status)
            ledger.claim(key)
            print(f"START {key} tier={tier} reason={tier_reason}", flush=True)
            print(f"START {key} tier={tier} reason={tier_reason}", flush=True)
            with (directory / "run.log").open("w") as log:
                process = subprocess.Popen(
                    worker_command(config, "--worker", runtime_directory),
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                process.communicate(source)
            audit_failures(config)
            summary_path = directory / "summary.json"
            if not summary_path.exists():
                status.update(
                    halted=True,
                    reason="worker exited without verifiable cost/reconciliation",
                    current=key,
                )
                write_json(status_path, status)
                ledger.result(key, None)
                return 2
            summary = read_json(summary_path)
            if summary["halt_queue"] or not summary["reconciliation"]["reconciled"]:
                status.update(
                    halted=True, reason=summary.get("halt_reasons"), current=key
                )
                write_json(status_path, status)
                ledger.result(key, None)
                return 2
            if process.returncode != summary.get("exit_code"):
                status.update(
                    halted=True,
                    reason="worker exit disagrees with its summary",
                    current=key,
                )
                write_json(status_path, status)
                ledger.result(key, None)
                return 2
            status["completed"].append(key)
            status.update(last=summary, current=None)
            if summary["exit_code"] == 0:
                # Failed runs keep their tree as evidence for --retry.
                prune_landed_worktrees(config)
            failures = status.setdefault("tier_failures", {})
            if counts_toward_escalation(summary):
                failures[key] = failures.get(key, 0) + 1
            elif summary["exit_code"] == 0:
                failures.pop(key, None)
            status.setdefault("attempts", {}).setdefault(key, []).append(
                {
                    "tier": tier,
                    "reason": tier_reason,
                    "status": summary["result"].get("status"),
                    "exit_code": summary["exit_code"],
                }
            )
            if summary["exit_code"]:
                blocker = {
                    key: summary["result"].get(key) for key in ("status", "detail")
                }
                status["consecutive_failures"] = (
                    status.get("consecutive_failures", 0) + 1
                    if status.get("blocker") == blocker
                    else 1
                )
                status["blocker"] = blocker
            else:
                status.update(consecutive_failures=0, blocker=None)
            write_json(status_path, status)
            print(
                "FINISH " + key + " " + summary["result"].get("status", "UNKNOWN"),
                flush=True,
            )
            ledger.result(key, summary, status["consecutive_failures"] >= failure_limit)
            if status["consecutive_failures"] >= failure_limit:
                status.update(
                    halted=True, reason="consecutive failures with a common blocker"
                )
                write_json(status_path, status)
                return 2
            time.sleep(config.get("inter_issue_delay_seconds", 60))
        status.update(done=True, current=None)
        write_json(status_path, status)
    return 0


BUDGET_BUCKETS = ((1, 5), (6, 10), (11, 20), (21, 40))


def budget_bucket(calls):
    for low, high in BUDGET_BUCKETS:
        if low <= calls <= high:
            return f"{low}-{high}"
    return ">40"


def budget_report(root):
    """Plain-text call-budget metrics from each job's summary.json['budget'].

    root is the queue state dir (one job dir per issue) or an iterable of job dirs.
    """
    if isinstance(root, (str, Path)):
        root = sorted(path for path in Path(root).iterdir() if path.is_dir())
    labels = [budget_bucket(n) for n in (1, 6, 11, 21, 41)]
    buckets = {label: [0, 0] for label in labels}
    runs = verified = exhausted = calls_verified = 0
    for directory in root:
        path = Path(directory) / "summary.json"
        try:
            parent = (read_json(path).get("budget") or {}).get("parent")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(parent, dict) or type(parent.get("calls_used")) is not int:
            continue
        done = bool(parent.get("verified"))
        calls = parent["calls_used"]
        if calls < 1:
            continue  # zero-call runs (UNSPLITTABLE, early failures) are not budget data
        bucket = buckets[budget_bucket(calls)]
        bucket[0] += 1
        runs += 1
        exhausted += bool(parent.get("exhausted"))
        if done:
            bucket[1] += 1
            verified += 1
            calls_verified += calls
    lines = ["bucket  runs  verified  rate"]
    for label, (count, ok) in buckets.items():
        rate = f"{ok / count:.0%}" if count else "n/a"
        lines.append(f"{label:<7} {count:>4}  {ok:>8}  {rate}")
    per = f"{calls_verified / verified:.1f}" if verified else "n/a"
    rate = f"{exhausted / runs:.0%}" if runs else "n/a"
    lines.append(f"runs={runs} verified={verified} calls_per_verified={per}")
    lines.append(f"budget_exhaustion_rate={rate} ({exhausted}/{runs})")
    return "\n".join(lines)


STALL_SECONDS = 1800


def supervisor_alive(root):
    """True when supervisor.pid names a live supervisor process."""
    try:
        os.kill(int((root / "supervisor.pid").read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def queue_status(config, now=None):
    """Name the runner state and the operator's next action."""
    root = Path(config["state_dir"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = root / "status.json"
    status = read_json(status_path) if status_path.exists() else {}
    # The supervisor holds queue.lock for its whole run, so a free lock means
    # the heartbeat is missing. --retry takes the same lock while the service
    # is stopped, so a held lock only means busy unless supervisor.pid names
    # a live supervisor.
    with (root / "queue.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not supervisor_alive(root):
                return "BUSY queue.lock held by a non-supervisor process (e.g. --retry). Next action: wait for it to finish"
        else:
            if status.get("halted"):
                return f"DOWN halted: {status.get('reason')}. Next action: inspect the halt, then restart with --clear-halt"
            return "DOWN runner heartbeat missing. Next action: Restart Service"
    current = status.get("current")
    if not current:
        return "IDLE nothing claimed. Next action: Ready"
    repo, number = current.rsplit("#", 1)
    log = root / (repo.replace("/", "_") + "-" + number) / "run.log"
    # A fresh claim has no run.log yet (the claim is recorded before the
    # worker starts); fall back to the recorded claim time, not the epoch.
    activity = log.stat().st_mtime if log.exists() else status.get("started_at", 0)
    quiet = (now or time.time()) - activity
    if quiet > config.get("stall_seconds", STALL_SECONDS):
        return f"STALLED {current} no activity for {int(quiet)}s. Next action: Investigate/Restart"
    return f"RUNNING {current} active"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_config = os.environ.get("QUEUE_CONFIG")
    group = parser.add_mutually_exclusive_group(required=not default_config)
    group.add_argument(
        "--config", type=Path, default=Path(default_config) if default_config else None
    )
    group.add_argument("--worker", type=Path)
    group.add_argument("--reconcile", type=Path)
    group.add_argument("--archive-worktree", type=Path)
    parser.add_argument(
        "--retry",
        metavar="REPO#ISSUE",
        help="archive a reconciled failed run and prioritize one retry; queue must be idle",
    )
    parser.add_argument(
        "--budget-report",
        action="store_true",
        help="print verified completion rate per coder call bucket and exit",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print runner state (idle, stalled or down) with the next action and exit",
    )
    parser.add_argument(
        "--clear-halt",
        action="store_true",
        help="acknowledge saved halts after successful reconciliation and resume",
    )
    args = parser.parse_args()
    if args.status:
        if args.worker or args.reconcile or args.archive_worktree or args.retry:
            parser.error("--status requires supervisor configuration only")
        print(queue_status(read_json(args.config)))
        return 0
    if args.budget_report:
        if args.worker or args.reconcile or args.archive_worktree or args.retry:
            parser.error("--budget-report requires supervisor configuration only")
        print(budget_report(Path(read_json(args.config)["state_dir"])))
        return 0
    if args.retry:
        if args.worker or args.reconcile or args.archive_worktree or args.clear_halt:
            parser.error(
                "--retry requires supervisor configuration without --clear-halt"
            )
        return retry_issue(args.config, args.retry)
    directory = args.worker or args.reconcile or args.archive_worktree
    if directory:
        if args.clear_halt:
            parser.error("--clear-halt requires supervisor configuration")
        with (directory / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.archive_worktree:
                return archive_issue_worktree(directory)
            return worker(directory) if args.worker else recover(directory)
    return supervise(args.config, clear_halt=args.clear_halt)


if __name__ == "__main__":
    sys.exit(main())

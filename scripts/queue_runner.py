#!/usr/bin/env python3
"""Sequential, restart-safe supervisor for the existing deep-agent CLI.

Host mode reads --config. Worker mode runs in the configured Python runtime,
which may be reached through docker exec -i with this source on stdin.
No provider credentials belong in configuration or issue manifests.
"""

import argparse
import collections
import fcntl
import functools
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


class SafetyStop(RuntimeError):
    llm_not_dispatched = True


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


def check_budget(scope, cap):
    paid = refresh_spend(scope)
    if paid >= checked_cap(cap):
        raise BudgetStop(f"recorded spend cap reached: ${paid:.8f} / ${cap:.2f}")
    return paid


def install_budget_guard(cap, directory, fatal):
    from backend.shared import model_router
    from backend.shared import twt_receipts

    def guard():
        if (directory.parent / "STOP").exists():
            raise BudgetStop("queue stop requested before dispatch")
        if fatal:
            raise SafetyStop(fatal[0])
        try:
            check_budget(twt_receipts.current.get(), cap)
        except BudgetStop:
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
        or not 0 < value <= 0.5
    ):
        raise ValueError("cap_usd must be finite, positive, and at most $0.50")
    return float(value)


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
    )
    for key in ("CODER_MODEL", "S5_AUDIT_MODEL"):
        os.environ.pop(key, None)
    os.chdir(repo)
    result = {}
    stages = []
    fatal = []
    started = time.time()

    def save(name, value):
        write_json(directory / name, value, owner)

    try:
        from backend.shared import cli
        import pipeline
        from backend.shared import runs

        install_budget_guard(cap, directory, fatal)
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
            value = original_run(instance)
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
        "result": result,
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
    cap = checked_cap(config.get("cap_usd", 0.5))
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


def supervise(config_path, *, clear_halt=False):
    config = read_json(config_path)
    cap = checked_cap(config.get("cap_usd", 0.5))
    failure_limit = config.get("max_common_failures", 3)
    if type(failure_limit) is not int or failure_limit < 1:
        raise ValueError("max_common_failures must be a positive integer")
    root = Path(config["state_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_root = Path(config["runtime_state_dir"])
    if not runtime_root.is_absolute():
        raise ValueError("runtime_state_dir must be absolute")
    pipeline_checkout = Path(config["pipeline_checkout"])
    if not pipeline_checkout.is_absolute():
        raise ValueError("pipeline_checkout must be absolute")
    source = Path(__file__).read_text()
    status_path = root / "status.json"
    with (root / "queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = read_json(status_path) if status_path.exists() else {"completed": []}
        finish_retry(root, status_path, status, config)
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
                recovery = reconcile_job(config, source, directory, runtime_root / name)
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
        stopping = []

        def stop(_number, _frame):
            stopping.append(True)
            (root / "STOP").touch(mode=0o600)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        issues = ordered_issues(read_json(config["issues_file"]))
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
            fresh = json.loads(
                subprocess.check_output(
                    config.get("gh_command", ["gh"])
                    + ["api", f"repos/{issue['repo']}/issues/{issue['number']}"],
                    text=True,
                )
            )
            if fresh["state"] != "open":
                status["completed"].append(key)
                if status.get("retry") == key:
                    status.pop("retry")
                write_json(status_path, status)
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
            directory.mkdir(mode=0o700)
            runtime_directory = runtime_root / name
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
            }
            write_json(directory / "job.json", job)
            status.update(current=key, started_at=time.time())
            if status.get("retry") == key:
                status.pop("retry")
            write_json(status_path, status)
            print("START " + key, flush=True)
            with (directory / "run.log").open("w") as log:
                process = subprocess.Popen(
                    worker_command(config, "--worker", runtime_directory),
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                process.communicate(source)
            summary_path = directory / "summary.json"
            if not summary_path.exists():
                status.update(
                    halted=True,
                    reason="worker exited without verifiable cost/reconciliation",
                    current=key,
                )
                write_json(status_path, status)
                return 2
            summary = read_json(summary_path)
            if summary["halt_queue"] or not summary["reconciliation"]["reconciled"]:
                status.update(
                    halted=True, reason=summary.get("halt_reasons"), current=key
                )
                write_json(status_path, status)
                return 2
            if process.returncode != summary.get("exit_code"):
                status.update(
                    halted=True,
                    reason="worker exit disagrees with its summary",
                    current=key,
                )
                write_json(status_path, status)
                return 2
            status["completed"].append(key)
            status.update(last=summary, current=None)
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
        "--clear-halt",
        action="store_true",
        help="acknowledge saved halts after successful reconciliation and resume",
    )
    args = parser.parse_args()
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

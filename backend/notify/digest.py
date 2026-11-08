"""Scheduled digests + notify check (CI-fail / conflict / job-fail)."""

from __future__ import annotations

import json
import os
import threading
import time

from backend.notify.prefs_state import console_url
from backend.notify.prefs_state import enabled_events
from backend.notify.prefs_state import load_notifier_state
from backend.notify.prefs_state import notifier_lock
from backend.notify.prefs_state import save_notifier_state
from backend.notify.prefs_state import send_notification


def build_digest_text(summaries=None, total_repos=0):
    """Format a multi-repo digest. summaries: list of
    (name, pr_count, failing_count, conflict_count) with activity."""
    lines = []
    total_prs = 0
    failing_runs = 0
    for name, prs, failing, conflicts in (summaries or []):
        total_prs += prs
        failing_runs += failing
        if prs or failing or conflicts:
            lines.append(
                f"{name}: {prs} PRs, {failing} failing CI, "
                f"{conflicts} conflicts")
    if not lines:
        return (f"All {total_repos} repos clear — 0 open PRs, "
                "0 failing runs.")
    return (f"Summary across {total_repos} repos ({total_prs} PRs, "
            f"{failing_runs} failing CI):\n" + "\n".join(lines))


def digest_interval_hours(default=12.0):
    try:
        return float(os.environ.get("DIGEST_INTERVAL_HOURS", "12"))
    except ValueError:
        return default


# Scheduled briefing digests (#636). Cadence + next-run live in a
# JSON store beside the deferred file so the schedule survives
# restarts; firing rides the shared repair/digest tick in
# backend/api/routers/issues.py (single ticker — never add a second
# one here). Cadences are plain intervals from set time (Daily is
# ~24h, Weekly ~7d); there is no calendar alignment and no
# timezone math beyond the server wall clock.
DIGEST_SCHEDULE_INTERVALS = {"daily": 24 * 3600, "weekly": 7 * 24 * 3600}

DEFAULT_DIGEST_SCHEDULE_PATH = "/var/lib/buttons/digest_schedule.json"

digest_schedule_lock = threading.Lock()


def digest_schedule_path() -> str:
    """Current digest-schedule file path (env override read
    dynamically so tests can point it at tmp)."""
    return os.environ.get("BUTTONS_DIGEST_SCHEDULE_PATH",
                          DEFAULT_DIGEST_SCHEDULE_PATH)


def _default_digest_schedule() -> dict:
    return {"cadence": "off", "next_run": None, "last_sent": None,
            "last_result": {}}


def load_digest_schedule() -> dict:
    path = digest_schedule_path()
    try:
        if os.path.exists(path):
            with open(path) as f:
                state = json.load(f)
            if isinstance(state, dict):
                merged = _default_digest_schedule()
                merged.update(state)
                return merged
    except Exception:
        pass
    return _default_digest_schedule()


def save_digest_schedule(state: dict) -> None:
    path = digest_schedule_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def set_digest_cadence(cadence: str, now: float | None = None) -> dict:
    """Set the digest cadence; daily/weekly schedule from now, off
    clears the next run (send history is kept)."""
    if cadence not in DIGEST_SCHEDULE_INTERVALS and cadence != "off":
        raise ValueError("cadence must be daily, weekly, or off")
    at = time.time() if now is None else now
    with digest_schedule_lock:
        state = load_digest_schedule()
        state["cadence"] = cadence
        state["next_run"] = (None if cadence == "off"
                             else at + DIGEST_SCHEDULE_INTERVALS[cadence])
        save_digest_schedule(state)
        return dict(state)


def digest_schedule_status() -> dict:
    with digest_schedule_lock:
        return dict(load_digest_schedule())


def _digest_send_ok(results: object) -> bool:
    if not isinstance(results, dict) or not results:
        return False
    return all(isinstance(r, dict) and r.get("ok")
               for r in results.values())


def _digest_failure_text(results: object, fatal: str) -> str:
    if fatal:
        return fatal
    if isinstance(results, dict) and "suppressed" in results:
        return str(results["suppressed"])
    if not isinstance(results, dict) or not results:
        return "no notification channels configured"
    parts = []
    for name, res in results.items():
        if isinstance(res, dict) and res.get("ok"):
            continue
        detail = (res or {}).get("error") if isinstance(
            res, dict) else None
        parts.append(f"{name} failed ({detail or 'unknown error'})")
    return "; ".join(parts) or "unknown error"


def fire_due_digest(now: float | None = None) -> bool:
    """Send one due scheduled digest. Returns True when a send was
    attempted. Never raises: the shared tick must not die on a
    digest failure."""
    try:
        return _fire_due_digest(time.time() if now is None else now)
    except Exception:
        return False


def _fire_due_digest(now: float) -> bool:
    with digest_schedule_lock:
        state = load_digest_schedule()
        interval = DIGEST_SCHEDULE_INTERVALS.get(
            state.get("cadence", "off"))
        if interval is None:
            return False
        if (state.get("next_run") or 0) > now:
            return False
    with digest_schedule_lock:
        # Claim before sending so a slow send can't double-fire.
        state = load_digest_schedule()
        state["next_run"] = now + interval
        save_digest_schedule(state)
    try:
        results = send_notification(
            "Scheduled Briefing Digest", build_digest_text(),
            priority="low", tags=["clipboard", "digest"],
            event_type="digest",
            click_url=console_url("/api/briefing"))
        fatal = ""
    except Exception as exc:
        results, fatal = {}, str(exc)
    ok = _digest_send_ok(results)
    # Stamp the RESULT, never blindly (#532): last_sent moves only
    # on a confirmed send; last_result records every attempt.
    with digest_schedule_lock:
        state = load_digest_schedule()
        if ok:
            state["last_sent"] = now
        state["last_result"] = {
            "ok": ok, "at": now,
            "error": "" if ok else _digest_failure_text(
                results, fatal),
        }
        save_digest_schedule(state)
    return True


# gh field sets for the scheduled notify check (#822): the two
# cheapest calls that still cover both push paths. mergeStateStatus
# rides `pr list` directly (verified live), so conflicts need no
# per-PR view calls; DIRTY is the only conflict verdict, mirroring
# backend/briefing_summary.py (overlap scans never count).
NOTIFY_RUN_FIELDS = "name,headBranch,conclusion,event,url"
NOTIFY_PR_FIELDS = "number,title,mergeStateStatus,url"
NOTIFY_GH_TIMEOUT = 60


def collect_notify_brief(repo_name, gh, runner=None):
    """Fresh {failing_runs, prs} brief for one repo, or None.

    Two gh calls (failed runs + open PR merge states). Any failure —
    gh error, empty output, unparseable JSON — returns None
    (unknown), and check_notifications_and_digest skips unknown
    repos without touching their stored state: a failed fetch must
    never look like an all-clear, or recovery would push a storm of
    "new" failures. ``runner`` injects a subprocess-like module.
    """
    from backend.gh import run_gh_json

    try:
        runs = run_gh_json(
            gh, ["run", "list", "--repo", gh,
                 "--limit", "50", "--json", NOTIFY_RUN_FIELDS],
            timeout=NOTIFY_GH_TIMEOUT, runner=runner)
        rows = run_gh_json(
            gh, ["pr", "list", "--repo", gh, "--state", "open",
                 "--limit", "50", "--json", NOTIFY_PR_FIELDS],
            timeout=NOTIFY_GH_TIMEOUT, runner=runner)
    except Exception as exc:
        print(f"notify brief fetch failed for {repo_name}: {exc}",
              flush=True)
        return None
    if not isinstance(runs, list) or not isinstance(rows, list):
        return None
    failing_runs = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        # gh run list exposes no conclusion filter: recent runs are
        # fetched and failures picked client-side (capped at 10 so
        # one red week cannot bloat the stored keys).
        if run.get("conclusion") != "failure":
            continue
        if len(failing_runs) >= 10:
            break
        failing_runs.append({
            "name": run.get("name") or "?",
            "branch": run.get("headBranch") or "?",
            "failed": run.get("conclusion") or run.get("event")
            or "failure",
            "url": run.get("url") or "",
        })
    prs = []
    for pr in rows:
        if not isinstance(pr, dict):
            continue
        prs.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "conflicted": str(pr.get("mergeStateStatus") or "")
            .lower() == "dirty",
            "url": pr.get("url") or "",
        })
    return {"failing_runs": failing_runs, "prs": prs}


def delivered(results) -> bool:
    """True when a digest send earned its `last_digest` stamp (#532).

    The stamp advances only on actual delivery: at least one target
    must confirm (`ok`). Suppressed sends (muted class) never stamp,
    and neither do sends where every target failed — the next tick
    retries instead of the failure eating the next digest. Empty
    results (no channels configured) still stamp: there is nothing
    to retry, so holding the stamp would re-fire every tick.
    Shared by the manual trigger endpoint and the scheduled path so
    the rule stays consistent (#430 reuses it for ok-derivation).
    """
    if not isinstance(results, dict):
        return False
    if "suppressed" in results:
        return False
    targets = [r for r in results.values() if isinstance(r, dict)]
    if not targets:
        return True
    return any(r.get("ok") for r in targets)


def _read_registry_runs() -> list:
    """Finished-run records from the jobs run store (the same file
    GET /api/jobs lists). [] on any failure. The engine import is
    lazy so notify stays light for API/test importers; only the
    store path helper is used, never the live registry (this tick
    runs in a subprocess).
    """
    try:
        from backend.exec.engine import runs_store_path
        with open(runs_store_path(), encoding="utf-8") as f:
            data = json.load(f)
        runs = data.get("runs", [])
        return [r for r in runs if isinstance(r, dict)]
    except Exception:
        return []


def check_notifications_and_digest(fetcher=None, sender=None,
                                   now=None, runs_lister=None):
    """Port of _check_notifications_and_digest (CI-fail / conflict /
    job-fail / digest paths only).

    fetcher(repo_name) -> briefing dict (or None to skip); defaults to
    a skip (backend owns no briefing pipeline yet). sender defaults to
    send_notification. runs_lister defaults to the persisted jobs run
    store (injectable for tests). Returns the persisted state dict.
    """
    sender = sender or send_notification
    with notifier_lock:
        state = load_notifier_state()
        now = now if now is not None else time.time()
        notify_events = enabled_events()
        repos = fetcher() if fetcher else None
        briefs = {}
        if callable(getattr(fetcher, "briefs", None)):
            briefs = fetcher.briefs()  # type: ignore
        elif isinstance(repos, dict):
            briefs = repos
        registry_runs = []
        if "job_fail" in notify_events:
            try:
                lister = runs_lister or _read_registry_runs
                registry_runs = lister() or []
            except Exception:
                registry_runs = []

        for repo_name, brief in briefs.items():
            if brief is None:
                # Unknown (#822): the fetch failed for this repo, so
                # its stored ci/conflict keys stay untouched — neither
                # pushed nor cleared — until a good fetch lands.
                continue
            if not isinstance(brief, dict):
                continue
            if "ci_fail" in notify_events:
                frs = brief.get("failing_runs") or []
                current = {
                    f"{r.get('name')}:{r.get('branch')}": r.get("failed")
                    for r in frs if isinstance(r, dict)
                }
                run_urls = {
                    f"{r.get('name')}:{r.get('branch')}":
                    r.get("url") or None
                    for r in frs if isinstance(r, dict)
                }
                prev = state.get("ci", {}).get(repo_name, {})
                for run_key, failed_details in current.items():
                    if run_key not in prev:
                        sender(
                            title=f"CI Failed on {repo_name}",
                            message=(f"Run {run_key} failed.\nDetails: "
                                     f"{failed_details or 'check logs'}"),
                            priority="high",
                            tags=["warning", "ci"],
                            event_type="ci_fail",
                            click_url=run_urls.get(run_key),
                        )
                state.setdefault("ci", {})[repo_name] = current

            if "conflict" in notify_events:
                prs = brief.get("prs") or []
                current = {
                    str(p.get("number")): p.get("title")
                    for p in prs
                    if isinstance(p, dict) and p.get("conflicted")
                }
                pr_urls = {
                    str(p.get("number")): p.get("url") or None
                    for p in prs
                    if isinstance(p, dict) and p.get("conflicted")
                }
                prev = state.get("conflicts", {}).get(repo_name, {})
                for pr_num, pr_title in current.items():
                    if pr_num not in prev:
                        sender(
                            title=(f"Merge Conflict on "
                                   f"{repo_name} #{pr_num}"),
                            message=(f"PR #{pr_num} ({pr_title}) has new "
                                     "merge conflicts."),
                            priority="default",
                            tags=["warning", "git"],
                            event_type="conflict",
                            click_url=pr_urls.get(pr_num),
                        )
                state.setdefault("conflicts", {})[repo_name] = current

            if "job_fail" in notify_events:
                # Engine runs that finished nonzero (#866). Scoped to
                # this tick's repos (the schedule seeds one run per
                # repo — scanning all repos here would push N times).
                # Watermarked per repo+action: each failed run pushes
                # once; the scan marks seen even when the send is
                # suppressed, mirroring the ci/conflict legs.
                wm = state.setdefault("job_fail", {}).setdefault(
                    repo_name, {})
                for run in registry_runs:
                    if not isinstance(run, dict):
                        continue
                    if run.get("repo") != repo_name:
                        continue
                    if not run.get("done"):
                        continue
                    code = run.get("exit_code")
                    if code in (None, 0):
                        continue
                    ts = run.get("finished_at") or 0
                    if ts <= 0:
                        continue
                    action = run.get("action") or "run"
                    if ts <= wm.get(action, 0):
                        continue
                    wm[action] = ts
                    out = run.get("output")
                    tail = out.strip().splitlines() \
                        if isinstance(out, str) else []
                    last = tail[-1][-300:] if tail else "no output"
                    rid = run.get("id")
                    sender(
                        title=(f"Job failed: {action} on {repo_name} "
                               f"(exit {code})"),
                        message=(f"Run {run.get('id', '?')} finished "
                                 f"exit {code}.\nLast line: {last}"),
                        priority="high",
                        tags=["warning", "job"],
                        event_type="job_fail",
                        click_url=(console_url(
                            f"/api/jobs/runs/{rid}") if rid else None),
                    )

        interval = digest_interval_hours()
        if interval > 0:
            last_digest = state.get("last_digest", 0)
            if (now - last_digest) >= (interval * 3600):
                # Suppressed/failed sends never stamp (#532).
                res = sender(
                    title="Scheduled Briefing Digest",
                    message=build_digest_text(
                        summaries=briefs.get("_digest_")  # type: ignore
                        if isinstance(briefs, dict) else None,
                        total_repos=sum(
                            1 for k, v in briefs.items()
                            if k != "_digest_" and v is not None)
                        if isinstance(briefs, dict) else 0,
                    ),
                    priority="low",
                    tags=["clipboard", "digest"],
                    event_type="digest",
                    click_url=console_url("/api/briefing"),
                )
                if delivered(res):
                    state["last_digest"] = now

        save_notifier_state(state)
        return state

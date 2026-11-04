"""Cross-repo lane dispatch queue (issue #853).

Lanes used to serve only arii/remote-server: every other repo needed
hand-driven console agent sessions. This module keys the lane queue
by (repo, issue) so any discovered console repo flows
queue -> dispatch -> branch/PR/CI -> green -> merge from one view.

Reuse, not fork:
- execution goes through the repo-scoped repair worker
  (issues.trigger_repair), the same branch/PR flow the console
  already runs — never a second worker;
- issue parsing, verify-allowlist preview, and the worker
  single-flight guard are imported from the issues router;
- repo validation, checkout reads, and discovery come from
  backend.core; gh reads go through backend.gh.

Per-repo gates are read live at dispatch (never assumed):
checkout presence/cleanliness, worker idle, no other running lane
item for the same repo (per-repo conflict detection), the issue's
verify command allowlisted, and the repo's own branch protection /
required checks. remote-server keeps byte-identical behavior: it is
repo #1 of N, dispatched through the same worker as before.
"""

from __future__ import annotations

import json
import os
import threading
import time

# States for one (repo, issue) queue item.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

TERMINAL_STATES = frozenset({DONE, FAILED})

# Monitor budget for one dispatched item: the repair worker's own
# timeout plus margin for spawn + teardown. Past this the item
# records failed instead of wedging the queue.
DISPATCH_MONITOR_TIMEOUT = 2100
MONITOR_POLL_SECONDS = 5

# Durable queue store (#883). The queue used to be pure in-memory,
# so every console restart wiped queued/running items with their
# fates unknown (the #461 amnesia class). Item dicts persist to a
# JSON snapshot on the buttons-data volume; a fresh process
# rehydrates on first touch. Single-writer: one console owns the
# file (cross-replica sharing is an explicit non-goal).
DEFAULT_DISPATCH_QUEUE_PATH = "/var/lib/buttons/dispatch_queue.json"

# Snapshot keeps the newest items only (oldest-first eviction past
# the cap). Items are small dicts; the cap bounds the file, not RAM.
DISPATCH_QUEUE_MAX_ITEMS = 200

# Reconcile verdict for items marked running when the previous
# process died: the repair worker died with it, so the outcome is
# honestly unknown. Terminal failed, never silently re-queued: the
# operator retries explicitly after checking the issue.
RESTART_DETAIL = ("console restarted while this item was running; "
                  "outcome unknown — check the issue before retrying")


def queue_store_path() -> str:
    """Current queue-store file path (env override read dynamically
    so tests can point it at tmp)."""
    return os.environ.get("BUTTONS_DISPATCH_QUEUE_PATH",
                          DEFAULT_DISPATCH_QUEUE_PATH)


def issue_url(repo: str, issue: int) -> str:
    """One-tap outcome link for a queue item: the GitHub issue. No
    PR URL is recorded anywhere (the repair worker drops gh's
    output), so the issue — which cross-links its PRs — is the
    stable artifact link."""
    return f"https://github.com/arii/{repo}/issues/{issue}"


_lock = threading.Lock()
# (repo, issue) -> item dict. Durable via the snapshot below.
_items: dict[tuple[str, str], dict] = {}
# Store path whose snapshot is currently loaded (None before the
# first touch). Compared per call so tests that repoint the env
# mid-process reload instead of reading stale memory.
_loaded_path: str | None = None


def _now() -> float:
    return time.time()


def _key(repo: str, issue: int) -> tuple[str, str]:
    return (repo, str(issue))


def _copy(item: dict) -> dict:
    return dict(item)


def _persist_locked() -> None:
    """Write the queue snapshot atomically. Never raises: a store
    failure must not fail the mutation it records. Caller must hold
    _lock."""
    try:
        ordered = sorted(_items.values(),
                         key=lambda i: (i.get("added_at", 0),
                                        i.get("repo", ""),
                                        i.get("issue", 0)))
        snap = [dict(i)
                for i in ordered[-DISPATCH_QUEUE_MAX_ITEMS:]]
        path = queue_store_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": snap}, f)
        os.replace(tmp, path)
    except Exception as exc:
        print(f"dispatch queue persist failed: {exc}", flush=True)


def _load_locked() -> None:
    """Rehydrate the snapshot left by the previous process. Items
    still marked running died with it: record failed with the
    restart verdict instead of wedging or silently re-queueing.
    Missing/corrupt stores load as empty. Caller must hold _lock."""
    _items.clear()
    try:
        with open(queue_store_path(), encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return
    rows = state.get("items") if isinstance(state, dict) else None
    if not isinstance(rows, list):
        return
    now = _now()
    reconciled = False
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        repo = rec.get("repo")
        issue = rec.get("issue")
        if not repo or not isinstance(repo, str) \
                or not isinstance(issue, int) \
                or isinstance(issue, bool) or issue <= 0:
            continue
        added = rec.get("added_at")
        state_name = rec.get("state")
        if state_name not in (QUEUED, RUNNING, DONE, FAILED):
            state_name = QUEUED
        item = {
            "repo": repo,
            "issue": issue,
            "state": state_name,
            "added_at": added
            if isinstance(added, (int, float))
            and not isinstance(added, bool) else now,
            "started_at": rec.get("started_at"),
            "finished_at": rec.get("finished_at"),
            "detail": rec.get("detail")
            if isinstance(rec.get("detail"), str) else "",
            "title": rec.get("title")
            if isinstance(rec.get("title"), str) else "",
            "artifact_url": rec.get("artifact_url")
            if isinstance(rec.get("artifact_url"), str) else "",
        }
        if item["state"] == RUNNING:
            item["state"] = FAILED
            item["finished_at"] = now
            item["detail"] = RESTART_DETAIL
            reconciled = True
        _items[_key(repo, issue)] = item
    if reconciled:
        _persist_locked()


def _ensure_loaded_locked() -> None:
    """Load once per store path. Caller must hold _lock."""
    global _loaded_path
    path = queue_store_path()
    if _loaded_path == path:
        return
    _load_locked()
    _loaded_path = path


def reset_queue() -> None:
    """Drop every queued item. Tests only. The store file keeps its
    bytes: _loaded_path stays, so nothing resurrects until the path
    itself changes."""
    with _lock:
        _items.clear()


def enqueue(repo: str, issue, parse_number=None,
            validate_repo=None,
            title: str = "") -> tuple[dict | None, str | None]:
    """Queue (repo, issue) for lane dispatch. Idempotent: an already
    queued/running item is returned unchanged; a terminal item
    re-queues (retry). Returns (item, None) or (None, error).

    ``parse_number`` / ``validate_repo`` are injectable seams (the
    issues router + backend.core defaults keep one canonical parser
    and one canonical validator). ``title`` is the issue title
    enriched at enqueue from the issues read (#882, shared with
    #883); "" when the enqueuer has none (manual numbers).
    """
    if validate_repo is None:
        from backend.core import validate_repo_name as validate_repo
    if parse_number is None:
        from backend.api.routers.issues import \
            parse_issue_number as parse_number
    try:
        name = validate_repo(repo)
    except (ValueError, AttributeError, TypeError):
        return None, "invalid repo"
    number = parse_number(issue)
    if number is None:
        return None, "invalid issue"
    with _lock:
        _ensure_loaded_locked()
        key = _key(name, number)
        existing = _items.get(key)
        if existing is not None and \
                existing.get("state") not in TERMINAL_STATES:
            return _copy(existing), None
        item = {
            "repo": name,
            "issue": number,
            "state": QUEUED,
            "added_at": _now(),
            "started_at": None,
            "finished_at": None,
            "detail": "",
            "title": title if isinstance(title, str) else "",
            "artifact_url": issue_url(name, number),
        }
        _items[key] = item
        _persist_locked()
        return _copy(item), None


def get_item(repo: str, issue: int) -> dict | None:
    """Stored item copy, or None."""
    with _lock:
        _ensure_loaded_locked()
        item = _items.get(_key(repo, issue))
        return _copy(item) if item is not None else None


def queue_list() -> list[dict]:
    """Every queued item, oldest first. The console groups by repo."""
    with _lock:
        _ensure_loaded_locked()
        ordered = sorted(_items.values(),
                         key=lambda i: (i.get("added_at", 0),
                                        i.get("repo", ""),
                                        i.get("issue", 0)))
        return [_copy(i) for i in ordered]


def cancel(repo: str, issue: int) -> tuple[bool, str | None]:
    """Drop a queued (or terminal) item. A running item refuses with
    a 409-style reason — abort it through the repair worker, which
    owns the proc. Returns (removed, error)."""
    with _lock:
        _ensure_loaded_locked()
        key = _key(repo, issue)
        item = _items.get(key)
        if item is None:
            return False, "unknown item"
        if item.get("state") == RUNNING:
            return False, "item is running"
        del _items[key]
        _persist_locked()
        return True, None


def read_repo_gates(repo: str, gh_runner=None) -> dict:
    """Live per-repo merge gates: default branch + required status
    checks from branch protection. Never assumed, never cached:
    tech-dancer's rulesets differ from remote-server's, and stale
    gates would merge against the wrong bar. ``gh_runner`` injects
    tests. Returns {"ok", "base", "required_checks", "error"} — ok
    False keeps dispatch blocked with the reason visible.
    """
    from backend.core import validate_repo_name
    from backend.gh import repo_gh, run_gh_json

    try:
        name = validate_repo_name(repo)
        gh = repo_gh(name)
    except ValueError as exc:
        return {"ok": False, "base": "", "required_checks": [],
                "error": str(exc)}
    try:
        # gh api (not repo view): neither subcommand accepts --repo
        # (#865), and the repos/... URL satisfies the #544 guard.
        # Full repo JSON (not --jq: that emits raw text, which the
        # JSON loader cannot parse).
        info = run_gh_json(
            gh, ["api", f"repos/{gh}"], timeout=30, runner=gh_runner)
        base = (info or {}).get("default_branch") or "main"
    except Exception as exc:
        return {"ok": False, "base": "", "required_checks": [],
                "error": f"could not read default branch: {exc}"[:300]}
    try:
        prot = run_gh_json(
            gh, ["api", f"repos/{gh}/branches/{base}/protection"],
            timeout=30, runner=gh_runner)
        checks = ((prot or {}).get("required_status_checks") or {}).get(
            "contexts") or []
        if not isinstance(checks, list):
            checks = []
    except Exception:
        # Unprotected branch (or no permission to read protection):
        # no required checks, still dispatchable — the gate is the
        # repo's own bar, and an empty bar is a bar.
        checks = []
    return {"ok": True, "base": base,
            "required_checks": [str(c) for c in checks], "error": ""}


def readiness(repo: str, issue: int, repo_status=None,
              verify_preview=None, worker_busy=None,
              gh_runner=None) -> dict:
    """Per-repo dispatch gates for one item. Returns {"ready",
    "reasons", "gates"}: ready True only when every gate passes;
    reasons names each failing gate for the console. All live reads
    are injectable for tests; defaults are the canonical sources.
    """
    reasons: list[str] = []
    gates: dict = {}
    if repo_status is None:
        from backend.core import get_repo_status as repo_status
    if verify_preview is None:
        from backend.api.routers.issues import \
            _repair_verify_preview as verify_preview
    if worker_busy is None:
        def worker_busy() -> bool:
            from backend.api.routers import issues as issues_mod

            with issues_mod.repair_state_lock:
                return issues_mod.repair_state["status"] == "running"

    try:
        status = repo_status(repo)
        gates["checkout"] = {"branch": status.get("branch", ""),
                             "clean": bool(status.get("clean"))}
        if not status.get("clean"):
            reasons.append("checkout has uncommitted changes")
    except FileNotFoundError:
        gates["checkout"] = {"branch": "", "clean": False}
        reasons.append("no checkout for this repo (clone it first)")
    except (ValueError, RuntimeError) as exc:
        gates["checkout"] = {"branch": "", "clean": False}
        reasons.append(str(exc)[:200])

    with _lock:
        _ensure_loaded_locked()
        repo_running = [i for i in _items.values()
                        if i.get("repo") == repo
                        and i.get("state") == RUNNING
                        and i.get("issue") != issue]
    gates["repo_idle"] = len(repo_running) == 0
    if repo_running:
        reasons.append(f"repo already has a lane item running "
                       f"(#{repo_running[0]['issue']})")

    busy = worker_busy()
    gates["worker_idle"] = not busy
    if busy:
        reasons.append("repair worker is busy")

    try:
        preview = verify_preview(issue, repo)
        gates["verify"] = {"command": preview.get("command", ""),
                           "allowlisted": bool(
                               preview.get("allowlisted"))}
        if preview.get("command") and not preview.get("allowlisted"):
            reasons.append(f"verify command not allowlisted: "
                           f"{preview.get('error', '')}"[:200])
    except Exception as exc:
        gates["verify"] = {"command": "", "allowlisted": False}
        reasons.append(f"could not read verify command: {exc}"[:200])

    protection = read_repo_gates(repo, gh_runner=gh_runner)
    gates["protection"] = protection
    if not protection.get("ok"):
        reasons.append(protection.get("error", "could not read gates"))

    return {"ready": len(reasons) == 0, "reasons": reasons,
            "gates": gates}


def _monitor_item(repo: str, issue: int,
                  timeout: float = DISPATCH_MONITOR_TIMEOUT,
                  poll: float = MONITOR_POLL_SECONDS) -> None:
    """Watch the repair worker until this item's run settles, then
    record done/failed. Never raises out of the thread."""
    from backend.api.routers import issues as issues_mod

    deadline = time.monotonic() + timeout
    outcome = FAILED
    detail = "timed out waiting for the repair worker"
    try:
        while time.monotonic() < deadline:
            with issues_mod.repair_state_lock:
                status = issues_mod.repair_state.get("status")
                same = (issues_mod.repair_state.get("issue") == issue
                        and issues_mod.repair_state.get("repo") == repo)
            if status != "running" or not same:
                if status == "done" and same:
                    outcome = DONE
                    detail = "repair worker finished"
                elif status == "aborted" and same:
                    detail = "repair worker aborted"
                elif not same:
                    detail = "worker moved on to another item"
                else:
                    with issues_mod.repair_state_lock:
                        code = issues_mod.repair_state.get("exit_code")
                    detail = (f"repair worker failed"
                              f"{f' (exit {code})' if code is not None else ''}")
                break
            time.sleep(poll)
    except Exception as exc:  # pragma: no cover - defensive
        detail = f"monitor error: {exc}"[:200]
    with _lock:
        _ensure_loaded_locked()
        item = _items.get(_key(repo, issue))
        if item is not None and item.get("state") == RUNNING:
            item["state"] = outcome
            item["finished_at"] = _now()
            item["detail"] = detail
            _persist_locked()


def dispatch(repo: str, issue: int, launcher=None,
             monitor=None, **gate_seams) -> tuple[dict | None, str | None]:
    """Dispatch one queued item through the repair worker. The item
    auto-enqueues when absent; terminal items re-dispatch (retry).
    Gates are checked live — a blocked dispatch returns (None,
    reason) and changes nothing. Returns (item, None) once the
    worker is launched and the monitor watches it.

    ``launcher`` defaults to issues.trigger_repair; ``monitor``
    defaults to a daemon thread running _monitor_item. ``gate_seams``
    pass through to readiness (tests).
    """
    if launcher is None:
        from backend.api.routers.issues import trigger_repair as launcher
    item, err = enqueue(repo, issue)
    if err is not None or item is None:
        return None, err or "invalid item"
    name = item["repo"]
    number = item["issue"]
    with _lock:
        current = _items.get(_key(name, number))
        if current is not None and current.get("state") == RUNNING:
            return None, "item is already running"
    gates = readiness(name, number, **gate_seams)
    if not gates["ready"]:
        return None, "; ".join(gates["reasons"])
    with _lock:
        current = _items.get(_key(name, number))
        if current is None:  # pragma: no cover - defensive
            return None, "unknown item"
        if current.get("state") == RUNNING:
            return None, "item is already running"
        current["state"] = RUNNING
        current["started_at"] = _now()
        current["finished_at"] = None
        current["detail"] = ""
        snapshot = _copy(current)
        _persist_locked()
    try:
        launcher(number, name)
    except Exception as exc:
        with _lock:
            current = _items.get(_key(name, number))
            if current is not None:
                current["state"] = QUEUED
                current["detail"] = f"launch failed: {exc}"[:200]
                _persist_locked()
        return None, f"launch failed: {exc}"[:200]
    if monitor is None:
        thread = threading.Thread(target=_monitor_item,
                                  args=(name, number), daemon=True)
        thread.start()
    else:
        monitor(name, number)
    return snapshot, None

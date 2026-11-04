"""Notify helpers ported from buttons/server.py (webhook + notifications).

Security boundary: HMAC-SHA256 verification and the fail-closed
missing-secret behavior are preserved exactly. No backcompat shims,
no new behavior. Jules-related triggers were intentionally NOT ported
(Jules integration is retired); only CI-fail / conflict / digest
paths are carried over.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from backend import ledger

WEBHOOK_MAX_BODY = 10_000_000

webhook_state: dict = {}  # repo -> {"at": epoch, "ok": bool,
#   "detail": str, "advanced": bool, "observed": {...}}
webhook_state_lock = threading.Lock()
webhook_pull_lock = threading.Lock()  # serializes pulls, not status reads
webhook_pending: set = set()

NOTIFIER_STATE_PATH = os.environ.get(
    "BUTTONS_NOTIFIER_STATE_PATH", "/var/lib/buttons/notifier_state.json"
)
# RLock: the send gate reads prefs, and check_notifications calls the
# gated sender while holding this lock — a plain Lock would deadlock.
notifier_lock = threading.RLock()


def webhook_secret() -> str:
    return os.environ.get("WEBHOOK_SECRET", "")


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expect = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expect, header)


def load_notifier_state() -> dict:
    try:
        if os.path.exists(NOTIFIER_STATE_PATH):
            with open(NOTIFIER_STATE_PATH) as f:
                return json.load(f)
    except Exception as exc:
        print(f"notifier state load failed: {exc}", flush=True)
    return {"ci": {}, "conflicts": {}, "last_digest": 0}


def save_notifier_state(state: dict) -> None:
    try:
        d = os.path.dirname(NOTIFIER_STATE_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = NOTIFIER_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, NOTIFIER_STATE_PATH)
    except Exception as exc:
        print(f"notifier state save failed: {exc}", flush=True)


# Operator-togglable event classes (#656). `jules` is deliberately
# absent (dead: no sender emits it — a toggle would be a fake
# control); `test` always bypasses (explicit tap) and `info` is an
# unused default, so neither is mutable.
MUTABLE_EVENTS = ("ci_fail", "conflict", "digest")

QUIET_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_muted_events(value) -> list:
    """Cleaned muted list, or ValueError naming the offender."""
    if not isinstance(value, list):
        raise ValueError("muted_events must be a list")
    for name in value:
        if name not in MUTABLE_EVENTS:
            raise ValueError(
                f"unknown event '{name}': must be one of "
                f"{list(MUTABLE_EVENTS)}")
    return sorted(set(value))


def validate_quiet_hours(value):
    """Cleaned {start, end} HH:MM pair, None (cleared), or ValueError."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("quiet_hours must be {start, end} or null")
    start = value.get("start", "")
    end = value.get("end", "")
    for label, text in (("start", start), ("end", end)):
        if not isinstance(text, str) or not QUIET_TIME_RE.match(text):
            raise ValueError(
                f"quiet_hours.{label} must be HH:MM (00:00-23:59)")
    if start == end:
        raise ValueError("quiet_hours start and end must differ")
    return {"start": start, "end": end}


def get_notification_prefs() -> dict:
    """Operator prefs from notifier state (never raises: corrupt
    rows fall back to unmuted/no-quiet)."""
    with notifier_lock:
        state = load_notifier_state()
    try:
        muted = validate_muted_events(state.get("muted_events", []))
    except ValueError:
        muted = []
    try:
        quiet = validate_quiet_hours(state.get("quiet_hours"))
    except ValueError:
        quiet = None
    return {"muted_events": muted, "quiet_hours": quiet}


_UNSET = object()


def set_notification_prefs(muted_events=_UNSET, quiet_hours=_UNSET) -> dict:
    """Validated prefs write (raises ValueError). _UNSET fields are
    left unchanged; quiet_hours=None clears the window."""
    with notifier_lock:
        state = load_notifier_state()
        if muted_events is not _UNSET:
            state["muted_events"] = validate_muted_events(muted_events)
        if quiet_hours is not _UNSET:
            state["quiet_hours"] = validate_quiet_hours(quiet_hours)
        save_notifier_state(state)
    return get_notification_prefs()


def in_quiet_hours(now=None, quiet=None) -> bool:
    """True when server-local wall time falls in the quiet window
    (inclusive start, exclusive end, overnight-aware)."""
    if quiet is None:
        quiet = get_notification_prefs()["quiet_hours"]
    if not quiet:
        return False
    if now is None:
        now = time.time()
    mins = time.localtime(now).tm_hour * 60 + time.localtime(now).tm_min
    start = int(quiet["start"][:2]) * 60 + int(quiet["start"][3:])
    end = int(quiet["end"][:2]) * 60 + int(quiet["end"][3:])
    if start <= end:
        return start <= mins < end
    return mins >= start or mins < end


def quiet_window_end(now, quiet) -> float:
    """Epoch seconds of the current quiet window's end (server-local;
    DST transitions may shift it by an hour — acceptable for a
    hold-until hint). now must fall inside quiet."""
    local = time.localtime(now)
    end_h, end_m = int(quiet["end"][:2]), int(quiet["end"][3:])
    midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                            0, 0, 0, 0, 0, -1))
    end_today = midnight + end_h * 3600 + end_m * 60
    if end_today > now:
        return float(end_today)
    return float(end_today + 86400)


def send_notification(title, message, priority="default", tags=None,
                      click_url=None, event_type="info", *,
                      bypass_quiet=False):
    """Dispatch a notification via ntfy and/or generic webhook if configured.

    Gate first (#656): muted event classes and quiet hours suppress
    the send with {"suppressed": reason} (never an error, never a
    blind stamp). bypass_quiet is for explicit operator taps (test +
    manual digest): it skips the time gate only — a muted class stays
    muted even when tapped.
    """
    prefs = get_notification_prefs()
    if event_type in prefs["muted_events"]:
        return {"suppressed": f"event '{event_type}' muted"}
    if not bypass_quiet and prefs["quiet_hours"] and in_quiet_hours(
            quiet=prefs["quiet_hours"]):
        return {"suppressed":
                f"quiet hours until {prefs['quiet_hours']['end']}"}
    results = {}
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    ntfy_token = os.environ.get("NTFY_TOKEN", "").strip()
    webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "").strip()

    if ntfy_topic:
        try:
            target = f"{ntfy_url}/{ntfy_topic}"
            req = urllib.request.Request(
                target, data=message.encode("utf-8"), method="POST")
            req.add_header("Title", title)
            if priority:
                req.add_header("Priority", priority)
            if tags:
                req.add_header(
                    "Tags",
                    ",".join(tags) if isinstance(tags, list) else str(tags))
            if click_url:
                req.add_header("Click", click_url)
            if ntfy_token:
                req.add_header("Authorization", f"Bearer {ntfy_token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                results["ntfy"] = {"ok": True, "status": resp.status}
        except Exception as exc:
            print(f"ntfy push failed: {exc}", flush=True)
            results["ntfy"] = {"ok": False, "error": str(exc)}

    if webhook_url:
        try:
            payload = json.dumps({
                "event": event_type,
                "title": title,
                "message": message,
                "url": click_url,
                "timestamp": time.time(),
            }).encode("utf-8")
            req = urllib.request.Request(
                webhook_url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                results["webhook"] = {"ok": True, "status": resp.status}
        except Exception as exc:
            print(f"webhook notification failed: {exc}", flush=True)
            results["webhook"] = {"ok": False, "error": str(exc)}

    return results


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


def enabled_events():
    return [e.strip() for e in
            os.environ.get("NOTIFY_EVENTS", "ci_fail,jules,conflict")
            .split(",") if e.strip()]


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
            "last_result": {}, "held_until": None}


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
    # Due inside quiet hours: hold, don't send. next_run stays due
    # so the first tick past the window fires exactly once
    # (coalesced, never dropped). held_until shows the wait.
    quiet = get_notification_prefs()["quiet_hours"]
    if quiet and in_quiet_hours(now, quiet):
        with digest_schedule_lock:
            state = load_digest_schedule()
            state["held_until"] = quiet_window_end(now, quiet)
            save_digest_schedule(state)
        return False
    with digest_schedule_lock:
        # Claim before sending so a slow send can't double-fire.
        state = load_digest_schedule()
        state.pop("held_until", None)
        state["next_run"] = now + interval
        save_digest_schedule(state)
    try:
        results = send_notification(
            "Scheduled Briefing Digest", build_digest_text(),
            priority="low", tags=["clipboard", "digest"],
            event_type="digest")
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


# Budget watch (#652). Spend caps fail silently: GET
# /agents/budget shows cap/spent/blocked but nothing pushes when the
# cap is nearly hit or hit. This job rides the shared repair/digest
# tick in backend/api/routers/issues.py (a third job, not a ticker)
# and edge-fires one warning push at 80% and one blocked push at
# 100% of the metered cap per cap cycle.
BUDGET_WARN_RATIO = 0.8
BUDGET_BLOCK_RATIO = 1.0
BUDGET_BAND_SEVERITY = {"warning": 1, "blocked": 2}


def _budget_cap_spend():
    """(cap, spent) via the same modules as GET /agents/budget.

    Lazy import: runs.py pulls deep-agent-local modules (cli, ledger,
    pricing) that need the deep-agent dir on sys.path. Never raises:
    an unresolvable read reports uncapped (None) so a broken read
    stays silent instead of paging anyone.
    """
    try:
        import sys
        deep_agent = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "deep-agent")
        if deep_agent not in sys.path:
            sys.path.insert(0, deep_agent)
        import pricing
        import runs
        cap = pricing.spend_cap()
        spent = runs.total_metered_spend()["spent"]
        return cap, spent
    except Exception:
        return None, 0.0


def _budget_band(cap, spent):
    """Current alert band: None under 80%, "warning" at 80-100%,
    "blocked" at/over cap. cap <= 0 blocks everything, mirroring
    the spend-cap gate (0 blocks every metered call)."""
    if cap is None:
        return None
    if cap <= 0:
        return "blocked"
    ratio = max(0.0, spent) / cap
    if ratio >= BUDGET_BLOCK_RATIO:
        return "blocked"
    if ratio >= BUDGET_WARN_RATIO:
        return "warning"
    return None


def _budget_send_ok(results) -> bool:
    """True when at least one channel confirmed delivery. ANY-ok
    (digests demand all-ok): one confirmed push is enough for an
    edge alert — retrying every tick because a second channel is
    down would re-push the working one."""
    if not isinstance(results, dict) or not results:
        return False
    if "suppressed" in results:
        return False
    return any(isinstance(r, dict) and r.get("ok")
               for r in results.values())


def _usd(value: float) -> str:
    """$ with cents normally, micro precision for dust."""
    if abs(value) >= 0.01:
        return f"${value:,.2f}"
    return f"${value:,.6f}"


def fire_budget_watch(now: float | None = None, sender=None) -> bool:
    """Edge-fire budget alerts. Returns True when a send was
    attempted. Never raises: the shared tick must not die on a
    budget-watch failure."""
    try:
        return _fire_budget_watch(
            time.time() if now is None else now,
            sender or send_notification)
    except Exception:
        return False


def _fire_budget_watch(now: float, sender) -> bool:
    cap, spent = _budget_cap_spend()
    band = _budget_band(cap, spent)
    with notifier_lock:
        state = load_notifier_state()
        prev = (state.get("last_budget_alert") or {}).get("band")
    prev_sev = BUDGET_BAND_SEVERITY.get(prev, 0)
    if band is None:
        # Below every threshold: silently re-arm (drop the stamp so
        # the next crossing fires again, e.g. after a cap raise).
        if prev is not None:
            with notifier_lock:
                state = load_notifier_state()
                state.pop("last_budget_alert", None)
                save_notifier_state(state)
        return False
    if BUDGET_BAND_SEVERITY[band] <= prev_sev:
        # Same band: no repeat. Downward move (cap raised, spend
        # cleared): silently re-arm to the lower band so a later
        # re-crossing fires again. Drops never push — a "warning"
        # arriving right after "blocked" would be noise.
        if band != prev:
            with notifier_lock:
                state = load_notifier_state()
                state["last_budget_alert"] = {"band": band, "at": now}
                save_notifier_state(state)
        return False
    remaining = max(0.0, cap - spent)
    if band == "blocked":
        title = "Spend cap reached — metered calls blocked"
        message = (
            f"{_usd(spent)} of {_usd(cap)} cap spent. "
            "The next metered call will refuse until the cap is "
            "raised or spend is cleared.")
        priority = "high"
    else:
        title = "Spend cap 80% used"
        message = (
            f"{_usd(spent)} of {_usd(cap)} cap spent "
            f"({_usd(remaining)} remaining). "
            "Metered calls will be refused at 100%.")
        priority = "default"
    try:
        results = sender(title, message, priority=priority,
                         tags=["warning", "budget"],
                         event_type="budget")
    except Exception:
        # Failed attempt: no stamp, so the next tick retries (one
        # attempt per tick max). An edge alert is one-shot — losing
        # it on a transient failure would silence it forever.
        return True
    if isinstance(results, dict) and "suppressed" in results:
        # Quiet-held (#532: suppressed sends never stamp): the next
        # tick retries, so the alert waits out quiet hours instead
        # of being dropped.
        return True
    if _budget_send_ok(results):
        with notifier_lock:
            state = load_notifier_state()
            state["last_budget_alert"] = {"band": band, "at": now}
            save_notifier_state(state)
    return True


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


def check_notifications_and_digest(fetcher=None, sender=None,
                                   now=None):
    """Port of _check_notifications_and_digest minus the Jules block.

    fetcher(repo_name) -> briefing dict (or None to skip); defaults to
    a skip (backend owns no briefing pipeline yet). sender defaults to
    send_notification. Returns the persisted state dict.
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

        for repo_name, brief in briefs.items():
            if not isinstance(brief, dict):
                continue
            if "ci_fail" in notify_events:
                frs = brief.get("failing_runs") or []
                current = {
                    f"{r.get('name')}:{r.get('branch')}": r.get("failed")
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
                        )
                state.setdefault("ci", {})[repo_name] = current

            if "conflict" in notify_events:
                prs = brief.get("prs") or []
                current = {
                    str(p.get("number")): p.get("title")
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
                        )
                state.setdefault("conflicts", {})[repo_name] = current

        interval = digest_interval_hours()
        if interval > 0:
            last_digest = state.get("last_digest", 0)
            if (now - last_digest) >= (interval * 3600):
                # Quiet holds the legacy digest too: leave the stamp
                # so the first tick past the window fires (coalesced).
                # Suppressed/failed sends never stamp (#532).
                if in_quiet_hours(now):
                    pass
                else:
                    res = sender(
                        title="Scheduled Briefing Digest",
                        message=build_digest_text(
                            summaries=briefs.get("_digest_")  # type: ignore
                            if isinstance(briefs, dict) else None,
                            total_repos=(len(briefs) - ("_digest_" in briefs))
                            if isinstance(briefs, dict) else 0,
                        ),
                        priority="low",
                        tags=["clipboard", "digest"],
                        event_type="digest",
                    )
                    if delivered(res):
                        state["last_digest"] = now

        save_notifier_state(state)
        return state


def _git_base(path):
    """git argv prefix for the pull path: quarantined hooks, explicit
    safe.directory. Observe/advance only — never rebuilds, never
    installs deps, never runs hooks (core.hooksPath=/dev/null)."""
    return ["git", "-c", f"safe.directory={path}", "-C", path,
            "-c", "core.hooksPath=/dev/null"]


def webhook_observe(path):
    """Observe-only remote state: fetch origin, record origin/main +
    HEAD positions and lag. Never writes to the tree: no merge, no
    checkout, no stash, no reset — safe on dirty/diverged checkouts.

    Returns {"ok", "detail", "origin", "head", "lag", "ahead"}: lag
    is commits behind origin/main (HEAD..origin/main), ahead is
    local-only commits (origin/main..HEAD). ok=False means the fetch
    itself failed and no remote state was observed.
    """
    git = _git_base(path)
    env = dict(os.environ)
    observed = {"ok": False, "detail": "", "origin": None,
                "head": None, "lag": None, "ahead": None}
    try:
        observed["head"] = subprocess.run(
            git + ["rev-parse", "HEAD"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, text=True,
            env=env).stdout.strip() or None
    except Exception:
        observed["head"] = None
    fetch = subprocess.run(
        git + ["fetch", "origin"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=120, text=True, env=env)
    if fetch.returncode != 0:
        observed["detail"] = (
            (fetch.stderr or "").strip().splitlines()[-1]
            if (fetch.stderr or "").strip() else "fetch failed")
        return observed
    try:
        observed["origin"] = subprocess.run(
            git + ["rev-parse", "origin/main"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True, env=env).stdout.strip() or None
    except Exception:
        observed["origin"] = None
    for key, rev_range in (("lag", "HEAD..origin/main"),
                           ("ahead", "origin/main..HEAD")):
        try:
            out = subprocess.run(
                git + ["rev-list", "--count", rev_range],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True, env=env).stdout.strip()
            observed[key] = int(out)
        except Exception:
            observed[key] = None
    observed["ok"] = True
    observed["detail"] = "observed"
    return observed


def _skip_detail(reason, observed):
    """Skip detail with fresh lag: 'skipped: <reason> — behind by N
    (local changes)'. Falls back to the bare reason when lag is
    unknown. Always starts with 'skipped' (attention matcher)."""
    lag = (observed or {}).get("lag")
    if isinstance(lag, bool) or not isinstance(lag, int):
        return f"skipped: {reason}"
    return f"skipped: {reason} \u2014 behind by {lag} (local changes)"


def webhook_pull(path):
    """Pull-only auto-pull: observe remote state, then fast-forward.

    Observe (fetch + record origin/main and HEAD positions + lag)
    always runs, even on dirty/diverged trees; advance (merge
    --ff-only) runs only when status --porcelain is empty AND HEAD is
    an ancestor of origin/main. Never touches local modifications: no
    stash, no reset, no checkout, no branch changes.

    Never rebuilds, never installs deps, never runs hooks
    (core.hooksPath=/dev/null). Returns (ok, detail, advanced,
    observed): observed is the webhook_observe dict — remote state
    fresh even when the local tree did not move.
    """
    git = _git_base(path)
    env = dict(os.environ)
    observed = {"ok": False, "detail": "", "origin": None,
                "head": None, "lag": None, "ahead": None}
    with webhook_pull_lock:
        try:
            observed = webhook_observe(path)
            if not observed.get("ok"):
                raise RuntimeError(
                    observed.get("detail") or "fetch failed")
            old_head = observed.get("head")
            ancestor = subprocess.run(
                git + ["merge-base", "--is-ancestor",
                       "HEAD", "origin/main"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True, env=env)
            if ancestor.returncode == 1:
                # Mid-work checkout: quiet skip, not a failure alert.
                # Remote state was still observed (fetch + lag above).
                return True, _skip_detail(
                    "tree not ff-able", observed)[:200], False, observed
            tree = subprocess.run(
                git + ["status", "--porcelain"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True, env=env)
            if (tree.stdout or "").strip():
                return True, _skip_detail(
                    "tree dirty", observed)[:200], False, observed
            merge = subprocess.run(
                git + ["merge", "--ff-only", "origin/main"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=120, text=True, env=env)
            if merge.returncode != 0:
                raise RuntimeError(
                    (merge.stderr or "").strip().splitlines()[-1]
                    if merge.stderr.strip() else "merge failed")
            out = (merge.stdout or "").strip().splitlines()
            ok, detail = True, (out[-1] if out else "up to date")[:200]
            try:
                new_head = subprocess.run(
                    git + ["rev-parse", "HEAD"], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=30, text=True,
                    env=env).stdout.strip() or None
            except Exception:
                new_head = None
            advanced = bool(old_head and new_head
                            and old_head != new_head)
        except Exception as exc:
            ok, detail, advanced = False, str(exc)[:200], False
    return ok, detail, advanced, observed


def webhook_worker(repo, path):
    try:
        ok, detail, advanced, observed = webhook_pull(path)
    except Exception as exc:  # never stick pending: coalesce would
        ok, detail, advanced = False, f"worker crashed: {exc}", False
        observed = None
    with webhook_state_lock:
        webhook_state[repo] = {"at": time.time(), "ok": ok,
                               "detail": detail, "advanced": advanced,
                               "observed": observed}
        webhook_pending.discard(repo)
    # Every outcome hits the ledger: successes resolve the attention
    # incidents that failures raise. Skips carry fresh observed
    # positions + lag so lagging checkouts never go stale. append()
    # never raises.
    ledger.append({"kind": "webhook", "repo": repo, "ok": ok,
                   "advanced": advanced, "detail": detail[:200],
                   "observed": observed})
    print(f"webhook pull {repo}: {'ok' if ok else 'FAILED'}: {detail}",
          flush=True)

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
import subprocess
import threading
import time
import urllib.request

WEBHOOK_MAX_BODY = 10_000_000

webhook_state: dict = {}  # repo -> {"at": epoch, "ok": bool, "detail": str}
webhook_state_lock = threading.Lock()
webhook_pull_lock = threading.Lock()  # serializes pulls, not status reads
webhook_pending: set = set()

NOTIFIER_STATE_PATH = os.environ.get(
    "BUTTONS_NOTIFIER_STATE_PATH", "/var/lib/buttons/notifier_state.json"
)
notifier_lock = threading.Lock()


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


def send_notification(title, message, priority="default", tags=None,
                      click_url=None, event_type="info"):
    """Dispatch a notification via ntfy and/or generic webhook if configured."""
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
                sender(
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
                state["last_digest"] = now

        save_notifier_state(state)
        return state


def webhook_pull(path):
    """Pull-only auto-pull: fetch + fast-forward merge of origin/main.

    Never rebuilds, never installs deps, never runs hooks
    (core.hooksPath=/dev/null). Returns (ok, detail, advanced).
    """
    git = ["git", "-c", f"safe.directory={path}", "-C", path,
           "-c", "core.hooksPath=/dev/null"]
    env = dict(os.environ)
    with webhook_pull_lock:
        try:
            old_head = subprocess.run(
                git + ["rev-parse", "HEAD"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=30, text=True,
                env=env).stdout.strip() or None
        except Exception:
            old_head = None
        try:
            fetch = subprocess.run(
                git + ["fetch", "origin"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120, text=True, env=env)
            if fetch.returncode != 0:
                raise RuntimeError(
                    (fetch.stderr or "").strip().splitlines()[-1]
                    if fetch.stderr.strip() else "fetch failed")
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
    return ok, detail, advanced


def webhook_worker(repo, path):
    ok, detail, advanced = webhook_pull(path)
    with webhook_state_lock:
        webhook_state[repo] = {"at": time.time(), "ok": ok,
                               "detail": detail, "advanced": advanced}
        webhook_pending.discard(repo)
    print(f"webhook pull {repo}: {'ok' if ok else 'FAILED'}: {detail}",
          flush=True)

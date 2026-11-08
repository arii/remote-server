"""Operator prefs/state + notification send gate."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

NOTIFIER_STATE_PATH = os.environ.get(
    "BUTTONS_NOTIFIER_STATE_PATH", "/var/lib/buttons/notifier_state.json"
)
# RLock: the send gate reads prefs, and check_notifications calls the
# gated sender while holding this lock — a plain Lock would deadlock.
notifier_lock = threading.RLock()


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


# Operator-togglable event classes (#656). `test` always bypasses
# (explicit tap) and `info` is an unused default, so neither is
# mutable.
MUTABLE_EVENTS = ("ci_fail", "conflict", "digest", "job_fail")


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


def get_notification_prefs() -> dict:
    """Operator prefs from notifier state (never raises: a corrupt
    row falls back to unmuted). A stale hold-window key left in
    the store by the retired feature (#846) is ignored, never read."""
    with notifier_lock:
        state = load_notifier_state()
    try:
        muted = validate_muted_events(state.get("muted_events", []))
    except ValueError:
        muted = []
    return {"muted_events": muted}


_UNSET = object()


def set_notification_prefs(muted_events=_UNSET) -> dict:
    """Validated prefs write (raises ValueError). _UNSET fields are
    left unchanged."""
    with notifier_lock:
        state = load_notifier_state()
        if muted_events is not _UNSET:
            state["muted_events"] = validate_muted_events(muted_events)
        save_notifier_state(state)
    return get_notification_prefs()


def console_base_url() -> str:
    """Public console base URL for deep links, "" when unconfigured.

    Empty (unset BUTTONS_PUBLIC_URL) means gh-links-only: legs that
    have no gh canonical object send today's plain text.
    """
    return os.environ.get("BUTTONS_PUBLIC_URL", "").strip().rstrip("/")


def console_url(path: str) -> str | None:
    """Absolute console URL for path, or None when unconfigured."""
    base = console_base_url()
    if not base:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def send_notification(title, message, priority="default", tags=None,
                      click_url=None, event_type="info"):
    """Dispatch a notification via ntfy and/or generic webhook if configured.

    Gate first (#656): a muted event class suppresses the send with
    {"suppressed": reason} (never an error, never a blind stamp).
    Otherwise sends fire on schedule regardless of time of day (#846).
    click_url (#891) rides ntfy's Click header + the webhook url
    field, and appends a Link: line to the body so consumers that
    ignore both still get something tappable. Absent URL = today's
    text, byte-identical.
    """
    prefs = get_notification_prefs()
    if event_type in prefs["muted_events"]:
        return {"suppressed": f"event '{event_type}' muted"}
    results = {}
    body = message + (f"\nLink: {click_url}" if click_url else "")
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    ntfy_token = os.environ.get("NTFY_TOKEN", "").strip()
    webhook_url = os.environ.get("NOTIFICATION_WEBHOOK_URL", "").strip()

    if ntfy_topic:
        try:
            target = f"{ntfy_url}/{ntfy_topic}"
            req = urllib.request.Request(
                target, data=body.encode("utf-8"), method="POST")
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
                "message": body,
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


def enabled_events():
    return [e.strip() for e in
            os.environ.get("NOTIFY_EVENTS", "ci_fail,conflict,job_fail")
            .split(",") if e.strip()]

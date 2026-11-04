"""Durable audit ledger (append-only JSONL).

Data-compatible port of the ledger in buttons/server.py: same file
(/var/lib/buttons/ledger.jsonl on the buttons-data volume, overridable
via BUTTONS_LEDGER_PATH), same line format (one JSON object per line
with an injected ``ts`` epoch, keys sorted), same prune policy, same
newest-first kind-filtered reads, and the same clear-with-tombstone
semantics. FILE FORMAT IS IDENTICAL — data, not code, compat.

Kind allowlist covers terminal/td + buttons action kinds plus the
interactive agent kind. There is deliberately no Jules-session kind:
Jules dies post-Nov-11 and no Jules-session ledger events are
recorded here.
"""

import json
import os
import threading
import time

DEFAULT_LEDGER_PATH = "/var/lib/buttons/ledger.jsonl"
LEDGER_MAX_BYTES = 2_000_000
LEDGER_PRUNE_LINES = 2000

LEDGER_KINDS = (
    "run",
    "webhook",
    "pr-action",
    "system",
    "job",
    "repair",
    "chat",
    "td",
    "terminal",
    "agent",
)

ledger_lock = threading.Lock()


def ledger_path() -> str:
    """Current ledger file path (env override read dynamically so tests
    can point at a tmp dir without import tricks)."""
    return os.environ.get("BUTTONS_LEDGER_PATH", DEFAULT_LEDGER_PATH)


def append(entry: dict) -> None:
    """Append one JSON line to the ledger. Never raises: a failed
    append must not fail the action it records — the ledger is
    audit, not gate."""
    try:
        line = json.dumps({"ts": time.time(), **entry},
                          sort_keys=True) + "\n"
        with ledger_lock:
            path = ledger_path()
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(path) \
                    and os.path.getsize(path) > LEDGER_MAX_BYTES:
                with open(path) as f:
                    lines = f.readlines()
                with open(path, "w") as f:
                    f.writelines(lines[-LEDGER_PRUNE_LINES:])
            with open(path, "a") as f:
                f.write(line)
    except Exception as exc:  # ledger failure must not fail actions
        print(f"ledger append failed: {exc}", flush=True)


def read(kind: str = "all", limit: int = 50,
         since: float = 0) -> list:
    """Newest-first entries, kind-filtered. [] on any failure.
    Unparseable lines are skipped, the rest are kept. since > 0
    drops entries with numeric ts older than since (before the
    limit slice); entries without a numeric ts are kept — their
    age can't be judged."""
    try:
        with open(ledger_path()) as f:
            lines = f.readlines()
    except Exception:
        return []
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue  # skip unparseable lines, keep the rest
        if not isinstance(e, dict):
            continue
        if kind != "all" and e.get("kind") != kind:
            continue
        ts = e.get("ts")
        if since > 0 and isinstance(ts, (int, float)) \
                and not isinstance(ts, bool) and ts < since:
            continue
        entries.append(e)
    return entries[-limit:][::-1]


def clear() -> bool:
    """Truncate the ledger, then leave a tombstone entry so the clear
    itself stays on the record. Without the tombstone, clearing would
    erase evidence of its own act. The append runs outside the
    truncate lock: append takes the same lock, and holding it across
    the call would deadlock. Returns True on success."""
    try:
        with ledger_lock:
            path = ledger_path()
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            open(path, "w").close()
    except Exception as exc:
        print(f"ledger clear failed: {exc}", flush=True)
        return False
    append({"kind": "system", "event": "ledger cleared"})
    return True

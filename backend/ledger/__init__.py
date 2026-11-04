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
    "cli-turn",
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
         since: float = 0, source: str = "all") -> list:
    """Newest-first entries, kind-filtered. [] on any failure.
    Unparseable lines are skipped, the rest are kept. since > 0
    drops entries with numeric ts older than since (before the
    limit slice); entries without a numeric ts are kept — their
    age can't be judged.

    source selects the ledgers: "console" reads the backend file
    exactly as before, "cli" reads only normalized CLI turns, and
    "all" (default) merges both newest-first by ts with the limit
    applied after the merge. A missing/unparseable CLI file simply
    contributes nothing — console rows are never affected. When no
    CLI rows survive, the console path keeps its legacy file-order
    reversal untouched."""
    if source == "cli":
        rows = _read_cli_rows(kind, since)
        rows.sort(key=_ts_sort_key, reverse=True)
        return rows[:limit]
    console = _read_console_rows(kind, since)
    if source == "console":
        return console[-limit:][::-1]
    cli = _read_cli_rows(kind, since)
    if not cli:
        return console[-limit:][::-1]
    merged = console + cli
    merged.sort(key=_ts_sort_key, reverse=True)
    return merged[:limit]


def _read_console_rows(kind: str, since: float) -> list:
    try:
        with ledger_lock:
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
        if _dropped_by_since(e.get("ts"), since):
            continue
        entries.append(e)
    return entries


def _dropped_by_since(ts, since: float) -> bool:
    return (since > 0 and isinstance(ts, (int, float))
            and not isinstance(ts, bool) and ts < since)


def _ts_sort_key(entry: dict) -> tuple:
    """Newest-first merge key; rows without a numeric ts sink last
    (their age can't be judged, same as the read() contract)."""
    ts = entry.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return (1, ts)
    return (0, 0)


def cli_ledger_path() -> str:
    """CLI-ledger path, mirroring deep-agent/ledger.py's resolution
    (EVAL_LEDGER > DEEP_AGENT_STATE_DIR > module-adjacent default).
    Duplicated rather than imported: deep-agent/ is a hyphenated dir
    whose sibling imports need sys.path games (see
    test_cli_path_parity_with_deep_agent, which pins the two)."""
    direct = os.environ.get("EVAL_LEDGER")
    if direct:
        return direct
    state = os.environ.get("DEEP_AGENT_STATE_DIR")
    if state:
        return os.path.join(state, "ledger.jsonl")
    here = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return os.path.join(root, "deep-agent", "ledger.jsonl")


def _normalize_cli_turn(entry: dict):
    """deep-agent-run row -> LedgerList-shaped cli-turn row, or None
    for rows of any other kind. No ok/status mapping exists: the CLI
    schema records no outcome field."""
    if entry.get("kind") != "deep-agent-run":
        return None
    usage = entry.get("usage")
    if not isinstance(usage, dict):
        usage = None
    calls = entry.get("tool_calls") or []
    tools = [c.get("name") for c in calls
             if isinstance(c, dict) and c.get("name")]
    return {
        "kind": "cli-turn",
        "ts": entry.get("ts"),
        "script": entry.get("script"),
        "model": entry.get("model"),
        "thread_id": entry.get("thread_id"),
        "duration_s": entry.get("duration_s"),
        "usage": usage,
        "cost_usd": entry.get("cost_usd"),
        "metered": entry.get("metered"),
        "tools": tools,
    }


def _read_cli_rows(kind: str, since: float) -> list:
    if kind != "all" and kind != "cli-turn":
        return []
    try:
        with open(cli_ledger_path()) as f:
            lines = f.readlines()
    except Exception:
        return []
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        row = _normalize_cli_turn(e)
        if row is None:
            continue
        if _dropped_by_since(row.get("ts"), since):
            continue
        entries.append(row)
    return entries


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

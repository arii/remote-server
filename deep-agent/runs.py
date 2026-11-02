"""Run listing + detail reader over ledger.jsonl + traces/ (deep-agent/).

Backend-agnostic read API behind the /api/agents* redirect: list agent
runs newest-first with stable ids, fetch one run with its trace events.
No Pi data, no backwards compat. Stdlib-only (system-python tests).

Ids are `run-<ledger-line-number>`; the ledger is append-only so ids
are stable. Corrupt lines are skipped, never fatal.
"""

import json
import os
import re

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "ledger.jsonl")
TRACES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "traces")
RUN_RE = re.compile(r"^run-(\d+)$")


def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return []


def _entry_summary(index, row):
    extra = row.get("extra") or {}
    usage = row.get("usage") or {}
    return {
        "id": f"run-{index}",
        "script": row.get("script", "?"),
        "model": row.get("model", "?"),
        "thread": row.get("thread_id", "?"),
        "started_ts": row.get("ts", 0),
        "duration_s": row.get("duration_s", 0),
        "ok": "error" not in extra,
        "tools": [c.get("name", "?")
                  for c in row.get("tool_calls", []) or []],
        "tokens": usage.get("total_tokens", 0) or 0,
        "preview": (row.get("final_preview", "") or "")[:200],
    }


def list_runs(limit=50, ledger_path=None):
    """Newest-first run summaries (at most `limit`). Never raises."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    rows = []
    for index, line in enumerate(_read_lines(ledger_path or LEDGER)):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(_entry_summary(index, row))
    rows.reverse()
    return rows[:limit]


def get_run(run_id, ledger_path=None):
    """Full run entry plus trace events, or None. Never raises."""
    match = RUN_RE.match(run_id or "")
    if not match:
        return None
    lines = _read_lines(ledger_path or LEDGER)
    index = int(match.group(1))
    if index >= len(lines):
        return None
    try:
        row = json.loads(lines[index])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    trace = []
    tpath = (row.get("extra") or {}).get("trace") or ""
    # Only serve traces from our own traces/ dir (no path escape).
    full = os.path.realpath(tpath) if tpath else ""
    if full.startswith(os.path.realpath(TRACES) + os.sep):
        for line in _read_lines(full):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(event, dict):
                trace.append(event)
    return {"id": f"run-{index}", "entry": row, "trace": trace}

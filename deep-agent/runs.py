"""Run listing + detail reader over ledger.jsonl + traces/ (deep-agent/).

Backend-agnostic read API behind the /api/agents* redirect: list agent
runs newest-first with stable ids, fetch one run with its trace events,
resolve a loose ref (run id, path, stem, thread) to a trace file.
No Pi data, no backwards compat. Stdlib-only (system-python tests).

Ids are `run-<ledger-line-number>`; the ledger is append-only so ids
are stable. Corrupt lines are skipped, never fatal.

Reader CLI: `python3 runs.py [n|ref]`
  no arg  -> last 10 ledger runs, compact, newest first.
  integer -> last n ledger runs.
  other   -> resolve one run id / path / stem / thread and print
             its trace path.
"""

import fcntl
import json
import os
import re
import sys
import time

import cli
import ledger
import pricing

LEDGER = os.environ.get("EVAL_LEDGER") or os.path.join(
    ledger.state_dir(), "ledger.jsonl")
TRACES = os.path.join(ledger.state_dir(), "traces")
RUN_RE = re.compile(r"^run-(\d+)$")


def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except OSError:
        return []


def _is_deleted(row):
    """Tombstoned rows stay in place so positional run-N ids stay stable."""
    return isinstance(row, dict) and row.get("deleted") is True


def _entry_summary(index, row):
    extra = row.get("extra") or {}
    usage = row.get("usage") or {}
    cost_usd = row.get("cost_usd")
    metered = row.get("metered")
    if not isinstance(cost_usd, (int, float)) \
            or not isinstance(metered, bool):
        # Old rows predate record-time quotes: derive at the
        # current card (display estimate, the row is untouched).
        cost_usd, metered = pricing.quote(row.get("model"), usage)
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
        "has_prompt": isinstance(extra.get("prompt"), str)
        and bool(extra.get("prompt")),
        "cost_usd": cost_usd,
        "metered": metered,
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
        if isinstance(row, dict) and not _is_deleted(row):
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
    if not isinstance(row, dict) or _is_deleted(row):
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


def delete_run(run_id, ledger_path=None):
    """Tombstone one ledger row. True when a live row was deleted."""
    match = RUN_RE.match(run_id or "")
    if not match:
        return False
    return _tombstone_lines({int(match.group(1))},
                            ledger_path or LEDGER) == 1


def clear_finished_runs(ledger_path=None):
    """Tombstone every live ledger row (all ledger rows are finished
    turns; in-flight runs live only in traces/). Returns the count."""
    try:
        with open(ledger_path or LEDGER, encoding="utf-8") as f:
            total = len(f.readlines())
    except OSError:
        return 0
    return _tombstone_lines(set(range(total)), ledger_path or LEDGER)


def _tombstone_lines(indexes, path):
    """Replace indexed lines with tombstones under an exclusive lock.

    The lock matches ledger.record's, so a concurrent turn append
    cannot interleave with the rewrite. Returns tombstoned count.
    """
    try:
        f = open(path, "r+", encoding="utf-8")
    except OSError:
        return 0
    with f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            lines = f.readlines()
            tombstoned = 0
            for i in sorted(indexes):
                if i >= len(lines):
                    continue
                try:
                    row = json.loads(lines[i])
                except (json.JSONDecodeError, ValueError):
                    continue
                if _is_deleted(row):
                    continue
                lines[i] = json.dumps({"deleted": True,
                                       "ts": time.time()}) + "\n"
                tombstoned += 1
            f.seek(0)
            f.writelines(lines)
            f.truncate()
            return tombstoned
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _trace_files(traces_dir=None):
    base = traces_dir or TRACES
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, n) for n in names if n.endswith(".jsonl")]


def _read_start(path):
    """First-line run_start summary, or {} when absent/unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    first = json.loads(line)
                except ValueError:
                    return {}
                if isinstance(first, dict) \
                        and first.get("type") == "run_start":
                    return first
                return {}
    except OSError:
        return {}


def _count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _in_dir(path, base):
    try:
        full = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    return full.startswith(os.path.realpath(base) + os.sep)


def list_traces(traces_dir=None, limit=None):
    """Newest-first trace-file summaries (runs not yet in the ledger
    show up here). Never raises on bad files."""
    runs = []
    for path in _trace_files(traces_dir):
        start = _read_start(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        runs.append({
            "run_id": os.path.basename(path)[:-len(".jsonl")],
            "trace_path": path,
            "script": start.get("script"),
            "thread": start.get("thread"),
            "model": start.get("model"),
            "ts": start.get("ts"),
            "n_lines": _count_lines(path),
            "mtime": mtime,
        })
    runs.sort(key=lambda r: (r["mtime"], r["run_id"]), reverse=True)
    if limit is not None:
        runs = runs[:max(0, limit)]
    return runs


def _ledger_trace_for_thread(ref, ledger_path, traces_dir):
    """Newest existing extra.trace path for ledger thread_id == ref,
    confined to the traces dir."""
    try:
        with open(ledger_path or LEDGER, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or _is_deleted(entry) \
                or entry.get("thread_id") != ref:
            continue
        extra = entry.get("extra") or {}
        trace = extra.get("trace") if isinstance(extra, dict) else None
        if isinstance(trace, str) and os.path.isfile(trace) \
                and _in_dir(trace, traces_dir or TRACES):
            return trace
    return None


def resolve_run(ref, traces_dir=None, ledger_path=None):
    """Resolve a run ref to a trace file path, or None. Never raises.

    Resolution order:
      0. `run-<n>` id -> that ledger row's extra.trace.
      1. `ref` is an existing file path -> that file.
      2. `traces/<ref>[.jsonl]` exists -> that file.
      3. Newest trace whose stem equals / starts with `ref`, or whose
         run_start thread equals `ref`.
      4. Newest ledger entry (extra.trace) whose thread_id equals `ref`.
    """
    if not ref:
        return None
    if RUN_RE.match(ref):
        row = get_run(ref, ledger_path)
        trace = ((row or {}).get("entry", {}).get("extra") or {}) \
            .get("trace")
        if isinstance(trace, str) and os.path.isfile(trace) \
                and _in_dir(trace, traces_dir or TRACES):
            return trace
        return None
    if os.path.isfile(ref):
        return os.path.abspath(ref)
    base = traces_dir or TRACES
    for cand in (os.path.join(base, ref),
                 os.path.join(base, ref + ".jsonl")):
        if os.path.isfile(cand):
            return cand
    candidates = []
    for path in _trace_files(base):
        stem = os.path.basename(path)[:-len(".jsonl")]
        if stem == ref:
            return path
        if stem.startswith(ref):
            candidates.append(path)
            continue
        if _read_start(path).get("thread") == ref:
            candidates.append(path)
    if candidates:
        candidates.sort(key=lambda p: os.path.basename(p))
        return candidates[-1]
    return _ledger_trace_for_thread(ref, ledger_path, base)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        found = list_runs(limit=10)
        if not found:
            print("no runs")
            return 0
        for r in found:
            print(f"{r['id']} script={r['script']} "
                  f"thread={r['thread']} model={r['model']} "
                  f"ok={r['ok']} dur={r['duration_s']}s "
                  f"tools={len(r['tools'])}")
        return 0
    ref = argv[0]
    try:
        n = int(ref)
    except ValueError:
        n = None
    if n is not None:
        if n <= 0:
            return 0
        for r in list_runs(limit=n):
            print(f"{r['id']} script={r['script']} "
                  f"thread={r['thread']} model={r['model']} "
                  f"ok={r['ok']} dur={r['duration_s']}s "
                  f"tools={len(r['tools'])}")
        return 0
    path = resolve_run(ref)
    if path is None:
        # Stdout, like every Sorry line: exit 1 promises the reply
        # on stdout so chunk readers show it on the phone.
        return cli.sorry(f"Sorry, no run found for {ref!r}.")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Group TwT and queue failures without sending trace contents to the ledger."""

import argparse
from collections import Counter
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile


AGENT = "S5-Local"
SEEN_FILE = "failure-audit-seen.json"
NO_PROVIDER = "TwT no provider available"
REPEAT = "Coder repeat loop"
READ_ONLY = "Coder read-only run"
OUTSIDE = "Coder write outside worktree"
WRITE_TOOLS = {"write_file", "edit_file", "delete"}
ATTEMPT_REASONS = {
    "timeout",
    "upstream connection error",
    "invalid upstream chat response",
    "upstream assistant message has no content, tool_calls or function_call",
}


def twt_counts(path):
    counts = Counter()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        for provider, status, reason, count in db.execute("""
            SELECT json_extract(data, '$.provider'),
                   json_extract(data, '$.http_status'),
                   json_extract(data, '$.reason'), COUNT(*)
            FROM attempts
            WHERE json_extract(data, '$.called') = 1
              AND json_extract(data, '$.reason') != 'success'
            GROUP BY 1, 2, 3
        """):
            if not isinstance(provider, str) or not re.fullmatch(
                r"[a-z0-9_-]{1,40}", provider
            ):
                continue
            if type(status) is int and 400 <= status <= 599:
                counts[f"TwT {provider} HTTP {status}"] += count
            elif reason in ATTEMPT_REASONS:
                counts[f"TwT {provider} {reason}"] += count
        (exhausted,) = db.execute("""
            SELECT COUNT(*) FROM traces
            WHERE json_extract(data, '$.response.error.type') = 'no_provider_available'
        """).fetchone()
        if exhausted:
            counts[NO_PROVIDER] = exhausted
    return counts


def coder_cause(event):
    if event.get("type") == "coder_verdict" and event.get("ok") is False:
        if "no written files for implementation" in event.get("reason", ""):
            return READ_ONLY
    if event.get("type") != "tool_call":
        return None
    error = event.get("error", "")
    result = event.get("result_preview", "")
    if error.startswith("BLOCKED:") and "repeated unchanged" in error:
        return REPEAT
    if re.match(r"BLOCKED: .* made \d+ inspection calls without editing", error):
        return READ_ONLY
    if event.get("name") == "run_check" and re.search(
        r"(?m)^DITHERING: this exact command has ", result
    ):
        return REPEAT
    if event.get("name") in WRITE_TOOLS and (error or result).startswith(
        "outside_worktree_root: use worktree-relative paths"
    ):
        return OUTSIDE
    return None


def trace_counts(root):
    counts = Counter()
    for parent in (root, root / "archive"):
        for path in parent.glob("*/state/traces/coder*.jsonl"):
            with path.open(encoding="utf-8") as stream:
                causes = {
                    coder_cause(event)
                    for line in stream
                    if line.strip() and line.endswith("\n")
                    if isinstance(event := json.loads(line), dict)
                }
            counts.update(causes - {None})
    return counts


def runtime_trace_counts(config):
    runtime = config.get("runtime_command", [])
    if not runtime:
        return trace_counts(Path(config["runtime_state_dir"]))
    scanned = subprocess.run(
        runtime
        + [config["worker_python"], "-", "--trace-counts", config["runtime_state_dir"]],
        input=Path(__file__).read_text(encoding="utf-8"),
        stdout=subprocess.PIPE,
        text=True,
        check=True,
        timeout=30,
    )
    return Counter(json.loads(scanned.stdout))


def load_seen(path):
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    if not isinstance(seen, list) or not all(isinstance(key, str) for key in seen):
        raise ValueError(f"invalid failure audit state: {path}")
    return set(seen)


def save_seen(path, seen):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(sorted(seen), stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def intake_note(cause):
    repo = "boomtick" if cause.startswith("TwT ") else "remote-server"
    return (
        f"[issue] {repo}: S5 failure audit: {cause}\n"
        f"Problem: The S5 audit observed {cause} in retained operational evidence.\n"
        "Out of scope: Unrelated failure signatures and queue readiness.\n"
        "Done when: Reproduce or explain the cause using S5 audit evidence, "
        "and verify a fix or document the expected behavior."
    )


def audit(config, twt_db, *, poster=None):
    counts = twt_counts(Path(twt_db))
    counts.update(runtime_trace_counts(config))
    state = Path(config["state_dir"]) / SEEN_FILE

    def report(seen):
        print(f"S5 failure audit: {len(counts)} causes", flush=True)
        for cause, count in sorted(counts.items()):
            print(
                f"  {cause}: {count} ({'seen' if cause in seen else 'new'})", flush=True
            )

    if poster is None:
        report(load_seen(state))
        return
    with state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seen = load_seen(state)
        report(seen)
        for cause in sorted(counts.keys() - seen):
            status = poster(AGENT, intake_note(cause), status="Act", link="")
            if not 200 <= status < 300:
                raise RuntimeError(f"failure audit ledger post returned HTTP {status}")
            seen.add(cause)
            save_seen(state, seen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path)
    source.add_argument("--trace-counts", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--twt-db", type=Path, help="override the configured TwT DB")
    args = parser.parse_args()
    if args.trace_counts is not None:
        print(json.dumps(trace_counts(args.trace_counts)))
        return
    config = json.loads(args.config.read_text(encoding="utf-8"))
    twt_db = args.twt_db or config.get("failure_audit_db")
    if not twt_db:
        parser.error("--twt-db or failure_audit_db in config is required")
    audit(config, twt_db)


if __name__ == "__main__":
    main()

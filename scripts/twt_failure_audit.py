#!/usr/bin/env python3
"""Group TwT and queue failures without sending trace contents to the ledger."""

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import UTC, datetime
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
DEFAULT_DAILY_POST_CAP = 3
NO_PROVIDER = "TwT no provider available"
REPEAT = "Coder repeat loop"
READ_ONLY = "Coder read-only run"
OUTSIDE = "Coder write outside worktree"
LOOP_DETECTED = "Coder loop detected"
GAVE_UP = "Coder gave up"
TERMINATION_CAUSES = {"loop_detected": LOOP_DETECTED, "gave_up": GAVE_UP}
WRITE_TOOLS = {"write_file", "edit_file", "delete"}
PROVIDER_ID = r"[a-z0-9_-]{1,40}"
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
            if not isinstance(provider, str) or not re.fullmatch(PROVIDER_ID, provider):
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
    if event.get("type") == "termination":
        return TERMINATION_CAUSES.get(event.get("reason"))
    if event.get("type") != "tool_call":
        return None
    error = event.get("error", "")
    result = event.get("result_preview", "")
    # result_preview drops the tail where run_check appends the warning;
    # older events lack check_output.
    check_output = event.get("check_output", result)
    if error.startswith("BLOCKED:") and "repeated unchanged" in error:
        return REPEAT
    if re.match(r"BLOCKED: .* made \d+ inspection calls without editing", error):
        return READ_ONLY
    if event.get("name") == "run_check" and re.search(
        r"(?m)^DITHERING: this exact command has ", check_output
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
                events = [
                    event
                    for line in stream
                    if line.strip() and line.endswith("\n")
                    if isinstance(event := json.loads(line), dict)
                ]
            causes = {coder_cause(event) for event in events}
            # Infra failures are classified first: a run that died on infra
            # says nothing about model capability.
            if any(
                e.get("type") == "termination" and e.get("reason") == "infra_error"
                for e in events
            ):
                causes -= set(TERMINATION_CAUSES.values())
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


def load_state(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = []
    if isinstance(state, list) and all(isinstance(cause, str) for cause in state):
        return {"seen": dict.fromkeys(state), "pending": {}, "day": "", "posted": 0}
    if (
        isinstance(state, dict)
        and isinstance(state.get("seen"), dict)
        and all(url is None or isinstance(url, str) for url in state["seen"].values())
        and isinstance(state.get("pending"), dict)
        and all(type(n) is int and n > 0 for n in state["pending"].values())
        and isinstance(state.get("day"), str)
        and type(state.get("posted")) is int
        and state["posted"] >= 0
    ):
        return state
    raise ValueError(f"invalid failure audit state: {path}")


def save_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def locked_state(path):
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield load_state(path)


def intake_repo(cause):
    return "boomtick" if cause.startswith("TwT ") else "remote-server"


def intake_note(cause):
    return (
        f"[issue] {intake_repo(cause)}: S5 failure audit: {cause}\n"
        f"Problem: The S5 audit observed {cause} in retained operational evidence.\n"
        "Out of scope: Unrelated failure signatures and queue readiness.\n"
        "Done when: Reproduce or explain the cause using S5 audit evidence, "
        "and verify a fix or document the expected behavior."
    )


def known_cause(entry):
    cause, _, url = entry.partition("=")
    provider = re.fullmatch(rf"TwT {PROVIDER_ID} (.+)", cause)
    valid = cause in {
        NO_PROVIDER,
        REPEAT,
        READ_ONLY,
        OUTSIDE,
        LOOP_DETECTED,
        GAVE_UP,
    } or (
        provider is not None
        and (
            provider[1] in ATTEMPT_REASONS
            or re.fullmatch(r"HTTP [45]\d\d", provider[1]) is not None
        )
    )
    if not valid or not re.fullmatch(
        rf"https://github\.com/arii/{intake_repo(cause)}/(?:issues|pull)/[1-9]\d*", url
    ):
        raise argparse.ArgumentTypeError(
            "expected SIGNATURE=https://github.com/arii/REPO/issues/N or /pull/N"
        )
    return cause, url


def mark_known(path, entries):
    with locked_state(path) as state:
        state["seen"].update(entries)
        for cause, _ in entries:
            state["pending"].pop(cause, None)
        save_state(path, state)


def utc_day():
    return datetime.now(UTC).date().isoformat()


def plan_posts(state, counts, cap):
    day = utc_day()
    if state["day"] != day:
        state.update(day=day, posted=0)
    pending = state["pending"]
    pending.update(
        {cause: count for cause, count in counts.items() if cause not in state["seen"]}
    )
    ranked = sorted(pending, key=lambda cause: (-pending[cause], cause))
    available = max(0, cap - state["posted"])
    print(f"S5 failure audit: {len(counts)} causes", flush=True)
    for cause, count in sorted(counts.items()):
        status = "seen" if cause in state["seen"] else "new"
        print(f"  {cause}: {count} ({status})", flush=True)
    eligible, deferred = ranked[:available], ranked[available:]
    print(f"  Eligible today (UTC): {', '.join(eligible) or 'none'}", flush=True)
    print(f"  Deferred: {len(deferred)}", flush=True)
    return eligible


def audit(config, twt_db, *, poster=None):
    cap = config.get("failure_audit_daily_post_cap", DEFAULT_DAILY_POST_CAP)
    if type(cap) is not int or cap < 1:
        raise ValueError("failure_audit_daily_post_cap must be a positive integer")
    counts = twt_counts(Path(twt_db))
    counts.update(runtime_trace_counts(config))
    path = Path(config["state_dir"]) / SEEN_FILE
    if poster is None:
        plan_posts(load_state(path), counts, cap)
        return
    with locked_state(path) as state:
        eligible = plan_posts(state, counts, cap)
        save_state(path, state)
        for cause in eligible:
            if utc_day() != state["day"]:
                break
            status = poster(AGENT, intake_note(cause), status="Act", link="")
            if not 200 <= status < 300:
                raise RuntimeError(f"failure audit ledger post returned HTTP {status}")
            accepted_day = utc_day()
            crossed_midnight = accepted_day != state["day"]
            if crossed_midnight:
                state.update(day=accepted_day, posted=0)
            state["seen"][cause] = None
            state["pending"].pop(cause)
            state["posted"] += 1
            save_state(path, state)
            if crossed_midnight:
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path)
    source.add_argument("--trace-counts", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--twt-db", type=Path, help="override the configured TwT DB")
    parser.add_argument(
        "--mark-known",
        action="append",
        type=known_cause,
        metavar="SIGNATURE=URL",
        help="record a tracking issue or PR without posting (repeatable)",
    )
    args = parser.parse_args()
    if args.trace_counts is not None:
        if args.mark_known or args.twt_db:
            parser.error("--trace-counts does not accept --mark-known or --twt-db")
        print(json.dumps(trace_counts(args.trace_counts)))
        return
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.mark_known:
        if args.twt_db:
            parser.error("--mark-known only needs --config, not --twt-db")
        mark_known(Path(config["state_dir"]) / SEEN_FILE, args.mark_known)
        return
    twt_db = args.twt_db or config.get("failure_audit_db")
    if not twt_db:
        parser.error("--twt-db or failure_audit_db in config is required")
    audit(config, twt_db)


if __name__ == "__main__":
    main()

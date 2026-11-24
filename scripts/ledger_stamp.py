#!/usr/bin/env python3
"""Write the kiro-gate ledger-ok stamps for ready issues without a fresh foreign
Agent Ledger claim. A failed ledger read writes no stamps (fail closed).

Runs forever (see scripts/twt-ledger-stamp.service). One failed `gh issue list`
or any other error skips that repo or that cycle and is logged; it never ends the
process (#1705: one transient gh failure killed it and every ready issue was then
skipped for hours). After each cycle that read the ledger and listed every repo,
the heartbeat file is touched, so `queue_runner.py --status` can report a stale
stamp daemon (config key `ledger_stamp_heartbeat`).

Moved into the repo from ~/twt/queue/ledger-stamp.py on S5 (#1705)."""

import datetime
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

QUEUE_DIR = Path.home() / "twt" / "queue"
INTERVAL_SECONDS = 300
GH_TIMEOUT_SECONDS = 120


def load_runner(path):
    """queue_runner.py from the path the queue itself runs (QUEUE_RUNNER). Its sibling
    imports (ledger_read, ledger_post) resolve from this script's own directory, which
    Python puts first on sys.path, so run the ledger_stamp.py from the same checkout."""
    spec = importlib.util.spec_from_file_location("queue_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_logger(path):
    def log(line):
        with path.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} STAMP {line}\n")

    return log


def ready_issues(repo):
    """Open issues labelled ready in repo. Raises on any gh or JSON failure."""
    out = subprocess.check_output(
        [
            "/usr/bin/gh",
            "issue",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--label",
            "ready",
            "--json",
            "number",
            "--limit",
            "100",
        ],
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
    )
    return json.loads(out)


def held_reason(runner, rows, link, now):
    """Why this issue must not be stamped (a fresh Act claim by another agent), or None."""
    mine = [r for r in rows if (r.get("Link") or "").strip() == link]
    if not mine:
        return None
    last = mine[-1]
    source = (last.get("Agent / Source") or "").strip()
    if (last.get("Status") or "").strip() == "Act" and source != runner.LEDGER_AGENT:
        age = runner.ledger_age_hours(last.get("Timestamp") or "", now)
        if age is None or age < runner.LEDGER_STALE_HOURS:
            return f"fresh Act claim by {source[:40]}"
    return None


def stamp_cycle(runner, repos, stamps, log, list_issues=ready_issues, now=None):
    """One pass. Returns True when the ledger was read and every repo was listed.

    A repo whose listing fails is skipped this cycle: its stamps are left as they
    are (neither created nor removed), so the gate's own freshness check decides."""
    try:
        rows = runner.ledger_rows()
    except Exception as error:
        log(f"ledger unreadable ({type(error).__name__}), no stamps written")
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    complete = True
    for repo in repos:
        try:
            listed = list_issues(repo)
        except Exception as error:
            log(
                f"{repo} issue list failed ({type(error).__name__}), skipped this cycle"
            )
            complete = False
            continue
        for issue in listed:
            number = issue["number"]
            held = held_reason(
                runner, rows, f"https://github.com/{repo}/issues/{number}", now
            )
            stamp = stamps / f"{repo.replace('/', '_')}_{number}"
            if held:
                stamp.unlink(missing_ok=True)
                log(f"{repo}#{number} held: {held}")
            else:
                stamp.touch()
    return complete


def run_forever(runner, repos, stamps, heartbeat, log, sleep=time.sleep, cycles=None):
    """Stamp every INTERVAL_SECONDS until killed. `cycles` bounds the loop for tests."""
    n = 0
    while cycles is None or n < cycles:
        n += 1
        try:
            if stamp_cycle(runner, repos, stamps, log):
                heartbeat.touch()
        except Exception as error:  # never let one bad cycle end the daemon
            log(f"cycle failed ({type(error).__name__}), retrying next cycle")
        sleep(INTERVAL_SECONDS)


def main():
    queue_dir = Path(os.environ.get("LEDGER_STAMP_DIR", QUEUE_DIR))
    stamps = Path(os.environ.get("LEDGER_STAMP_STAMPS", queue_dir / "ledger-ok"))
    heartbeat = Path(
        os.environ.get("LEDGER_STAMP_HEARTBEAT", queue_dir / "ledger-stamp.heartbeat")
    )
    log = make_logger(
        Path(os.environ.get("LEDGER_STAMP_LOG", queue_dir / "kiro-gate.log"))
    )
    repos = json.loads(Path(os.environ["QUEUE_CONFIG"]).read_text())["discover_repos"]
    runner = load_runner(Path(os.environ["QUEUE_RUNNER"]))
    stamps.mkdir(parents=True, exist_ok=True)
    run_forever(runner, repos, stamps, heartbeat, log)


if __name__ == "__main__":
    main()

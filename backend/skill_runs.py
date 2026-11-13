"""Persistent run history (issue #1258): one row per run.

A run is recorded by run_and_record, which runs a skill through
the replayer (#1255) and appends the outcome. Rows are JSONL —
not notebook prose — under BROWSER_RUNS_FILE (default
/var/lib/buttons/browser_runs.jsonl, call-time env like the other
browser paths). run_number counts per skill from 1, so the demo's
three runs read as run 1 (pass), run 2 (fail at the changed step),
run 3 (pass after correction).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from backend.skill_replay import run_skill


def runs_file() -> Path:
    """Resolve the run log at call time so tests redirect it."""
    return Path(
        os.environ.get("BROWSER_RUNS_FILE", "/var/lib/buttons/browser_runs.jsonl")
    )


def list_runs(skill: str | None = None, limit: int = 100) -> list:
    """Rows oldest first, optionally filtered to one skill."""
    try:
        text = runs_file().read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (skill is None or row.get("skill") == skill):
            rows.append(row)
    return rows[-max(1, limit) :]


def _next_run_number(skill: str) -> int:
    numbers = [
        r.get("run_number", 0)
        for r in list_runs(skill)
        if isinstance(r.get("run_number"), int)
    ]
    return (max(numbers) + 1) if numbers else 1


def run_and_record(skill: dict, driver) -> dict:
    """Run a skill and append its row. Returns the replay result
    with run_number added. Invalid skills are refused by the
    replayer first and still recorded, so the log shows what was
    attempted, not only what passed."""
    result = run_skill(skill, driver)
    name = skill.get("skill", "") if isinstance(skill, dict) else ""
    version = skill.get("version", 0) if isinstance(skill, dict) else 0
    row = {
        "run_number": _next_run_number(name),
        "skill": name,
        "version": version,
        "ts": int(time.time()),
        "passed": bool(result.get("passed")),
        "failed_step": result.get("failed_step"),
        "reason": result.get("reason", ""),
    }
    try:
        path = runs_file()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass
    result["run_number"] = row["run_number"]
    return result

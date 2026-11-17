"""Persistent run history (issue #1258): one row per run.

A run is recorded by run_and_record, which runs a skill through
the replayer (#1255) and appends the outcome. Rows are JSONL —
not notebook prose, under BROWSER_RUNS_FILE in the configured browser
data directory. run_number counts per skill from 1, so the demo's
three runs read as run 1 (pass), run 2 (fail at the changed step),
run 3 (pass after correction).
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import time
from pathlib import Path

from backend.browser_config import data_path
from backend.skill_replay import run_skill
from backend.skill_trace import REDACTED, redact_persistence
from backend.skill_format import ACTIONS


def runs_file() -> Path:
    """Resolve the run log at call time so tests redirect it."""
    return data_path("BROWSER_RUNS_FILE", "browser_runs.jsonl")


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


def record_run(result: dict) -> dict:
    """Redact and durably append a live or replay run under one writer lock."""
    row = redact_persistence(result)
    row["ts"] = int(time.time())
    path = runs_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        number = 0
        terminated = True
        for line in fh:
            terminated = line.endswith("\n")
            try:
                old = json.loads(line)
            except ValueError:
                continue
            if isinstance(old, dict) and old.get("skill") == row.get("skill"):
                candidate = old.get("run_number")
                if type(candidate) is int:
                    number = max(number, candidate)
        row["run_number"] = number + 1
        fh.seek(0, os.SEEK_END)
        if not terminated:
            fh.write("\n")
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return row


def run_and_record(skill: dict, driver) -> dict:
    """Run a skill and append its row. Returns the replay result
    with run_number added. Invalid skills are refused by the
    replayer first and still recorded, so the log shows what was
    attempted, not only what passed."""
    result = run_skill(skill, driver)
    return record_run(result)


def replay_trace(row: dict) -> dict:
    """Validate and reproduce recorded verdicts without browser or skill access."""
    if (
        not isinstance(row, dict)
        or type(row.get("trace_version")) is not int
        or row["trace_version"] != 1
    ):
        raise ValueError("unsupported or missing trace version")
    steps = row.get("steps")
    if not isinstance(steps, list) or not isinstance(row.get("context"), dict):
        raise ValueError("trace needs steps and context")
    if type(row.get("passed")) is not bool or not isinstance(row.get("reason"), str):
        raise ValueError("trace needs a recorded verdict and reason")
    if "failed_step" not in row or (
        row["failed_step"] is not None and type(row["failed_step"]) is not int
    ):
        raise ValueError("trace needs a failure position")
    failed_step = None
    required = {
        "input",
        "proposal",
        "gate",
        "executed",
        "outcome",
        "page_hash",
        "model_version",
        "gate_version",
        "mode",
        "context",
    }
    for n, step in enumerate(steps, 1):
        if (
            not isinstance(step, dict)
            or type(step.get("n")) is not int
            or step.get("n") != n
            or not required.issubset(step)
        ):
            raise ValueError("trace steps must be complete and sequential")
        if failed_step is not None:
            raise ValueError("trace continues beyond the recorded failure")
        if not all(
            isinstance(step[key], dict)
            for key in ("input", "proposal", "gate", "outcome", "context")
        ):
            raise ValueError("invalid step context")
        if step["mode"] not in ("live", "replay") or type(step["executed"]) is not bool:
            raise ValueError("invalid step execution context")
        if not all(
            isinstance(step[key], str) and step[key].strip()
            for key in ("model_version", "gate_version")
        ):
            raise ValueError("missing step versions")
        unobservable_dialog = (
            step["mode"] == "live"
            and step["page_hash"] is None
            and step["context"].get("page_hash_unavailable") == "dialog"
            and step["context"].get("dialog_type")
            in ("alert", "confirm", "prompt", "beforeunload")
        )
        if (
            step["executed"]
            and not unobservable_dialog
            and (
                not isinstance(step["page_hash"], str)
                or not re.fullmatch(r"[a-f0-9]{12,64}", step["page_hash"])
            )
        ):
            raise ValueError("executed step has no page-state hash")
        gate = step["gate"].get("verdict")
        verdict = step["outcome"].get("verdict")
        if step["proposal"].get("action") not in ACTIONS or not all(
            isinstance(step[key].get("reason"), str) for key in ("gate", "outcome")
        ):
            raise ValueError("incomplete proposal or verdict")
        if gate not in ("allowed", "blocked") or verdict not in (
            "passed",
            "failed",
            "error",
            "blocked",
        ):
            raise ValueError("unknown step verdict")
        if (gate == "blocked") != (verdict == "blocked") or (
            gate == "blocked" and step["executed"]
        ):
            raise ValueError("gate and execution contradict each other")
        if verdict in ("passed", "failed") and not step["executed"]:
            raise ValueError("check verdict without execution")
        if verdict != "passed":
            failed_step = n
    passed = bool(steps) and failed_step is None
    if row["passed"] != passed or row.get("failed_step") != failed_step:
        raise ValueError("summary contradicts step verdicts")
    if not steps and not row["reason"]:
        raise ValueError("empty trace has no validation failure")
    result = copy.deepcopy(row)
    for step in result["steps"]:
        payload = json.dumps({"input": step["input"], "proposal": step["proposal"]})
        if step.get("redacted") or REDACTED in payload:
            step["replay_status"] = "human_intervention"
    return result

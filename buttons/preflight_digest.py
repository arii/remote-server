#!/usr/bin/env python3
"""Compact digest of `td --json preflight` output.

`td preflight` returns 30KB+ of nested JSON (doctor, typecheck, lint
reports from oxlint/pylint/semgrep, overlap detection, merge
simulation). Nobody reads that on a phone. This prints one line per
check — OK / FAIL — plus a one-line detail on failures, then a
summary line.

The schema is not pinned: checks are found heuristically (a dict
with a name-ish key and a status-ish key, at any nesting depth, the
parent key serving as the name when the dict has none). Anything
unrecognized is skipped; if nothing classifies, the raw input is
printed so the run never loses the underlying output.

Exit 0 when every found check passes, 1 when any fails. Pair with
`set -o pipefail` so td's own non-zero exit still fails the step.
"""

import json
import sys

MAX_LINES = 40

NAME_KEYS = ("name", "title", "check", "step", "id")
STATUS_KEYS = ("status", "state", "result")
BOOL_KEYS = ("ok", "passed", "pass", "success", "clean")
DETAIL_KEYS = ("message", "error", "detail", "summary")
COUNT_KEYS = ("errors", "errorCount", "warnings", "warningCount",
              "issues", "failures")

OK_WORDS = {"ok", "pass", "passed", "success", "successful", "clean",
            "healthy", "up-to-date"}
FAIL_WORDS = {"fail", "failed", "failure", "error", "errored", "dirty",
              "conflicted"}


def _classify(value):
    """True/False/None from a status-ish value. Numbers are never
    statuses (a count of 0 is not a verdict)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None
    s = str(value).strip().lower()
    if s in OK_WORDS:
        return True
    if s in FAIL_WORDS:
        return False
    return None


def _detail(node):
    for k in DETAIL_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0][:160]
    counts = []
    for k in COUNT_KEYS:
        v = node.get(k)
        if isinstance(v, int) and v > 0:
            counts.append(f"{v} {k}")
        elif isinstance(v, list) and v:
            counts.append(f"{len(v)} {k}")
    return ", ".join(counts)


def _collect(node, checks, depth=0, key_name=None):
    if depth > 5 or len(checks) > 200:
        return
    if isinstance(node, dict):
        name = key_name
        for k in NAME_KEYS:
            v = node.get(k)
            if isinstance(v, (str, int)):
                name = str(v)
                break
        status = None
        for k in STATUS_KEYS:
            if k in node:
                status = _classify(node[k])
                if status is not None:
                    break
        if status is None:
            for k in BOOL_KEYS:
                if k in node:
                    status = _classify(node[k])
                    if status is not None:
                        break
        if status is not None:
            checks.append((name or "check", status, _detail(node)))
        else:
            for k, v in node.items():
                _collect(v, checks, depth + 1,
                         key_name=k if isinstance(k, str) else None)
    elif isinstance(node, list):
        for v in node:
            _collect(v, checks, depth + 1)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        sys.stdout.write(raw if raw.endswith("\n") else raw + "\n")
        return 0
    checks = []
    _collect(data, checks)
    # Dedupe identical (name, status) pairs; keep first detail.
    seen, uniq = set(), []
    for name, status, detail in checks:
        if (name, status) not in seen:
            seen.add((name, status))
            uniq.append((name, status, detail))
    if not uniq:
        sys.stdout.write(raw if raw.endswith("\n") else raw + "\n")
        return 0
    lines = []
    failed = 0
    for name, status, detail in uniq[:MAX_LINES]:
        mark = "✓" if status else "✗"
        if not status:
            failed += 1
        line = f"{mark} {name}"
        if detail:
            line += f" — {detail}"
        lines.append(line)
    if len(uniq) > MAX_LINES:
        lines.append(f"… {len(uniq) - MAX_LINES} more")
    passed = len(uniq) - failed
    lines.append(f"preflight: {passed} passed, {failed} failed")
    sys.stdout.write("\n".join(lines) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

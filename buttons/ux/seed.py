#!/usr/bin/env python3
"""Seed fake data for the buttons UX preview (serve.sh).

Creates a fake repos dir with a couple of git checkouts, a
fixture briefing so the Briefing and Actions tabs render realistic states,
and a throwaway ledger with representative entries across all kinds.

Usage: seed.py <seed_dir> <ledger_path> [fixture_briefing_path]
Stdlib only. Idempotent.
"""
import json
import os
import subprocess
import sys
import time

SEED_DIR = sys.argv[1]
LEDGER_PATH = sys.argv[2]
FIXTURE_PATH = sys.argv[3] if len(sys.argv) > 3 else os.path.join(SEED_DIR, "fixture-briefing.json")

os.makedirs(SEED_DIR, exist_ok=True)

# 1. Fake git repositories
for name, files in (("demo-app", ["README.md", "main.py"]),
                    ("demo-site", ["README.md", "index.html"])):
    d = os.path.join(SEED_DIR, name)
    os.makedirs(d, exist_ok=True)
    for f in files:
        p = os.path.join(d, f)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("# " + name + "\n")
    if not os.path.isdir(os.path.join(d, ".git")):
        subprocess.run(["git", "init", "-q"], cwd=d, check=False)
        subprocess.run(["git", "add", "-A"], cwd=d, check=False)
        subprocess.run(
            ["git", "-c", "user.email=ux@example.com",
             "-c", "user.name=ux", "commit", "-qm", "seed"],
            cwd=d, check=False)
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/example/{name}.git"],
            cwd=d, check=False)

# 2. Rich, realistic ledger entries (JSON lines format as expected by server.py)
now = time.time()
entries = [
    {
        "kind": "system",
        "ts": now - 7200,
        "event": "buttons started",
    },
    {
        "kind": "webhook",
        "ts": now - 5400,
        "repo": "example/demo-app",
        "ok": True,
        "advanced": True,
        "detail": "Fast-forward 4a2b1c..9d8e7f",
    },
    {
        "kind": "run",
        "ts": now - 3600,
        "action": "status",
        "repo": "demo-app",
        "exit": 0,
        "output": "demo-app up 2 hours\nContainers: healthy\nBranch: main",
    },
    {
        "kind": "pr-action",
        "ts": now - 2400,
        "action": "merge",
        "pr": 42,
        "repo": "demo-app",
        "ok": True,
        "note": "Squashed and merged #42",
        "output": "Merge successful. Deleted branch fix/cache-invalidation.",
    },
    {
        "kind": "job",
        "ts": now - 1800,
        "job": "job-8f2a10",
        "template": "triage",
        "repo": "demo-app",
        "step": 2,
        "event": "step done",
        "ok": True,
        "outcome": "done",
    },
    {
        "kind": "run",
        "ts": now - 900,
        "action": "preflight",
        "repo": "demo-app",
        "exit": 1,
        "output": "✗ lint: FAIL\n  src/index.ts:12: Unexpected console.log statement.\nstep failed with exit code 1; aborting",
        "error": "preflight failed on lint check",
    },
]

d = os.path.dirname(LEDGER_PATH)
if d:
    os.makedirs(d, exist_ok=True)
with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
    for entry in entries:
        fh.write(json.dumps(entry) + "\n")

# 3. Fixture briefing data showing realistic PR cards, CI checks, reviews, and Jules sessions
fixture_briefing = {
    "repo": "demo-app",
    "attention": [
        "PR #45 has failing CI: test-e2e timed out after 10m",
        "PR #43 has merge conflicts with main",
    ],
    "prs": [
        {
            "number": 42,
            "title": "Clean up terminal takeover signal handlers",
            "branch": "fix/terminal-takeover",
            "ci": "passing",
            "merge": "clean",
            "conflicted": False,
            "draft": False,
            "stale": False,
            "updated": "2 hours ago",
            "url": "https://github.com/example/demo-app/pull/42",
            "verdicts": [{"user": "reviewer1", "state": "APPROVED"}],
            "reviews_picked": [{"user": "reviewer1", "state": "APPROVED", "body": "LGTM, clean takeover handling"}],
        },
        {
            "number": 43,
            "title": "Migrate ledger storage to streaming JSONL",
            "branch": "feat/streaming-ledger",
            "ci": "passing",
            "merge": "dirty",
            "conflicted": True,
            "draft": False,
            "stale": False,
            "updated": "yesterday",
            "url": "https://github.com/example/demo-app/pull/43",
            "verdicts": [{"user": "lead-dev", "state": "CHANGES_REQUESTED"}],
            "reviews_picked": [{"user": "lead-dev", "state": "CHANGES_REQUESTED", "body": "Resolve conflicts with main"}],
        },
        {
            "number": 45,
            "title": "Upgrade Playwright headless browser test suite",
            "branch": "chore/playwright-upgrade",
            "ci": "failing",
            "merge": "behind",
            "conflicted": False,
            "draft": False,
            "stale": False,
            "updated": "15 mins ago",
            "url": "https://github.com/example/demo-app/pull/45",
            "failing": ["test-e2e (timeout in browser launch)"],
        },
    ],
    "pr_list": [
        {"number": 42, "title": "Clean up terminal takeover signal handlers"},
        {"number": 43, "title": "Migrate ledger storage to streaming JSONL"},
        {"number": 45, "title": "Upgrade Playwright headless browser test suite"},
    ],
    "prs_err": None,
    "failing_runs": [
        {
            "name": "CI / e2e-tests",
            "branch": "chore/playwright-upgrade",
            "event": "pull_request",
            "failed": [{"job": "test-e2e", "steps": ["run playwright test"]}],
        }
    ],
    "jules": {
        "items": [
            "- Fix CI on #45 — running — 2026-11-01",
        ],
        "other": 0,
        "unscoped": 0,
        "unavailable": None,
    },
    "reviews_lines": [
        "- #42: @reviewer1 APPROVED — 'LGTM, clean takeover handling'",
        "- #43: @lead-dev CHANGES_REQUESTED — 'Resolve conflicts with main'",
    ],
    "board": [
        {"branch": "fix/terminal-takeover", "issue": "38", "status": "in-review"},
        {"branch": "chore/playwright-upgrade", "issue": "41", "status": "in-progress"},
    ],
    "board_lines": [
        "- fix/terminal-takeover — issue 38 — in-review",
        "- chore/playwright-upgrade — issue 41 — in-progress",
    ],
    "board_err": None,
    "conflicts": [{"number": 43, "files": ["buttons/server.py"]}],
    "conflicts_lines": ["- #43 conflicts in buttons/server.py"],
    "conf_err": None,
    "workflow_lines": [],
    "runs_err": None,
}

fd = os.path.dirname(FIXTURE_PATH)
if fd:
    os.makedirs(fd, exist_ok=True)
with open(FIXTURE_PATH, "w", encoding="utf-8") as fh:
    json.dump(fixture_briefing, fh, indent=2)

print(f"seeded {SEED_DIR}, {LEDGER_PATH}, and {FIXTURE_PATH}")

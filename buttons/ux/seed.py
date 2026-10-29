#!/usr/bin/env python3
"""Seed fake data for the buttons UX preview (serve.sh).

Creates a fake repos dir with a couple of git checkouts and a
throwaway ledger with a few representative entries, so the
page renders populated states instead of empty/error ones.

Usage: seed.py <seed_dir> <ledger_path>
Stdlib only. Idempotent.
"""
import json
import os
import subprocess
import sys
import time

SEED_DIR, LEDGER_PATH = sys.argv[1], sys.argv[2]
os.makedirs(SEED_DIR, exist_ok=True)

for name, files in (("demo-app", ["README.md", "main.py"]),
                    ("demo-site", ["README.md", "index.html"])):
    d = os.path.join(SEED_DIR, name)
    os.makedirs(d, exist_ok=True)
    for f in files:
        p = os.path.join(d, f)
        if not os.path.exists(p):
            with open(p, "w") as fh:
                fh.write("# " + name + "\n")
    if not os.path.isdir(os.path.join(d, ".git")):
        subprocess.run(["git", "init", "-q"], cwd=d,
                       check=False)
        subprocess.run(["git", "add", "-A"], cwd=d,
                       check=False)
        subprocess.run(
            ["git", "-c", "user.email=ux@example.com",
             "-c", "user.name=ux", "commit", "-qm", "seed"],
            cwd=d, check=False)

entries = [
    {"kind": "system", "at": time.time() - 3600,
     "action": "ux-seed", "ok": True,
     "note": "preview data seeded"},
    {"kind": "run", "at": time.time() - 1800,
     "action": "status", "repo": "demo-app", "ok": True,
     "exit_code": 0, "output": "demo-app up 2 hours"},
    {"kind": "run", "at": time.time() - 600,
     "action": "briefing-td", "repo": "demo-app", "ok": False,
     "exit_code": 1,
     "output": "briefing unavailable: demo td not installed"},
]
with open(LEDGER_PATH, "w") as fh:
    json.dump(entries, fh)
print(f"seeded {SEED_DIR} and {LEDGER_PATH}")

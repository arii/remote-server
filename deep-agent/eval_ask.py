"""Eval gate for ask_investigator.py (manual, not part of unittest).

Runs 5 live prompts against the local model and checks each reply for
the expected tool use + answer anchor (or a well-formed PROPOSE block).
Gate passes only at 5/5.

Usage: eval_ask.py [owner/repo]   (default arii/remote-server)
"""

import base64
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, ".venv", "bin", "python")
SCRIPT = os.path.join(HERE, "ask_investigator.py")

PROMPTS = [
    {
        "name": "docs-howto",
        "prompt": "How do I run the buttons server locally?",
        "tools": ["search_docs"],
        "anchor": re.compile(r"compose|docker|buttons", re.I),
    },
    {
        "name": "code-locate",
        "prompt": "Which file implements POST /buttons/api/ask?",
        "tools": ["grep_repo", "read_repo_file"],
        "anchor": re.compile(r"server\.py"),
    },
    {
        "name": "pr-state",
        "prompt": "What is the state of PR #153?",
        "tools": ["pr_state"],
        "anchor": re.compile(r"153"),
    },
    {
        "name": "propose-issue",
        "prompt": "File an issue titled 'Buttons crash on load' saying "
                  "tapping Run crashes the page.",
        "tools": [],
        "anchor": re.compile(
            r"PROPOSE issue\nTITLE: .+\nBODY:\n.+END PROPOSE", re.S | re.I
        ),
    },
    {
        "name": "propose-td",
        "prompt": "Run the CI fix agent on PR 153.",
        "tools": [],
        "anchor": re.compile(
            r"PROPOSE td\nCOMMAND: td --json agent fix-ci "
            r"--pr 153 --execute\nEND PROPOSE",
            re.I,
        ),
    },
]


def run_case(repo, case):
    b64 = base64.b64encode(case["prompt"].encode()).decode()
    proc = subprocess.run(
        [EXE, SCRIPT, repo, b64],
        capture_output=True,
        text=True,
        timeout=900,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    final = ""
    idx = (proc.stdout or "").find("FINAL:")
    if idx >= 0:
        final = proc.stdout[idx + len("FINAL:"):].strip()
    return proc.returncode, out, final


def last_run_tools():
    path = os.path.join(HERE, "ledger.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except OSError:
            return []
    for row in reversed(rows):
        if row.get("script") == "ask_investigator.py":
            return [c.get("name", "") for c in row.get("tool_calls", [])]
    return []


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "arii/remote-server"
    passed = 0
    for case in PROMPTS:
        print(f"=== {case['name']}: {case['prompt']}", flush=True)
        try:
            code, out, final = run_case(repo, case)
        except subprocess.TimeoutExpired:
            print("TIMEOUT after 900s")
            continue
        ok = True
        if code != 0 or not final:
            print(f"exit={code} empty_final={not final}")
            ok = False
        if case["tools"]:
            used = last_run_tools()
            if not any(t in used for t in case["tools"]):
                print(f"tools MISS: used={used} want one of={case['tools']}")
                ok = False
        if not case["anchor"].search(final):
            print(f"anchor MISS in: {final[:400]}")
            ok = False
        print("PASS" if ok else "FAIL")
        passed += ok
    print(f"\n{passed}/{len(PROMPTS)} passed")
    return 0 if passed == len(PROMPTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

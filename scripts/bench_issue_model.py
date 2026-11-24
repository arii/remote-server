"""Benchmark: replay a queue issue's validated spec through run_coder with a
pinned TwT model (``provider/<id>``). Verifies dispatch correctness (the
gateway serves the pin directly, not a waterfall) and whether the routed
model can complete the coding task.

Usage (inside remote-terminal-1):
  PYTHONPATH=<checkout> /opt/venvs/td/bin/python3 \
    <checkout>/scripts/bench_issue_model.py <provider/id> [--probe] \
    [--base <sha>] [--issue-dir <dir>]

--probe: one trivial chat invoke through the pinned model only (dispatch check).
Full run: fresh worktree at --base + run_coder on <issue-dir>/spec-validated.md.
Results land in /srv/repos/.twt-queue/bench-<issue>-<slug>/ (result.json,
diff.patch, ledger.jsonl, traces).
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

from backend.shared import cli

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("model", help="provider/id pin, e.g. google/gemini-3.1-flash-lite")
parser.add_argument("--probe", action="store_true")
parser.add_argument(
    "--base", default="65b7bd7", help="worktree base (default: pre-#1700 merge)"
)
parser.add_argument(
    "--issue-dir", default="/srv/repos/.twt-queue/arii_remote-server-1571"
)
args = parser.parse_args()

MODEL = args.model
PROBE = args.probe
BASE = args.base
SPEC = os.path.join(args.issue_dir, "spec-validated.md")
issue_name = os.path.basename(args.issue_dir.rstrip("/"))
slug = MODEL.replace("/", "-").replace(":", "-")
run_dir = pathlib.Path(f"/srv/repos/.twt-queue/bench-{issue_name}-{slug}")
run_dir.mkdir(parents=True, exist_ok=True)

os.environ.update(
    {
        "TWT_BASE_URL": "http://twt/v1",
        "TWT_UDS": "/run/twt/gateway.sock",
        "CODER_MODEL": MODEL,
        "CODER_TIMEOUT": "900",
        "DEEP_AGENT_STATE_DIR": str(run_dir),
        "EVAL_LEDGER": str(run_dir / "ledger.jsonl"),
        "S5_ISSUE_PROVIDER": "twt",
        "NEBIUS_ONLY": "1",
        "S5_LOCAL_ONLY": "0",
        "OLLAMA_STRICT": "0",
    }
)
cli.bootstrap()  # deep-agent flat imports; env vars set above
REPO = cli.repo_root()


def _coder():
    # env vars must be set before the langchain import chain runs
    import coder

    return coder


coder = _coder()

worktree = os.path.join(REPO, ".worktrees", f"bench-{slug}")

started = time.time()

if PROBE:
    model, spec = coder.build_model(MODEL)
    reply = model.invoke("Reply with the single word READY.")
    out = {
        "probe": True,
        "model": MODEL,
        "spec_provider": spec.get("provider"),
        "spec_model": spec.get("model"),
        "reply": getattr(reply, "content", str(reply))[:200],
        "elapsed_s": round(time.time() - started, 1),
    }
    print(json.dumps(out))
    (run_dir / "probe.json").write_text(json.dumps(out, indent=2))
    sys.exit(0)

subprocess.run(
    ["git", "-C", REPO, "worktree", "remove", "--force", worktree], check=False
)
subprocess.run(
    ["git", "-C", REPO, "worktree", "add", "--detach", worktree, BASE], check=True
)

brief = pathlib.Path(SPEC).read_text(encoding="utf-8")
meter = {}
outcome = {"model": MODEL, "worktree": worktree}
try:
    state = coder.run_coder(
        worktree,
        brief,
        coder.build_system(worktree, "", "arii/remote-server"),
        f"bench-{issue_name}-{slug}-{time.time_ns()}",
        coder._timeout(),
        repo="arii/remote-server",
        meter=meter,
        issue=issue_name.replace("_", "/", 1),
        run_id=f"bench-{issue_name}-{slug}",
        stage="code",
    )
    outcome["ok"] = True
    outcome["state"] = (
        {
            key: state.get(key)
            for key in ("valid", "fatal", "errors", "drive_exhausted", "worktree_root")
        }
        if isinstance(state, dict)
        else {"raw": str(state)[:500]}
    )
except Exception as exc:  # noqa: BLE001 -- benchmark reports the failure, never raises
    outcome["ok"] = False
    outcome["error"] = f"{type(exc).__name__}: {exc}"

# what actually changed in the worktree
diff = subprocess.run(
    ["git", "-C", worktree, "status", "--porcelain"],
    check=False,
    capture_output=True,
    text=True,
)
outcome["changed_files"] = diff.stdout.split()
outcome["meter"] = meter
outcome["elapsed_s"] = round(time.time() - started, 1)
(run_dir / "result.json").write_text(json.dumps(outcome, indent=2))
print(json.dumps(outcome, indent=1)[:3000])

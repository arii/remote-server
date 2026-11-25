"""Benchmark: replay a queue issue's validated spec through run_coder with a
pinned model, then judge the task by the spec's own verification commands.

Routes (``--route``):
  twt     the pin (``provider/<id>``) goes to TwT ``/v1/chat/completions``; the
          gateway serves that model only, no waterfall.
  native  ``S5_LOCAL_ONLY=1``: the coder uses native Ollama chat through TwT's
          ``/local/api/chat`` route (pin ``ollama-local/<model>``).

``--strict-local`` (native route only) disables the
strong-tier and default-route fallbacks, refuses hosted model construction,
and rejects non-local chat requests in the transport hook before network I/O.

``valid`` is true only when the reply ends ``DONE #<n>``, files changed and
every verification command exits 0. ``run_returned`` only says run_coder did
not raise. ``gateway`` lists the TwT receipts for the run's trace IDs; a run
with no trace IDs has no gateway proof.

Usage (inside remote-terminal-1):
  PYTHONPATH=<checkout> /opt/venvs/td/bin/python3 \
    <checkout>/scripts/bench_issue_model.py <provider/id> [--probe] \
    [--route twt|native] [--strict-local] [--base <sha>] [--issue-dir <dir>]

Results land in /srv/repos/.twt-queue/bench-<issue>-<route>-<slug>/
(result.json, diff.patch, ledger.jsonl, traces).
"""

import argparse
import json
import math
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

LOCAL_PREFIX = "ollama-local/"
STRICT_LOCAL_CAP_USD = 0.0
VERIFY_TIMEOUT_S = 300


def verification_commands(spec):
    """Commands in the spec's ``## Verification Commands`` fenced block."""
    match = re.search(
        r"^##\s+Verification Commands\s*\n+```[^\n]*\n(.*?)^```",
        spec,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    return [
        line.strip()
        for line in match.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def reply_text(result):
    """run_coder returns a tuple whose first item is the final reply text."""
    if isinstance(result, tuple) and result and isinstance(result[0], str):
        return result[0]
    if isinstance(result, str):
        return result
    return ""


def terminal(reply):
    lines = reply.strip().splitlines()
    match = re.fullmatch(r"(DONE|BLOCKED) #\d+", lines[-1].strip()) if lines else None
    return match.group(1) if match else None


def judge(outcome):
    """True only for a finished, verified task; never for a run that just returned."""
    checks = outcome.get("verification") or []
    return bool(
        outcome.get("run_returned")
        and outcome.get("terminal") == "DONE"
        and outcome.get("changed_files")
        and checks
        and all(check["exit"] == 0 for check in checks)
        and outcome.get("gateway", {}).get("proof") is True
        and not outcome.get("strict_violations")
    )


def gateway_proof(trace_ids, local_ids, receipts, expected_model=None):
    rows = [row for row in receipts if isinstance(row, dict)]
    ids = [row.get("trace_id") for row in rows]
    identities = bool(trace_ids) and len(ids) == len(set(ids)) and set(ids) == set(trace_ids)
    known = all(
        row.get("cost_complete") is True
        and type(row.get("cost_usd")) in (int, float)
        and math.isfinite(row["cost_usd"])
        and row["cost_usd"] >= 0
        for row in rows
    )
    served = sorted({f"{row.get('provider')}/{row.get('model')}" for row in rows})
    pinned = bool(served) and (expected_model is None or served == [expected_model])
    return {
        "trace_ids": len(trace_ids),
        "local_route_trace_ids": len(local_ids),
        "receipts": len(rows),
        "served": served,
        "cost_usd": sum(row["cost_usd"] for row in rows) if known else None,
        "proof": identities and known and pinned,
    }


def strict_violations(route, gateway):
    """Anything that shows a non-local model served the run."""
    violations = []
    if not gateway["trace_ids"]:
        violations.append("no TwT trace IDs: run did not go through the gateway")
    elif not gateway["proof"]:
        violations.append("a trace has no TwT receipt")
    for name in gateway["served"]:
        if not name.startswith(LOCAL_PREFIX):
            violations.append("non-local model served: " + name)
    if gateway["cost_usd"] is None:
        violations.append("unknown receipt cost")
    elif gateway["cost_usd"]:
        violations.append(f"nonzero cost: {gateway['cost_usd']}")
    return violations


def install_strict_local_guard(router, receipts):
    """Deny hosted model construction and non-local chat dispatch before I/O."""
    original = receipts.capture

    def reject_remote(*args, **kwargs):
        raise RuntimeError("strict local benchmark refuses hosted model construction")

    def capture(request):
        if request.url.path.endswith(("/chat/completions", "/api/chat")):
            if request.url.path != "/local/api/chat":
                raise RuntimeError("strict local benchmark refuses non-local chat dispatch")
        return original(request)

    async def acapture(request):
        return capture(request)

    router._remote_spec = reject_remote
    receipts.capture = capture
    receipts.acapture = acapture


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", help="provider/id pin, e.g. ollama-local/qwen3:8b")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--route", choices=("twt", "native"), default="twt")
    parser.add_argument("--strict-local", action="store_true")
    parser.add_argument(
        "--base", default="65b7bd7", help="worktree base (default: pre-#1700 merge)"
    )
    parser.add_argument(
        "--issue-dir", default="/srv/repos/.twt-queue/arii_remote-server-1571"
    )
    args = parser.parse_args(argv)
    if args.route == "native" and not args.model.startswith(LOCAL_PREFIX):
        parser.error("--route native needs an ollama-local/<model> pin")
    if args.strict_local and args.route != "native":
        # TwT's hosted /v1 route does not serve ollama-local pins; local runs go
        # through its native /local/api/chat route.
        parser.error("--strict-local needs --route native")
    if args.route == "twt" and args.model.startswith(LOCAL_PREFIX):
        parser.error("ollama-local pins are served only by --route native")
    return args


def environment(args, run_dir):
    env = {
        "TWT_BASE_URL": "http://twt/v1",
        "TWT_UDS": "/run/twt/gateway.sock",
        "CODER_TIMEOUT": "900",
        "DEEP_AGENT_STATE_DIR": str(run_dir),
        "EVAL_LEDGER": str(run_dir / "ledger.jsonl"),
        "S5_ISSUE_PROVIDER": "twt",
        "NEBIUS_ONLY": "1",
        "S5_LOCAL_ONLY": "0",
        "OLLAMA_STRICT": "0",
        "CODER_MODEL": args.model,
    }
    if args.route == "native":
        env.update(
            S5_LOCAL_ONLY="1",
            NEBIUS_ONLY="0",
            S5_ISSUE_PROVIDER="ollama",
            OLLAMA_STRICT="1",
            ASK_MODEL=args.model[len(LOCAL_PREFIX) :],
            CODER_MODEL="",
        )
    if args.strict_local:
        # No strong-tier escalation and no default-route fallback model.
        env.update(CODER_FALLBACK_MODEL="")
    return env


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    issue_name = os.path.basename(args.issue_dir.rstrip("/"))
    slug = args.model.replace("/", "-").replace(":", "-")
    run_dir = pathlib.Path(
        f"/srv/repos/.twt-queue/bench-{issue_name}-{args.route}-{slug}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(environment(args, run_dir))
    if args.strict_local:
        os.environ.pop("CODER_STRONG_MODEL", None)

    from backend.shared import cli

    cli.bootstrap()  # deep-agent flat imports; env vars set above
    repo = cli.repo_root()
    import coder  # env vars must be set before the langchain import chain runs
    from backend.shared import twt_receipts
    from backend.shared import model_router

    fatal, stops = [], []
    if args.strict_local:
        install_strict_local_guard(model_router, twt_receipts)

    worktree = os.path.join(repo, ".worktrees", f"bench-{args.route}-{slug}")
    started = time.time()
    run_id = f"bench-{issue_name}-{args.route}-{slug}"
    outcome = {"model": args.model, "route": args.route, "worktree": worktree}
    outcome["strict_local"] = args.strict_local

    with twt_receipts.run(run_id) as scope:
        if args.probe:
            model, spec = coder.build_model(os.environ["CODER_MODEL"] or None)
            reply = model.invoke("Reply with the single word READY.")
            outcome.update(
                probe=True,
                spec_provider=spec.get("provider"),
                spec_model=spec.get("model"),
                reply=getattr(reply, "content", str(reply))[:200],
            )
        else:
            subprocess.run(
                ["git", "-C", repo, "worktree", "remove", "--force", worktree],
                check=False,
            )
            subprocess.run(
                ["git", "-C", repo, "worktree", "add", "--detach", worktree, args.base],
                check=True,
            )
            spec_text = pathlib.Path(args.issue_dir, "spec-validated.md").read_text(
                encoding="utf-8"
            )
            meter = {}
            try:
                result = coder.run_coder(
                    worktree,
                    spec_text,
                    coder.build_system(worktree, "", "arii/remote-server"),
                    f"{run_id}-{time.time_ns()}",
                    coder._timeout(),
                    repo="arii/remote-server",
                    meter=meter,
                    issue=issue_name.replace("_", "/", 1),
                    run_id=run_id,
                    stage="code",
                )
                outcome["run_returned"] = True
                outcome["terminal"] = terminal(reply_text(result))
            except Exception as exc:  # noqa: BLE001 -- benchmark reports, never raises
                outcome["run_returned"] = False
                outcome["error"] = f"{type(exc).__name__}: {exc}"
            outcome["meter"] = meter
    trace_ids, local_ids = set(scope.ids), set(scope.local_ids)
    receipts = (
        twt_receipts.fetch(scope.base_url, sorted(trace_ids), uds=scope.uds)
        if trace_ids
        else []
    )
    outcome["gateway"] = gateway_proof(trace_ids, local_ids, receipts, args.model)
    outcome["guard"] = {"stops": stops, "fatal": fatal}

    if not args.probe:
        status = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        outcome["changed_files"] = [
            line[3:] for line in status.stdout.splitlines() if line.strip()
        ]
        diff = subprocess.run(
            ["git", "-C", worktree, "diff"], capture_output=True, text=True, check=False
        )
        (run_dir / "diff.patch").write_text(diff.stdout)
        checks = []
        for command in verification_commands(spec_text):
            began = time.time()
            try:
                done = subprocess.run(
                    shlex.split(command),
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=VERIFY_TIMEOUT_S,
                    check=False,
                )
                code = done.returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
            checks.append(
                {"command": command, "exit": code, "s": round(time.time() - began, 1)}
            )
        outcome["verification"] = checks
    if args.strict_local:
        outcome["strict_violations"] = strict_violations(args.route, outcome["gateway"])
    outcome["valid"] = False if args.probe else judge(outcome)
    outcome["elapsed_s"] = round(time.time() - started, 1)
    name = "probe.json" if args.probe else "result.json"
    (run_dir / name).write_text(json.dumps(outcome, indent=2, default=str))
    print(json.dumps(outcome, indent=1, default=str)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

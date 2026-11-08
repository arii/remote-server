"""ops router: terminal takeover, workflow health, clear, health.

Ports of the buttons/server.py handlers, under the backend's /api
prefix convention (buttons serves /buttons/api/*; this backend serves
/api/* like its sibling routers):

- POST /api/terminal/takeover  disconnect the ttyd client so this
  window can take over the terminal session (SIGHUP to zellij attach)
- GET  /api/workflow-health?repo=  failing-run diagnosis (RepoAuditor
  parity: annotations, failed jobs/steps, diagnosed cause)
- POST /api/clear  clear stored run output only (no cancel), same
  active-or-newest default and 404 as the original
- GET  /api/health  read-only box health (disk/memory/containers)

Takeover and workflow-health shell out through injectable module
functions so tests never touch docker or the network.
"""

import json
import logging
import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core.containers import get_system_health
from backend.core.env_config import (
    briefing_fixture_candidate,
    resolve_briefing_fixture_path,
)
from backend.core.validation import require_confirm, validate_repo_name
from backend.ledger import append as ledger_append

router = APIRouter(prefix="/api", tags=["ops"])

logger = logging.getLogger(__name__)


def run_takeover() -> str:
    """SIGHUP the zellij attach client inside the terminal container.

    zellij lives in remote-terminal-1, not here: pkill must run over
    there (compose exec through the mounted docker socket). The
    pattern is start-anchored so it hits only the attach client,
    never ttyd (whose argv contains the same words) nor the zellij
    server. The hup_exit marker tells callers whether a client was
    actually signaled (0) or none matched (1). Injectable for tests.
    """
    from backend.exec.engine import exec_argv

    proc = subprocess.run(
        exec_argv("pkill -HUP -f '^zellij attach'; echo hup_exit=$?"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, text=True)
    return proc.stdout


def _fetch_run_jobs(gh: str, run_id) -> list | None:
    """Jobs payload for one failing run; None when the call fails."""
    from backend.exec.engine import td_run

    try:
        jobs_out = td_run(
            f"gh api repos/{shlex.quote(gh)}/actions/runs/"
            f"{run_id}/jobs", timeout=30)
        jobs = json.loads(jobs_out).get("jobs", [])
        return jobs if isinstance(jobs, list) else []
    except Exception:
        return None


def _fetch_annotations(gh: str, check_id) -> list:
    """First-5 mapped annotations for one check run; [] on any failure.

    Partial results are kept when a malformed entry aborts the loop,
    matching the historical inline behavior.
    """
    from backend.exec.engine import td_run

    out: list = []
    try:
        ann_out = td_run(
            f"gh api repos/{shlex.quote(gh)}/check-runs/"
            f"{check_id}/annotations", timeout=20)
        ann_list = json.loads(ann_out)
        if isinstance(ann_list, list):
            for a in ann_list[:5]:
                out.append({
                    "path": a.get("path"),
                    "start_line": a.get("start_line"),
                    "annotation_level": a.get("annotation_level"),
                    "message": a.get("message"),
                    "title": a.get("title")})
    except Exception:
        pass
    return out


def fetch_workflow_health(repo: str, gh: str) -> dict:
    """Live workflow-health analysis via td/gh. Injectable for tests.

    Mirrors buttons/server.py: list workflow runs, keep failures,
    then per failing run (max 5) collect failed jobs/steps and
    check-run annotations, diagnosing the cause from the first
    annotation or the first failed job.

    The per-run jobs calls and the per-job annotation calls run
    concurrently on a shared pool (assembly stays ordered), so a
    5-failure load costs ~3 GH round trips instead of ~11 sequential
    subprocess calls (#432). Content and error semantics match the
    previous sequential version exactly.
    """
    from backend.exec.engine import td_run

    # Explicit --repo: never depend on td's upward config discovery
    # from the worker cwd (a missing github_repo reads as a silent
    # exit-1 with empty stderr in CI).
    runs_out = td_run(
        f"td --json repo workflow-runs --repo {shlex.quote(gh)}",
        timeout=45)
    runs_data = json.loads(runs_out)
    runs_list = (runs_data.get("workflow_runs")
                 or runs_data.get("workflowRuns") or [])
    # Repo-level signal only (issue #1150): pull_request-event runs are
    # branch CI for unmerged PRs, not the repo's health. Surfacing them
    # pins PR-branch failures (e.g. a release-train PR) as crit cards
    # on the repo feed while main stays green. Push, schedule, and
    # dispatch events still page.
    failing = [r for r in runs_list
               if (r.get("conclusion") == "failure"
                   or (r.get("conclusion") is None
                       and r.get("status") == "failure"))
               and r.get("event") != "pull_request"][:5]
    run_ids = [fr.get("id") for fr in failing]
    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs_futs = [pool.submit(_fetch_run_jobs, gh, rid)
                     if rid else None for rid in run_ids]
        jobs_payloads = [f.result() if f is not None else None
                         for f in jobs_futs]
        ann_futs: dict[tuple[int, int], object] = {}
        for idx, jobs in enumerate(jobs_payloads):
            if not jobs:
                continue
            for pos, job in enumerate(jobs):
                try:
                    failed = job.get("conclusion") == "failure"
                    check_id = job.get("id") if failed else None
                except Exception:
                    break
                if check_id:
                    ann_futs[(idx, pos)] = pool.submit(
                        _fetch_annotations, gh, check_id)
        ann_payloads = {k: f.result() for k, f in ann_futs.items()}
    workflows = []
    for idx, fr in enumerate(failing):
        failed_jobs: list = []
        annotations: list = []
        diagnosed = "Workflow failed."
        jobs = jobs_payloads[idx]
        if jobs:
            for pos, job in enumerate(jobs):
                try:
                    if job.get("conclusion") != "failure":
                        continue
                    steps = [s.get("name", "?")
                             for s in (job.get("steps") or [])
                             if s.get("conclusion") == "failure"]
                    failed_jobs.append({"name": job.get("name", "?"),
                                        "conclusion": "failure",
                                        "failed_steps": steps})
                    if job.get("id"):
                        annotations.extend(
                            ann_payloads.get((idx, pos), []))
                except Exception:
                    break
        if annotations:
            first = annotations[0]
            diagnosed = (f"[{first.get('path', 'unknown')}:"
                         f"{first.get('start_line', '?')}] "
                         f"{first.get('message', '')}")
        elif failed_jobs:
            first = failed_jobs[0]
            steps = (": " + ", ".join(first["failed_steps"])
                     if first["failed_steps"] else "")
            diagnosed = f"Job '{first['name']}' failed{steps}."
        workflows.append({
            "id": run_ids[idx],
            "name": fr.get("name", "Workflow"),
            "branch": fr.get("head_branch") or fr.get("headBranch") or "?",
            "event": fr.get("event", "?"),
            "conclusion": "failure",
            "url": fr.get("html_url") or fr.get("url"),
            "created_at": fr.get("created_at") or fr.get("createdAt"),
            "updated_at": fr.get("updated_at") or fr.get("updatedAt"),
            "diagnosed_cause": diagnosed,
            "annotations": annotations,
            "jobs": failed_jobs,
        })
    return {"repo": repo,
            "status": "failing" if workflows else "healthy",
            "total_failing": len(workflows),
            "workflows": workflows}


def _fixture_workflow_health(repo: str) -> dict | None:
    """Fixture override for preview/seed environments (#453).

    Same rule as the briefing route: the explicit
    BUTTONS_FIXTURE_BRIEFING path always wins; a
    fixture-briefing.json next to the checkouts only applies with
    the BUTTONS_FIXTURE_AUTO_DISCOVER opt-in. None when no fixture
    applies (live serves). Fixture serves are marked and logged —
    never silent.
    """
    fixture_path = resolve_briefing_fixture_path()
    if fixture_path and not os.path.exists(fixture_path):
        logger.warning(
            "workflow-health: fixture %s for repo %s missing; "
            "serving live", fixture_path, repo)
        return None
    if not fixture_path:
        stray = briefing_fixture_candidate()
        if os.path.exists(stray):
            logger.warning(
                "workflow-health: ignoring stray fixture %s for repo %s "
                "(set BUTTONS_FIXTURE_BRIEFING or "
                "BUTTONS_FIXTURE_AUTO_DISCOVER=1 to serve fixtures)",
                stray, repo)
        return None
    try:
        with open(fixture_path, encoding="utf-8") as fh:
            fix_data = json.load(fh)
    except Exception:
        return None
    items = []
    for r in fix_data.get("failing_runs", []):
        items.append({
            "id": 101,
            "name": r.get("name", "CI"),
            "branch": r.get("branch", "main"),
            "event": r.get("event", "push"),
            "conclusion": "failure",
            "url": r.get("url", "https://github.com/example/demo-app/"
                                "actions/runs/101"),
            "created_at": r.get("created_at") or r.get("createdAt"),
            "updated_at": r.get("updated_at") or r.get("updatedAt"),
            "diagnosed_cause": "Browser context crashed during Playwright "
                               "launch in e2e-tests step.",
            "annotations": [{
                "path": "tests/test_ui.py",
                "start_line": 42,
                "annotation_level": "failure",
                "message": "Error: Playwright connection refused: chromium "
                           "executable missing at /root/.cache/ms-playwright",
                "title": "Browser Launch Exception"}],
            "jobs": [{"name": "test-e2e", "conclusion": "failure",
                      "failed_steps": ["run playwright test"]}],
        })
    logger.warning(
        "workflow-health: serving fixture %s for repo %s",
        fixture_path, repo)
    return {"repo": repo,
            "status": "failing" if items else "healthy",
            "total_failing": len(items),
            "workflows": items,
            "fixture": True,
            "fixture_source": fixture_path}


class TakeoverResult(BaseModel):
    ok: bool = True
    matched: bool = True
    output: str = ""


class WorkflowHealth(BaseModel):
    repo: str = ""
    status: str = ""
    total_failing: int = 0
    workflows: list = Field(default_factory=list)
    # Fixture marker (#453): True + source path when canned data
    # serves. Live responses carry the False default.
    fixture: bool = False
    fixture_source: str = ""


class ClearRequest(BaseModel):
    id: str | None = None
    intent: str = ""
    confirmed: bool = False


class TakeoverRequest(BaseModel):
    intent: str = ""
    confirmed: bool = False


class ClearResult(BaseModel):
    ok: bool = True
    id: str = ""


class SystemHealth(BaseModel):
    disk: dict = Field(default_factory=dict)
    memory: dict = Field(default_factory=dict)
    containers: list = Field(default_factory=list)
    containers_error: str | None = None


@router.post("/terminal/takeover", response_model=TakeoverResult)
def takeover(body: TakeoverRequest) -> dict:
    require_confirm(body, "terminal-takeover")
    try:
        out = run_takeover()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    # Truth in response (#431): the hup_exit marker tells whether a
    # client was actually signaled (0) or none matched (1). ok mirrors
    # it so a no-match takeover no longer reports success. A missing
    # marker (unreachable in the real path — the echo always runs)
    # keeps the historical ok:true contract.
    marker = re.search(r"hup_exit=(\d+)", out or "")
    matched = (int(marker.group(1)) == 0) if marker else True
    ledger_append({"kind": "system",
                   "event": "terminal session takeover requested"})
    return {"ok": matched, "matched": matched, "output": out}


# TTL cache: the gh subprocess chain behind this endpoint fans out per
# failing run; the History view fetches it on every visit. Freshness of
# a minute is plenty for a diagnosis list. Only successful live fetches
# are cached (never fixtures, never errors). Tests reset the cache
# directly.
WORKFLOW_HEALTH_CACHE_TTL = 60.0
_WORKFLOW_HEALTH_CACHE: dict[str, tuple[float, dict]] = {}


@router.get("/workflow-health", response_model=WorkflowHealth)
def workflow_health(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    from backend.core.git_helpers import get_repo_status

    try:
        get_repo_status(name)
    except (ValueError, FileNotFoundError, RuntimeError):
        raise HTTPException(status_code=400, detail="unknown repo")
    fixture = _fixture_workflow_health(name)
    if fixture is not None:
        return fixture
    hit = _WORKFLOW_HEALTH_CACHE.get(name)
    if hit is not None and time.time() - hit[0] < WORKFLOW_HEALTH_CACHE_TTL:
        return hit[1]
    try:
        data = fetch_workflow_health(name, f"arii/{name}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    _WORKFLOW_HEALTH_CACHE[name] = (time.time(), data)
    return data


@router.post("/clear", response_model=ClearResult)
def clear(body: ClearRequest) -> dict:
    from backend.api.routers.jobs import registry
    from backend.core.validation import require_confirm

    require_confirm(body, "run-clear")

    run_id, err = registry.clear_run(body.id)
    if err:
        raise HTTPException(status_code=404, detail=err)
    return {"ok": True, "id": run_id}


@router.get("/health", response_model=SystemHealth)
def health() -> dict:
    return get_system_health()

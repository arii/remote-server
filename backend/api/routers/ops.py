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
import os
import shlex
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core import get_system_health, require_confirm, validate_repo_name
from backend.ledger import append as ledger_append

router = APIRouter(prefix="/api", tags=["ops"])


def run_takeover() -> str:
    """Send SIGHUP to the zellij attach client. Injectable for tests."""
    proc = subprocess.run(
        ["bash", "-c", "pkill -HUP -f 'zellij attach' || true"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, text=True)
    return proc.stdout


def fetch_workflow_health(repo: str, gh: str) -> dict:
    """Live workflow-health analysis via td/gh. Injectable for tests.

    Mirrors buttons/server.py: list workflow runs, keep failures,
    then per failing run (max 5) collect failed jobs/steps and
    check-run annotations, diagnosing the cause from the first
    annotation or the first failed job.
    """
    from backend.exec.engine import td_run

    runs_out = td_run("td --json repo workflow-runs", timeout=45)
    runs_data = json.loads(runs_out)
    runs_list = (runs_data.get("workflow_runs")
                 or runs_data.get("workflowRuns") or [])
    failing = [r for r in runs_list
               if r.get("conclusion") == "failure"
               or (r.get("conclusion") is None
                   and r.get("status") == "failure")]
    workflows = []
    for fr in failing[:5]:
        run_id = fr.get("id")
        failed_jobs: list = []
        annotations: list = []
        diagnosed = "Workflow failed."
        if run_id:
            try:
                jobs_out = td_run(
                    f"gh api repos/{shlex.quote(gh)}/actions/runs/"
                    f"{run_id}/jobs", timeout=30)
                for job in json.loads(jobs_out).get("jobs", []):
                    if job.get("conclusion") != "failure":
                        continue
                    steps = [s.get("name", "?")
                             for s in (job.get("steps") or [])
                             if s.get("conclusion") == "failure"]
                    failed_jobs.append({"name": job.get("name", "?"),
                                        "conclusion": "failure",
                                        "failed_steps": steps})
                    check_id = job.get("id")
                    if check_id:
                        try:
                            ann_out = td_run(
                                f"gh api repos/{shlex.quote(gh)}/check-runs/"
                                f"{check_id}/annotations", timeout=20)
                            ann_list = json.loads(ann_out)
                            if isinstance(ann_list, list):
                                for a in ann_list[:5]:
                                    annotations.append({
                                        "path": a.get("path"),
                                        "start_line": a.get("start_line"),
                                        "annotation_level": a.get(
                                            "annotation_level"),
                                        "message": a.get("message"),
                                        "title": a.get("title")})
                        except Exception:
                            pass
            except Exception:
                pass
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
            "id": run_id,
            "name": fr.get("name", "Workflow"),
            "branch": fr.get("head_branch") or fr.get("headBranch") or "?",
            "event": fr.get("event", "?"),
            "conclusion": "failure",
            "url": fr.get("html_url") or fr.get("url"),
            "diagnosed_cause": diagnosed,
            "annotations": annotations,
            "jobs": failed_jobs,
        })
    return {"repo": repo,
            "status": "failing" if workflows else "healthy",
            "total_failing": len(workflows),
            "workflows": workflows}


def _fixture_workflow_health(repo: str) -> dict | None:
    """Fixture override for preview/seed environments, same rule as
    buttons: BUTTONS_FIXTURE_BRIEFING or a fixture-briefing.json next
    to the repo checkouts. None when no fixture applies."""
    fixture_path = os.environ.get("BUTTONS_FIXTURE_BRIEFING", "")
    if not fixture_path:
        candidate = os.path.join(
            os.environ.get("BUTTONS_REPOS_DIR", "/home/ari/repos"),
            "fixture-briefing.json")
        if os.path.exists(candidate):
            fixture_path = candidate
    if not (fixture_path and os.path.exists(fixture_path)):
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
    return {"repo": repo,
            "status": "failing" if items else "healthy",
            "total_failing": len(items),
            "workflows": items}


class TakeoverResult(BaseModel):
    ok: bool = True
    output: str = ""


class WorkflowHealth(BaseModel):
    repo: str = ""
    status: str = ""
    total_failing: int = 0
    workflows: list = Field(default_factory=list)


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


@router.post("/terminal/takeover", response_model=TakeoverResult)
def takeover(body: TakeoverRequest) -> dict:
    require_confirm(body, "terminal-takeover")
    try:
        out = run_takeover()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])
    ledger_append({"kind": "system",
                   "event": "terminal session takeover requested"})
    return {"ok": True, "output": out}


@router.get("/workflow-health", response_model=WorkflowHealth)
def workflow_health(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    from backend.core import get_repo_status

    try:
        get_repo_status(name)
    except (ValueError, FileNotFoundError, RuntimeError):
        raise HTTPException(status_code=400, detail="unknown repo")
    fixture = _fixture_workflow_health(name)
    if fixture is not None:
        return fixture
    try:
        return fetch_workflow_health(name, f"arii/{name}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])


@router.post("/clear", response_model=ClearResult)
def clear(body: ClearRequest) -> dict:
    from backend.api.routers.jobs import registry
    from backend.core import require_confirm

    require_confirm(body, "run-clear")

    run_id, err = registry.clear_run(body.id)
    if err:
        raise HTTPException(status_code=404, detail=err)
    return {"ok": True, "id": run_id}


@router.get("/health", response_model=SystemHealth)
def health() -> dict:
    return get_system_health()

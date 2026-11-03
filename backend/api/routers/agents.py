"""agents router: agent runs listing.

Runs shape: {"runs": [<runs.py summaries>]}, newest first, stable
run-<ledger-line> ids. Run detail adds the ledger entry plus its
trace events.
"""

import os
import sys

from fastapi import APIRouter
from pydantic import BaseModel

from fastapi import HTTPException

sys.path.insert(0, os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "deep-agent"))

import runs  # noqa: E402

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentsHealth(BaseModel):
    ok: bool = True


class ConfirmRequest(BaseModel):
    intent: str = ""
    confirmed: bool = False


class ClearRunsResult(BaseModel):
    ok: bool = True
    cleared: int = 0


class DeleteRunResult(BaseModel):
    ok: bool = True
    id: str = ""


@router.get("/health", response_model=AgentsHealth)
def health() -> dict:
    return {"ok": True}


@router.get("/runs")
def agent_runs(limit: int = 50) -> dict:
    return {"runs": runs.list_runs(limit)}


@router.get("/runs/{run_id}")
def agent_run(run_id: str) -> dict:
    row = runs.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return row


@router.post("/runs/clear", response_model=ClearRunsResult)
def clear_agent_runs(body: ConfirmRequest) -> dict:
    from backend.core import require_confirm

    require_confirm(body, "agents-clear")
    return {"ok": True, "cleared": runs.clear_finished_runs()}


@router.delete("/runs/{run_id}", response_model=DeleteRunResult)
def delete_agent_run(run_id: str, body: ConfirmRequest) -> dict:
    from backend.core import require_confirm

    require_confirm(body, "agents-delete")
    if not runs.delete_run(run_id):
        raise HTTPException(status_code=404, detail="unknown run")
    return {"ok": True, "id": run_id}

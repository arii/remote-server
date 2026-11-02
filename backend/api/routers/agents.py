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

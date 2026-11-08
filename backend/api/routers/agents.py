"""agents router: agent runs listing.

Runs shape: {"runs": [<runs.py summaries>]}, newest first, stable
run-<ledger-line> ids. Run detail adds the ledger entry plus its
trace events. Summaries carry provider + latency + cost + repo
slots, null-safe for local/unmetered turns and pre-repo rows.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from fastapi import HTTPException

import runs  # noqa: E402
import pricing  # noqa: E402

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


class BudgetStatus(BaseModel):
    cap_usd: float | None = None
    spent_usd: float = 0.0
    remaining_usd: float | None = None
    blocked: bool = False
    unknown: int = 0


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


@router.get("/budget", response_model=BudgetStatus)
def agent_budget() -> dict:
    cap = pricing.spend_cap()
    total = runs.total_metered_spend()
    spent = total["spent"]
    # Mirror the router trip: unrated rows block when capped.
    blocked = cap is not None and (
        spent >= cap or total["unknown"] > 0)
    return {"cap_usd": cap, "spent_usd": spent,
            "remaining_usd": round(max(0.0, cap - spent), 6)
            if cap is not None else None,
            "blocked": blocked, "unknown": total["unknown"]}


@router.post("/runs/clear", response_model=ClearRunsResult)
def clear_agent_runs(body: ConfirmRequest) -> dict:
    from backend.core.validation import require_confirm

    require_confirm(body, "agents-clear")
    return {"ok": True, "cleared": runs.clear_finished_runs()}


@router.delete("/runs/{run_id}", response_model=DeleteRunResult)
def delete_agent_run(run_id: str, body: ConfirmRequest) -> dict:
    from backend.core.validation import require_confirm

    require_confirm(body, "agents-delete")
    if not runs.delete_run(run_id):
        raise HTTPException(status_code=404, detail="unknown run")
    return {"ok": True, "id": run_id}

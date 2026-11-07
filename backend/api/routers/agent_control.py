"""Agent co-browsing pause/resume control (follow_up Issue 11).

Atomic state transitions (RUNNING <-> PAUSED_BY_USER) backed by a
shared JSON state file, so FastAPI and CLI worker processes
(deep-agent/pipeline.py) read and write the same lock. Compose sets
AGENT_CONTROL_STATE_FILE to /var/lib/buttons/agent_control_state.json
on buttons and terminal (shared buttons-data volume); /tmp is
per-container and would split-brain the pause (#1104).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/agent", tags=["agent_control"])


def state_file() -> Path:
    """Resolve the state file at call time so tests can redirect it."""
    return Path(os.environ.get(
        "AGENT_CONTROL_STATE_FILE", "/tmp/agent_control_state.json"))


class AgentControlState(BaseModel):
    state: Literal["RUNNING", "PAUSED_BY_USER", "IDLE"] = "IDLE"
    active_issue: int | None = None
    current_step: str = ""
    requires_dom_refresh: bool = False
    updated_at: float = 0.0
    # Agent-initiated handoff (#1097): the agent is parked waiting for
    # the human (login wall, MFA, CAPTCHA, ambiguous UI). Orthogonal to
    # `state` so pause/resume semantics stay untouched; the drawer gives
    # a pending handoff visual priority over the paused banner.
    handoff_reason: str | None = None
    handoff_url: str | None = None
    handoff_requested_at: float | None = None


class HandoffRequest(BaseModel):
    reason: str
    current_url: str | None = None


class HandoffResolve(BaseModel):
    note: str | None = None


def read_control_state() -> AgentControlState:
    path = state_file()
    if not path.exists():
        return AgentControlState(updated_at=time.time())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AgentControlState(**data)
    except Exception:
        return AgentControlState(updated_at=time.time())


def write_control_state(state: AgentControlState) -> AgentControlState:
    state.updated_at = time.time()
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.model_dump(), indent=2),
                    encoding="utf-8")
    return state


@router.get("/state")
def get_agent_state() -> dict[str, Any]:
    return read_control_state().model_dump()


@router.post("/pause")
def pause_agent(reason: str = "user_takeover") -> dict[str, Any]:
    current = read_control_state()
    current.state = "PAUSED_BY_USER"
    current.current_step = f"Paused ({reason})"
    current.requires_dom_refresh = True
    return write_control_state(current).model_dump()


@router.post("/resume")
def resume_agent() -> dict[str, Any]:
    current = read_control_state()
    current.state = "RUNNING"
    current.current_step = "Resuming - mandatory DOM re-snapshot scheduled"
    current.requires_dom_refresh = True
    return write_control_state(current).model_dump()


def handoff_pending(state: AgentControlState) -> bool:
    """True while an agent-initiated handoff awaits the human."""
    return state.handoff_reason is not None


@router.post("/handoff/request")
def request_handoff(body: HandoffRequest) -> dict[str, Any]:
    """Park the agent on a human handoff (login wall, MFA, CAPTCHA,
    ambiguous UI). Idempotent: re-requesting refreshes the reason and
    timestamp without duplicating state (interrupt re-fires on resume).
    """
    reason = (body.reason or "").strip()
    if not reason:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="reason is required")
    current = read_control_state()
    current.state = "PAUSED_BY_USER"
    current.current_step = f"Waiting for human handoff: {reason[:120]}"
    current.requires_dom_refresh = True
    current.handoff_reason = reason[:500]
    current.handoff_url = (body.current_url or "").strip() or None
    current.handoff_requested_at = time.time()
    return write_control_state(current).model_dump()


@router.post("/handoff/resolve")
def resolve_handoff(body: HandoffResolve) -> dict[str, Any]:
    """Human acted: clear the handoff and resume with a mandatory DOM
    re-snapshot so the viewer shows the post-handoff state. Idempotent:
    resolving with nothing pending is a no-op success.
    """
    current = read_control_state()
    current.handoff_reason = None
    current.handoff_url = None
    current.handoff_requested_at = None
    current.state = "RUNNING"
    current.current_step = "Resuming - mandatory DOM re-snapshot scheduled"
    current.requires_dom_refresh = True
    return write_control_state(current).model_dump()

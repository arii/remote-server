"""Agent co-browsing pause/resume control (follow_up Issue 11).

Atomic state transitions (RUNNING <-> PAUSED_BY_USER) backed by a
shared JSON state file, so FastAPI and CLI worker processes
(deep-agent/pipeline.py) read and write the same lock. Override the
file location with AGENT_CONTROL_STATE_FILE when backend and workers
do not share /tmp (e.g. point both at a checkout-relative path).
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

"""Shared human-wheel gate for live driving and skill replay."""

from fastapi import HTTPException

from backend.api.routers import agent_control

GATE_VERSION = "human-wheel-v1"


def require_human_not_driving() -> None:
    state = agent_control.read_control_state()
    if state.state == "PAUSED_BY_USER":
        raise HTTPException(
            status_code=409,
            detail="you have the wheel — human is driving (agent paused)",
        )

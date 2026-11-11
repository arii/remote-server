"""Skill store API (issue #1257): list, read, and save skills."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import skill_runs, skill_store

router = APIRouter(prefix="/api/browser/skills", tags=["browser_skills"])


class SkillSave(BaseModel):
    skill: dict[str, Any]


@router.get("/")
async def list_all() -> dict[str, Any]:
    """Every stored skill name with its latest version."""
    return {"ok": True, "skills": skill_store.list_skills()}


@router.get("/{name}")
async def read_one(name: str) -> dict[str, Any]:
    """Latest version of one skill, byte-identical to what was saved."""
    data = skill_store.read_skill(name)
    if data is None:
        raise HTTPException(status_code=404, detail="unknown skill")
    return {"ok": True, "skill": data}


@router.get("/{name}/runs")
async def read_runs(name: str, limit: int = 100) -> dict[str, Any]:
    """Run history rows for one skill, oldest first."""
    return {"ok": True, "runs": skill_runs.list_runs(name, limit)}


@router.post("/")
async def save_one(body: SkillSave) -> dict[str, Any]:
    """Validate and store a new skill version."""
    result = skill_store.save_skill(body.skill)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"ok": True, **result}

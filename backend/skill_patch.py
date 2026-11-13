"""Skill correction patch (issue #1256).

One call patches one thing: a single step's selector, value, or
check — then bumps the version by 1 and logs who corrected it
and why. The input skill dict is never mutated; the old version
stays intact for the caller (the store in #1257 keeps both) and
the patched skill re-validates before it is returned.

Patch shape:
  {"step": 2, "selector": "...", "by": "ari", "why": "..."}
  {"step": 2, "value": "...", "by": ..., "why": ...}
  {"step": 2, "check": {"type": ..., "expected": ...}, ...}
Exactly one of selector/value/check per call. check must be a
complete {"type", "expected"} pair — half a check is rejected.
"""

from __future__ import annotations

import copy
import time

from backend.skill_format import validate_skill

_PATCHABLE = ("selector", "value", "check")


def apply_patch(skill: dict, patch: dict) -> dict:
    """Apply one correction. Returns {"skill", "log"} on success or
    {"error"}: the input skill is never mutated and the version
    only bumps on a patch that re-validates."""
    if not isinstance(skill, dict) or not isinstance(patch, dict):
        return {"error": "skill and patch must be objects"}
    present = [f for f in _PATCHABLE if f in patch]
    if len(present) != 1:
        return {"error": "patch exactly one of selector, value, check"}
    by = patch.get("by", "")
    why = patch.get("why", "")
    if not by or not why:
        return {"error": "patch needs by and why"}
    n = patch.get("step")
    steps = skill.get("steps", []) if isinstance(skill, dict) else []
    targets = [s for s in steps if isinstance(s, dict) and s.get("n") == n]
    if not isinstance(n, int) or len(targets) != 1:
        return {"error": f"no single step n={n!r}"}
    field = present[0]
    new_skill = copy.deepcopy(skill)
    new_step = next(s for s in new_skill["steps"] if s.get("n") == n)
    old = new_step.get(field)
    new_value = patch[field]
    if field == "check":
        if not isinstance(new_value, dict) or set(new_value) != {"type", "expected"}:
            return {"error": "check needs exactly {type, expected}"}
        new_step["check"] = dict(new_value)
    else:
        if not isinstance(new_value, str) or not new_value:
            return {"error": f"{field} needs a non-empty string"}
        new_step[field] = new_value
    new_skill["version"] = skill.get("version", 0) + 1
    errors = validate_skill(new_skill)
    if errors:
        return {"error": f"patched skill invalid: {errors[0]}"}
    entry = {
        "skill": skill.get("skill", ""),
        "from_version": skill.get("version", 0),
        "to_version": new_skill["version"],
        "step": n,
        "field": field,
        "old": old,
        "new": (dict(new_value) if isinstance(new_value, dict) else new_value),
        "by": by,
        "why": why,
        "at": int(time.time()),
    }
    return {"skill": new_skill, "log": entry}

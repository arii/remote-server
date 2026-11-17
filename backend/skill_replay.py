"""Skill replayer (issue #1255). One replayer, one checker.

Runs a validated skill file step by step against a driver,
runs the deterministic check after each step, and stops on the
first failed check — returning the failing step number. No
planner/executor/verifier split, no model anywhere: acts come
from the skill, verdicts come from skill_check.

Driver protocol (structural — #1258 will add the Grid, local
Playwright, and Browser MCP drivers behind this same shape):
  snapshot() -> page dict {url, title, elements}
  navigate(url) / click(x, y) / type_text(text) / key(key)
  wheel(x, y, dx, dy) -> None
The replayer resolves selectors to coordinates through
match_selector on the pre-action snapshot; a selector that
matches nothing fails the step before anything executes.

run_skill validates the skill first with the frozen-format
validator (#1253) and refuses to run an invalid skill.
"""

from __future__ import annotations

from fastapi import HTTPException

from backend.browser_safety import (
    require_human_not_driving as _require_human_not_driving,
)
from backend.skill_check import check_step, match_selector
from backend.skill_drivers import BrowserDriver
from backend.skill_format import validate_skill
from backend.skill_trace import finish_step, page_hash_of, step_trace


def _resolve(step: dict, page: dict) -> tuple[str, dict | None, str]:
    """Resolve a step to an executable action.

    Returns (action, kwargs, error): error is empty when the step
    can run. Click/wheel need coordinates — selector first, and a
    step with neither fails closed.
    """
    action = step.get("action")
    if action == "navigate":
        url = step.get("selector") or ""
        if not url:
            return action, None, "navigate needs a URL in selector"
        return action, {"url": url}, ""
    if action in ("click", "wheel"):
        matches = match_selector(page, step.get("selector", ""))
        if not matches:
            return (
                action,
                None,
                (f"selector {step.get('selector', '')!r} matches nothing"),
            )
        el = matches[0]
        try:
            x, y = int(el.get("x", 0)), int(el.get("y", 0))
        except (TypeError, ValueError):
            return action, None, "matched target has no coordinates"
        if action == "click":
            return action, {"x": x, "y": y}, ""
        return action, {"x": x, "y": y, "dx": 0, "dy": 0}, ""
    if action == "type":
        return action, {"text": step.get("value", "")}, ""
    if action == "key":
        return action, {"key": step.get("value", "")}, ""
    return action, None, f"unknown action {action!r}"


def _perform(driver, action: str, kwargs: dict, trace: dict) -> str:
    """Execute one resolved action. Returns an error or ''."""
    try:
        _require_human_not_driving()
        trace["executed"] = True
        if action == "navigate":
            driver.navigate(kwargs["url"])
        elif action == "click":
            driver.click(kwargs["x"], kwargs["y"])
        elif action == "wheel":
            driver.wheel(kwargs["x"], kwargs["y"], kwargs["dx"], kwargs["dy"])
        elif action == "type":
            driver.type_text(kwargs["text"])
        elif action == "key":
            driver.key(kwargs["key"])
        else:
            return f"unknown action {action!r}"
    except HTTPException:
        raise
    except Exception as exc:
        return f"driver failed: {exc}"
    return ""


def run_skill(skill: dict, driver: BrowserDriver) -> dict:
    """Run a skill dict to completion or the first failure.

    Returns {skill, version, passed, failed_step, reason, steps} —
    failed_step is the 1-based step n (None on pass) and steps
    records each attempted step's own verdict. Steps after a
    failure never execute.
    """
    errors = validate_skill(skill)
    context = {"source_url": skill.get("source_url", ""), "mode": "replay"}
    if errors:
        return {
            "skill": skill.get("skill", ""),
            "version": skill.get("version", 0),
            "passed": False,
            "failed_step": None,
            "reason": f"invalid skill: {errors[0]}",
            "steps": [],
            "trace_version": 1,
            "context": context,
        }
    result = {
        "skill": skill.get("skill", ""),
        "version": skill.get("version", 0),
        "passed": True,
        "failed_step": None,
        "reason": "",
        "steps": [],
        "trace_version": 1,
        "context": context,
    }
    for step in skill.get("steps", []):
        n = step.get("n")
        trace = step_trace(n, step, context=context)
        try:
            _require_human_not_driving()
            pre_page = driver.snapshot()
            trace = step_trace(n, step, page=pre_page, context=context)
            action, kwargs, resolve_error = _resolve(step, pre_page)
            if resolve_error:
                finish_step(trace, "error", resolve_error)
            else:
                perform_error = _perform(driver, action, kwargs, trace)
                if perform_error:
                    finish_step(trace, "error", perform_error)
                else:
                    post_page = driver.snapshot()
                    trace["context"]["post_page_hash"] = page_hash_of(post_page)
                    ok, reason = check_step(post_page, step)
                    finish_step(trace, "passed" if ok else "failed", reason)
        except HTTPException as exc:
            verdict = (
                "blocked"
                if exc.status_code == 409 and not trace["executed"]
                else "error"
            )
            finish_step(trace, verdict, f"replay refused: {exc.detail}")
        except Exception as exc:
            finish_step(trace, "error", f"driver failed: {exc}")
        result["steps"].append(trace)
        if not trace["ok"]:
            result.update(passed=False, failed_step=n, reason=trace["reason"])
            break
    return result

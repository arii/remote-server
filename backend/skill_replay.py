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

from backend.skill_check import check_step, match_selector
from backend.skill_drivers import BrowserDriver
from backend.skill_format import validate_skill


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


def _perform(driver, action: str, kwargs: dict) -> str:
    """Execute one resolved action. Returns an error or ''."""
    try:
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
    if errors:
        return {
            "skill": skill.get("skill", ""),
            "version": skill.get("version", 0),
            "passed": False,
            "failed_step": None,
            "reason": f"invalid skill: {errors[0]}",
            "steps": [],
        }
    result = {
        "skill": skill.get("skill", ""),
        "version": skill.get("version", 0),
        "passed": True,
        "failed_step": None,
        "reason": "",
        "steps": [],
    }
    for step in skill.get("steps", []):
        n = step.get("n")
        pre_page = driver.snapshot()
        action, kwargs, resolve_error = _resolve(step, pre_page)
        if resolve_error:
            result["steps"].append(
                {"n": n, "action": action, "ok": False, "reason": resolve_error}
            )
            result.update(passed=False, failed_step=n, reason=resolve_error)
            break
        perform_error = _perform(driver, action, kwargs)
        if perform_error:
            result["steps"].append(
                {"n": n, "action": action, "ok": False, "reason": perform_error}
            )
            result.update(passed=False, failed_step=n, reason=perform_error)
            break
        post_page = driver.snapshot()
        ok, check_reason = check_step(post_page, step)
        result["steps"].append(
            {"n": n, "action": action, "ok": ok, "reason": check_reason}
        )
        if not ok:
            result.update(passed=False, failed_step=n, reason=check_reason)
            break
    return result

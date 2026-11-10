"""Deterministic skill-step checker (issue #1254).

Code verifies every step; the model never does. Pure functions over
a snapshot page dict — no model call, no network, no environment
reads of any kind. The replayer (#1255) calls check_step after
each action and stops on the first failure.

Check types (the only four the frozen format allows):
- url_contains: expected substring of the page URL.
- element_visible: the step selector matches a snapshot target
  (snapshot only lists visible elements).
- text_contains: expected substring of the title or any target text.
- value_equals: the matched target's value equals expected.

Matching limits (documented, not silent): the snapshot element
list is flat, so ancestry in scoped selectors is not verified —
only the subject (last) compound must match. Pseudo-classes are
ignored in matching. Class and value matching need the cls/val
fields the drive snapshot captures (#1252 fields plus #1254
additions); targets without them simply do not match .class and
fall back to text for value_equals.
"""

from __future__ import annotations

import re

from backend.skill_format import _split_compounds

_ATTR_FIELD = {
    "data-testid": "tid",
    "id": "id",
    "name": "name",
    "role": "role",
}

_COMPOUND_TAG_RE = r"[a-zA-Z][a-zA-Z0-9_-]*"


def _compound_parts(compound: str) -> tuple[str | None, list]:
    """Split one compound into (tag, hooks). Hooks are (kind, value)
    with kind in id/class/attr; pseudo-classes are dropped."""
    tag_match = re.match(_COMPOUND_TAG_RE, compound)
    tag = tag_match.group(0).upper() if tag_match else None
    rest = compound[len(tag_match.group(0)) :] if tag_match else compound
    hooks: list = []
    for hook in re.finditer(
        r"([#.])([a-zA-Z0-9_-]+)|\[([a-zA-Z-]+)(?:='([^']*)')?\]",
        rest,
    ):
        if hook.group(1) == "#":
            hooks.append(("id", hook.group(2)))
        elif hook.group(1) == ".":
            hooks.append(("class", hook.group(2)))
        else:
            hooks.append(("attr", hook.group(3), hook.group(4)))
    return tag, hooks


def _element_matches(element: dict, compound: str) -> bool:
    tag, hooks = _compound_parts(compound)
    if tag and str(element.get("tag", "")).upper() != tag:
        return False
    for hook in hooks:
        if hook[0] == "id":
            if str(element.get("id") or "") != hook[1]:
                return False
        elif hook[0] == "class":
            classes = str(element.get("cls") or "").split()
            if hook[1] not in classes:
                return False
        else:
            _, name, want = hook
            field = _ATTR_FIELD.get(name)
            if field is None:
                return False
            have = element.get(field)
            if want is None:
                if not have:
                    return False
            elif str(have) != want:
                return False
    return True


def match_selector(page: dict, selector: str) -> list:
    """Snapshot targets matching the selector's subject compound.

    Only the last compound must match (flat snapshot: ancestry is
    not verified). Returns the matching target dicts, possibly
    empty. Never raises on odd input — no match is a miss.
    """
    try:
        parts = _split_compounds(selector)
        if not parts:
            return []
        subject = parts[-1]
        elements = page.get("elements", []) or []
        return [
            el
            for el in elements
            if isinstance(el, dict) and _element_matches(el, subject)
        ]
    except Exception:
        return []


def _element_value(element: dict) -> str:
    val = element.get("val")
    if val is not None:
        return str(val)
    return str(element.get("text", ""))


def check_step(page: dict, step: dict) -> tuple[bool, str]:
    """Run one step's check against a snapshot page.

    Returns (passed, reason): reason is empty on pass and names
    the mismatch on failure. Deterministic; no model involved.
    """
    check = step.get("check") or {}
    ctype = check.get("type")
    expected = check.get("expected", "")
    if ctype == "url_contains":
        url = str(page.get("url", ""))
        if expected in url:
            return True, ""
        return False, f"url {url!r} does not contain {expected!r}"
    if ctype == "element_visible":
        selector = step.get("selector", "")
        if match_selector(page, selector):
            return True, ""
        return False, f"selector {selector!r} matches no visible target"
    if ctype == "text_contains":
        haystacks = [str(page.get("title", ""))]
        haystacks.extend(
            str(el.get("text", ""))
            for el in page.get("elements", []) or []
            if isinstance(el, dict)
        )
        if any(expected in hay for hay in haystacks):
            return True, ""
        return False, f"text {expected!r} not found on page"
    if ctype == "value_equals":
        selector = step.get("selector", "")
        matches = match_selector(page, selector)
        if not matches:
            return False, f"selector {selector!r} matches no target"
        actual = _element_value(matches[0])
        if actual == expected:
            return True, ""
        return False, f"value {actual!r} != {expected!r}"
    return False, f"unknown check type {ctype!r}"

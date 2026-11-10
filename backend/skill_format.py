"""Frozen co-browse skill file format (issue #1253).

One file per skill, versioned, human readable. The format contract
lives in two places with a documented split: skill_schema.json
(draft 2020-12, for builders and external tools) enforces the
structural rules (required fields, enums, types, minimums, and a
no-XPath pattern on selectors); the pydantic models below enforce
the same structure plus what JSON Schema cannot express cheaply
(sequential step numbers, strict ints, no extra properties, the
no-bare-text selector rule, type-steps-need-values).

Rules: data-testid or ARIA role first, then id, name. Never XPath,
never bare text — except navigate steps, whose selector carries
the destination http(s) URL instead of a CSS selector (clarified
for the replayer, issue #1255). Every step has exactly one check,
of one of four types: url_contains, element_visible,
text_contains, value_equals. The model never verifies a step;
code does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

SKILL_SCHEMA_PATH = Path(__file__).with_name("skill_schema.json")

CHECK_TYPES = (
    "url_contains",
    "element_visible",
    "text_contains",
    "value_equals",
)

ACTIONS = ("navigate", "click", "type", "key", "wheel")


class _ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillCheck(_ForbidExtra):
    type: str
    expected: str


class SkillStep(_ForbidExtra):
    n: int = Field(strict=True)
    action: str
    selector: str = ""
    value: str = ""
    check: SkillCheck


class SkillFile(_ForbidExtra):
    skill: str = Field(min_length=1)
    version: int = Field(ge=1, strict=True)
    source_url: str
    steps: list[SkillStep] = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("source_url needs an http(s) URL")
        return value


#: Bare tag names the validator accepts without any hook: a
#: bare word that is not a tag is treated as a text-only selector.
_BARE_TAGS = frozenset(
    {
        "a",
        "button",
        "input",
        "select",
        "textarea",
        "form",
        "label",
        "div",
        "span",
        "p",
        "h1",
        "h2",
        "h3",
        "ul",
        "ol",
        "li",
        "table",
        "tr",
        "td",
        "img",
    }
)

#: One compound selector: optional tag plus id/class/attribute/
#: pseudo hooks, including functional pseudos like :nth-child(2).
_COMPOUND_RE = (
    r"(?:[a-zA-Z][a-zA-Z0-9_-]*)?"
    r"(?:[#.][a-zA-Z0-9_-]+|\[[^\]]+\]|:[a-zA-Z-]+(?:\([^)]*\))?)*"
)


def _compound_ok(part: str) -> bool:
    if not re.fullmatch(_COMPOUND_RE, part):
        return False
    if not any(c in part for c in "#.["):
        tag = re.split(r"[#.\[:]", part, maxsplit=1)[0].lower()
        return tag in _BARE_TAGS
    return True


def _split_compounds(text: str) -> list[str]:
    """Split on top-level combinators (space, >, +, ~) and groups
    (,), ignoring separators inside quotes and [...] spans, so
    attribute values with spaces survive intact."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    cur: list[str] = []
    for ch in text:
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == "[":
            depth += 1
            cur.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif depth == 0 and (ch in ">,+~" or ch.isspace()):
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _selector_errors(selector: str, n: int, action: str = "") -> list[str]:
    if not selector:
        return [f"step {n}: missing selector"]
    if action == "navigate":
        # The navigate target is a URL, not a CSS selector.
        if selector.strip().startswith(("http://", "https://")):
            return []
        return [f"step {n}: navigate needs an http(s) URL in selector"]
    text = selector.strip()
    if text.lower().startswith(("//", "xpath=", "xpath:")):
        return [f"step {n}: XPath selectors are forbidden"]
    # Every compound must stand on its own, so scoped selectors
    # like "[data-testid='form'] button" pass while bare text like
    # "Submit" still fails.
    parts = _split_compounds(text)
    if not parts:
        return [f"step {n}: not a CSS selector"]
    if not all(_compound_ok(p) for p in parts):
        if not any(c in text for c in "#.["):
            return [f"step {n}: text-only selectors are forbidden"]
        return [f"step {n}: not a CSS selector"]
    return []


def validate_skill(data: dict) -> list[str]:
    """Validate parsed skill data. Returns error strings; empty means
    the skill matches the frozen format."""
    if not isinstance(data, dict):
        return ["skill must be a JSON object"]
    try:
        skill = SkillFile.model_validate(data)
    except Exception as exc:
        errors_fn = getattr(exc, "errors", None)
        if callable(errors_fn):
            return [str(err) for err in errors_fn()]
        return [str(exc)]
    errors: list[str] = []
    for want, step in enumerate(skill.steps, start=1):
        if step.n != want:
            errors.append(f"step {want}: n is {step.n}, steps must run 1..N in order")
        if step.action not in ACTIONS:
            errors.append(f"step {step.n}: unknown action {step.action!r}")
        errors.extend(_selector_errors(step.selector, step.n, step.action))
        if step.action == "type" and not step.value:
            errors.append(f"step {step.n}: type needs a value")
        if step.check.type not in CHECK_TYPES:
            errors.append(f"step {step.n}: unknown check type {step.check.type!r}")
        if not step.check.expected:
            errors.append(f"step {step.n}: check needs an expected value")
    return errors


def validate_skill_file(path: str | Path) -> list[str]:
    """Validate a skill file on disk. JSON errors count as errors."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"unreadable: {exc}"]
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"invalid JSON: {exc}"]
    return validate_skill(data)

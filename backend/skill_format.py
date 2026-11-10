"""Frozen co-browse skill file format (issue #1253).

One file per skill, versioned, human readable. The format contract
lives in two equivalent places: skill_schema.json (draft 2020-12,
for builders and external tools) and the pydantic models below
(the validator the store and replayer call). Both enforce the same
rules; the models add the two checks JSON Schema cannot express
cheaply (sequential step numbers, no-XPath selectors).

Rules: data-testid or ARIA role first, then id, name. Never XPath,
never bare text. Every step has exactly one check, of one of four
types: url_contains, element_visible, text_contains, value_equals.
The model never verifies a step; code does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

SKILL_SCHEMA_PATH = Path(__file__).with_name("skill_schema.json")

CHECK_TYPES = (
    "url_contains",
    "element_visible",
    "text_contains",
    "value_equals",
)

ACTIONS = ("navigate", "click", "type", "key", "wheel")


class SkillCheck(BaseModel):
    type: str
    expected: str


class SkillStep(BaseModel):
    n: int
    action: str
    selector: str = ""
    value: str = ""
    check: SkillCheck


class SkillFile(BaseModel):
    skill: str
    version: int = Field(ge=1)
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

_SELECTOR_RE = (
    r"^(?:[a-zA-Z][a-zA-Z0-9_-]*)?"
    r"(?:[#.][a-zA-Z0-9_-]+|\[[^\]]+\]|:[a-zA-Z-]+)*$"
)


def _selector_errors(selector: str, n: int) -> list[str]:
    if not selector:
        return [f"step {n}: missing selector"]
    text = selector.strip()
    if text.lower().startswith(("//", "xpath=", "xpath:")):
        return [f"step {n}: XPath selectors are forbidden"]
    has_tag = bool(re.match(r"[a-zA-Z]", text))
    has_hook = any(c in text for c in "#.[:")
    if not has_tag and not has_hook:
        return [f"step {n}: not a CSS selector"]
    if not re.match(_SELECTOR_RE, text):
        return [f"step {n}: not a CSS selector"]
    if not has_hook:
        tag = re.split(r"[#.\[:]", text, maxsplit=1)[0].lower()
        if tag not in _BARE_TAGS:
            return [f"step {n}: text-only selectors are forbidden"]
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
        errors.extend(_selector_errors(step.selector, step.n))
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
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]
    return validate_skill(data)

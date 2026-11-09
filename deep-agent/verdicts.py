"""Machine-readable verdict schema for agent turns (additive only)."""

import json
import re
from typing import Literal, Union

from pydantic import BaseModel, Field


class Finding(BaseModel):
    file: str = Field(min_length=1)
    line: int = Field(ge=0, strict=True)
    note: str = Field(min_length=1)


class Verdict(BaseModel):
    verdict: Literal["READY", "NEEDS-WORK", "BLOCK", "APPROVE"]
    findings: list[Finding] = []
    summary: str = Field(min_length=1, max_length=500)


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)


def parse_verdict(text: str) -> tuple[bool, Union[Verdict, str]]:
    """Parse a model reply into a Verdict; never raises."""
    try:
        stripped = text.strip()
        m = _FENCE_RE.match(stripped)
        if m:
            stripped = m.group(1).strip()
        data = json.loads(stripped)
        if not isinstance(data, dict):
            return False, "expected a JSON object"
        return True, Verdict.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def verdict_prompt_hint() -> str:
    return (
        "Reply with ONLY a JSON object, no markdown fences and no prose:\n"
        '{"verdict": "READY" | "NEEDS-WORK" | "BLOCK" | "APPROVE",\n'
        ' "findings": [{"file": "<path>", "line": <int>, "note": "<text>"}],\n'
        ' "summary": "<text>"}\n'
        "verdict must be exactly one of READY, NEEDS-WORK, BLOCK, APPROVE.\n"
        "line must be an integer (not a string); findings may be [].\n"
        "summary is required and must be 500 characters or fewer."
    )

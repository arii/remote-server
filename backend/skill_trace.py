"""Shared step representation and redaction at the browser persistence boundary."""

from __future__ import annotations

import copy
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.browser_safety import GATE_VERSION
from backend.skill_check import match_selector

REDACTED = "[redacted]"
_URL = re.compile(r"https?://[^\s<>\"']+")
_CREDENTIAL = re.compile(
    r"pass(?:word|wd)?|token|secret|api[-_]?key|auth|cookie|session|signature",
    re.IGNORECASE,
)
_LABELS = {
    "skill",
    "kind",
    "via",
    "target",
    "action",
    "type",
    "verdict",
    "mode",
    "model_version",
    "gate_version",
    "page_hash",
    "post_page_hash",
    "replay_status",
}


def page_hash_of(page: dict) -> str:
    lines = [str(page.get("url", "")), str(page.get("title", ""))]
    for el in page.get("elements", []) or []:
        lines.append(
            "|".join(str(el.get(k, "")) for k in ("tag", "tid", "text", "x", "y"))
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


def sensitive_element(element: dict) -> bool:
    return bool(
        element.get("sensitive")
        or element.get("val") == REDACTED
        or element.get("text") == REDACTED
        or element.get("kind") in ("password", "username")
        or element.get("type") in ("password", "username")
    )


def step_trace(
    n: int,
    proposal: dict,
    *,
    page: dict | None = None,
    mode: str = "replay",
    context: dict | None = None,
) -> dict:
    page = page or {}
    context = dict(context or {})
    context["url"] = page.get("url", context.get("source_url", ""))
    elements = page.get("elements", [])
    selector = proposal.get("selector") or ""
    focused = [el for el in elements if el.get("focused")]
    selected = []
    if proposal.get("action") != "navigate":
        selected = match_selector(page, selector)
    action = proposal["action"]
    input_action = action in ("type", "key")
    targets = selected + focused if input_action else selected
    declared_sensitive = bool(
        re.search(
            r"password|passwd|username|one-time-code|webauthn",
            selector,
            re.IGNORECASE,
        )
    )
    observed = targets
    unknown = not observed or any(
        "sensitive" not in el and not el.get("type") for el in observed
    )
    reads_value = proposal.get("check", {}).get("type") in (
        "value_equals",
        "text_contains",
    )
    sensitive = action != "navigate" and (
        declared_sensitive
        or any(sensitive_element(el) for el in targets)
        or (input_action or reads_value)
        and unknown
    )
    return {
        "n": n,
        "action": proposal["action"],
        "input": {"value": proposal.get("value", "")},
        "proposal": copy.deepcopy(proposal),
        "gate": {"verdict": "allowed", "reason": ""},
        "executed": False,
        "outcome": {"verdict": "error", "reason": ""},
        "page_hash": page_hash_of(page) if page else None,
        "model_version": "deterministic",
        "gate_version": GATE_VERSION,
        "mode": mode,
        "context": context,
        "sensitive": sensitive,
        "_secrets": [el.get("val") for el in targets if sensitive_element(el)],
    }


def finish_step(step: dict, verdict: str, reason: str = "") -> None:
    step.update(ok=verdict == "passed", reason=reason)
    step["outcome"] = {"verdict": verdict, "reason": reason}
    if verdict == "blocked":
        step["gate"] = {"verdict": "blocked", "reason": reason}


def redact_persistence(record: dict) -> dict:
    """Scrub the entire row, including errors that echo a protected input."""
    record = copy.deepcopy(record)
    if record.get("_validation_error"):
        record["reason"] = "invalid skill: validation failed"
    for step in record.get("steps", []):
        if step.get("sensitive") and step["outcome"]["verdict"] in ("error", "failed"):
            step["reason"] = REDACTED
            step["outcome"]["reason"] = REDACTED
            if record.get("failed_step") == step["n"]:
                record["reason"] = REDACTED
    secrets: set[str] = set()

    def collect(value):
        if isinstance(value, dict):
            secrets.update(
                v
                for v in value.get("_secrets", [])
                if isinstance(v, str) and v and v != REDACTED
            )
            if value.get("sensitive"):
                check = value.get("proposal", {}).get("check", {})
                if check.get("type") in ("value_equals", "text_contains"):
                    candidate = check.get("expected")
                    if (
                        isinstance(candidate, str)
                        and candidate
                        and candidate != REDACTED
                    ):
                        secrets.add(candidate)
                typed_parts = (
                    (value.get("input", {}), value.get("proposal", {}))
                    if value.get("action") in ("type", "key")
                    else ()
                )
                for part in typed_parts:
                    for key in ("value", "text", "key"):
                        candidate = part.get(key)
                        if (
                            isinstance(candidate, str)
                            and candidate
                            and candidate != REDACTED
                        ):
                            secrets.add(candidate)
            for part in value.values():
                collect(part)
        elif isinstance(value, list):
            for part in value:
                collect(part)
        elif isinstance(value, str):
            for match in _URL.finditer(value):
                try:
                    url = urlsplit(match.group())
                    secrets.update(v for v in (url.username, url.password) if v)
                    secrets.update(
                        v
                        for k, v in parse_qsl(url.query)
                        if v and _CREDENTIAL.search(k)
                    )
                except ValueError:
                    continue

    collect(record)
    secret_pattern = (
        re.compile(
            "|".join(
                re.escape(secret) for secret in sorted(secrets, key=len, reverse=True)
            )
        )
        if secrets
        else None
    )

    def clean_url(match):
        try:
            url = urlsplit(match.group())
            query = parse_qsl(url.query, keep_blank_values=True)
            if url.username is None and not any(
                _CREDENTIAL.search(k) for k, _ in query
            ):
                return match.group()
            return urlunsplit(
                (
                    url.scheme,
                    url.netloc.rsplit("@", 1)[-1],
                    url.path,
                    urlencode(
                        [
                            (k, REDACTED if _CREDENTIAL.search(k) else v)
                            for k, v in query
                        ]
                    ),
                    url.fragment,
                )
            )
        except ValueError:
            return REDACTED

    def scrub(value, key=""):
        if isinstance(value, dict):
            return {k: scrub(v, k) for k, v in value.items() if not k.startswith("_")}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str) and key not in _LABELS:
            if value == REDACTED:
                return value
            value = _URL.sub(clean_url, value)
            if secret_pattern is not None:
                value = secret_pattern.sub(lambda match: REDACTED, value)
        return value

    cleaned = scrub(record)
    for raw, step in zip(record.get("steps", []), cleaned.get("steps", [])):
        if step.get("sensitive"):
            if step["action"] in ("type", "key"):
                step["input"]["value"] = REDACTED
                if "value" in step["proposal"]:
                    step["proposal"]["value"] = REDACTED
            check = step["proposal"].get("check", {})
            if check.get("type") in ("value_equals", "text_contains") and check.get(
                "expected"
            ):
                check["expected"] = REDACTED
        if step["input"] != raw["input"] or step["proposal"] != raw["proposal"]:
            step["redacted"] = True
    return cleaned

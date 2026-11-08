"""Environment and config helpers (split from the backend.core god file)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# Briefing fixture override (#453): tests and preview environments
# serve canned briefing JSON instead of shelling out. The explicit
# path env var always wins; a fixture-briefing.json sitting next to
# the checkouts is only honored with the auto-discovery opt-in — a
# stray file must never silently shadow live data.
FIXTURE_BRIEFING_ENV = "BUTTONS_FIXTURE_BRIEFING"
FIXTURE_AUTO_DISCOVER_ENV = "BUTTONS_FIXTURE_AUTO_DISCOVER"
FIXTURE_FILENAME = "fixture-briefing.json"
_FIXTURE_TRUTHY = frozenset({"1", "true", "yes", "on"})


def fixture_auto_discover_enabled() -> bool:
    """Whether a stray next-to-checkouts fixture may shadow live."""
    return os.environ.get(
        FIXTURE_AUTO_DISCOVER_ENV, "").strip().lower() in _FIXTURE_TRUTHY


def briefing_fixture_candidate() -> str:
    """Implicit fixture path next to the repo checkouts."""
    return os.path.join(
        os.environ.get("BUTTONS_REPOS_DIR", "/home/ari/repos"),
        FIXTURE_FILENAME)


def resolve_briefing_fixture_path() -> str:
    """Configured fixture path, or "" when live should serve.

    The explicit env var always applies (callers check existence so
    a missing file falls through to live with a warning); the
    implicit next-to-checkouts candidate only applies with the
    auto-discovery opt-in.
    """
    explicit = os.environ.get(FIXTURE_BRIEFING_ENV, "").strip()
    if explicit:
        return explicit
    if fixture_auto_discover_enabled():
        candidate = briefing_fixture_candidate()
        if os.path.exists(candidate):
            return candidate
    return ""


def to_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def load_config(prefix: str = "BACKEND_") -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(prefix)}


def run_td(args: list[str], cwd: Path) -> dict:
    """Invoke ``td <args> --json`` in cwd and return the parsed payload.

    Single deterministic gateway for backend/deep-agent callers that
    need boomtick's td CLI (validate-issue, verify-scope, audit-code,
    rag query, worktree create/remove, ...). No fallback shims: a
    missing td, non-JSON output, nonzero exit, or td-reported error
    raises RuntimeError for the caller to surface as a failed gate.
    """
    import subprocess

    argv = ["td", *args]
    if "--json" not in argv and "--no-json" not in argv:
        argv.insert(1, "--json")
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=300)
    except FileNotFoundError as exc:
        raise RuntimeError("td CLI not found on PATH") from exc
    out = (proc.stdout or "").strip()
    try:
        payload = json.loads(out) if out else {}
    except ValueError as exc:
        raise RuntimeError(
            f"td returned non-JSON output: {out[:300]}") from exc
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise RuntimeError(
            f"td error: {payload.get('message', 'unknown')!s:.300}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"td exited {proc.returncode}: {(proc.stderr or out)[:300]}")
    if isinstance(payload, dict):
        return payload
    return {"result": payload}

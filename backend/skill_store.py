"""Skill store (issue #1257): versioned skill files on disk.

One file per skill version: <skills_dir>/<name>.v<version>.json.
Reads always serve the latest version; saves validate with the
frozen-format validator (#1253) and refuse unknown or downgrade
versions, so history only moves forward. The correction patch
(#1256) produces new versions; this store keeps every one.

Directory resolves from BROWSER_SKILLS_DIR (default
/var/lib/buttons/skills), call-time like BROWSER_NOTES_FILE, so
tests redirect it. Skill names are restricted to letters,
digits, dash, and underscore — anything else cannot become a
filename and is rejected.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from backend.skill_format import validate_skill

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def skills_dir() -> Path:
    """Resolve the store directory at call time so tests redirect it."""
    return Path(os.environ.get("BROWSER_SKILLS_DIR", "/var/lib/buttons/skills"))


def _check_name(name: str) -> str | None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return f"invalid skill name {name!r}"
    return None


def _versions(name: str) -> list[int]:
    found = []
    try:
        for child in skills_dir().iterdir():
            if child.suffix != ".json":
                continue
            stem = child.stem
            if not stem.startswith(name + ".v"):
                continue
            try:
                found.append(int(stem[len(name) + 2 :]))
            except ValueError:
                continue
    except OSError:
        pass
    return sorted(found)


def list_skills() -> list:
    """One entry per skill name: name + latest version."""
    try:
        files = [c for c in skills_dir().iterdir() if c.suffix == ".json"]
    except OSError:
        return []
    names: dict[str, int] = {}
    for child in files:
        stem = child.stem
        if ".v" not in stem:
            continue
        name, _, ver = stem.rpartition(".v")
        try:
            ver_n = int(ver)
        except ValueError:
            continue
        if not _NAME_RE.match(name):
            continue
        names[name] = max(names.get(name, 0), ver_n)
    return [{"skill": name, "version": ver} for name, ver in sorted(names.items())]


def read_skill(name: str) -> dict | None:
    """Latest version of a skill, or None when unknown."""
    if _check_name(name):
        return None
    versions = _versions(name)
    if not versions:
        return None
    try:
        text = (skills_dir() / f"{name}.v{versions[-1]}.json").read_text(
            encoding="utf-8"
        )
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_skill(data: dict) -> dict:
    """Validate and store a new version. Returns {"skill", "version"}
    or {"error"}. Saving then loading returns the identical skill."""
    if not isinstance(data, dict):
        return {"error": "skill must be a JSON object"}
    errors = validate_skill(data)
    if errors:
        return {"error": f"invalid skill: {errors[0]}"}
    name = data["skill"]
    if _check_name(name):
        return {"error": f"invalid skill name {name!r}"}
    version = data["version"]
    existing = _versions(name)
    if existing and version <= existing[-1]:
        return {"error": f"version {version} not newer than {existing[-1]}"}
    try:
        directory = skills_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.v{version}.json"
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        return {"error": f"unwritable: {exc}"}
    return {"skill": name, "version": version}

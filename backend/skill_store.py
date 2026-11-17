"""Skill store (issue #1257): versioned skill files on disk.

One file per skill version: <skills_dir>/<name>.v<version>.json.
Reads always serve the latest version; saves validate with the
frozen-format validator (#1253) and refuse unknown or downgrade
versions, so history only moves forward. The correction patch
(#1256) produces new versions; this store keeps every one.

Directory resolves from BROWSER_SKILLS_DIR in the configured browser data
directory. Skill names are restricted to letters, digits, dash, and
underscore. Saved bytes are pinned in skills-lock.json.
"""

from __future__ import annotations

import json
import os
import re
import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from backend.browser_config import data_path
from backend.skill_format import validate_skill

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def skills_dir() -> Path:
    """Resolve the store directory at call time so tests redirect it."""
    return data_path("BROWSER_SKILLS_DIR", "skills")


@contextmanager
def _locked_store(directory: Path, *, shared: bool = False):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _load_pins(directory: Path) -> dict:
    try:
        lock = json.loads((directory / "skills-lock.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    pins = lock.get("skills") if isinstance(lock, dict) else None
    if not isinstance(pins, dict):
        raise ValueError("browser skills lock malformed: skills must be a mapping")
    for filename, entry in pins.items():
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+\.v[1-9][0-9]*\.json", filename)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("computedHash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["computedHash"])
        ):
            raise ValueError("browser skills lock contains an invalid pin")
    return pins


def _write_pins(directory: Path, pins: dict) -> None:
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, delete=False
    ) as temporary:
        path = Path(temporary.name)
        try:
            temporary.write(
                json.dumps({"skills": pins}, indent=2, sort_keys=True) + "\n"
            )
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(path, directory / "skills-lock.json")
        finally:
            path.unlink(missing_ok=True)


def verify_skills() -> list[str]:
    directory = skills_dir()
    problems = []
    try:
        if not directory.exists():
            return []
        with _locked_store(directory, shared=True):
            pins = _load_pins(directory)
            files = {
                path.name
                for path in directory.iterdir()
                if path.suffix == ".json" and path.name != "skills-lock.json"
            }
            for filename, entry in sorted(pins.items()):
                try:
                    actual = hashlib.sha256(
                        (directory / filename).read_bytes()
                    ).hexdigest()
                except OSError as exc:
                    problems.append(
                        f"browser skill {filename}: cannot read pinned file: {exc}"
                    )
                    continue
                if actual != entry["computedHash"]:
                    problems.append(f"browser skill {filename}: hash mismatch")
            for filename in sorted(files - pins.keys()):
                problems.append(f"browser skill {filename}: file is not pinned")
    except (OSError, ValueError) as exc:
        problems.append(f"browser skills verification failed: {exc}")
    return problems


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
    try:
        directory = skills_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with _locked_store(directory):
            existing = _versions(name)
            if existing and version <= existing[-1]:
                return {"error": f"version {version} not newer than {existing[-1]}"}
            pins = _load_pins(directory)
            path = directory / f"{name}.v{version}.json"
            payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            created = False
            try:
                with path.open("xb") as output:
                    created = True
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                pins[path.name] = {"computedHash": hashlib.sha256(payload).hexdigest()}
                _write_pins(directory, pins)
            except OSError:
                if created:
                    path.unlink()
                raise
    except (OSError, ValueError) as exc:
        return {"error": f"cannot save skill: {exc}"}
    return {"skill": name, "version": version}

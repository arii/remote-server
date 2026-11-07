"""First-party skill pin verification (issue #1120).

`deep-agent/skills/*.md` load into live agent turns via
`skills_registry.py` with no hash check; `skills-lock.json` plus
`verify-skills.sh` cover only vendored third-party skills. This
module pins the first-party skills against the adjacent
`skills-lock.json` (same mechanism: name -> sha256 of the file) and
is called at backend startup from `backend/server.py`. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SKILLS_DIR = HERE / "skills"
DEFAULT_LOCK_PATH = HERE / "skills-lock.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(lock_path: Path | str = DEFAULT_LOCK_PATH) -> dict:
    """Read the pin file; raises FileNotFoundError when absent."""
    with open(lock_path, encoding="utf-8") as f:
        return json.load(f)


def verify_skills(
    skills_dir: Path | str = DEFAULT_SKILLS_DIR,
    lock_path: Path | str = DEFAULT_LOCK_PATH,
) -> list:
    """Compare the skills tree against the pin file.

    Returns a list of human-readable problem lines; empty means
    clean. Missing lock file counts as a problem (fail closed on
    configuration, warn path is the caller's choice). Extra `.md`
    files fail: an unpinned skill must never load silently.
    """
    skills_dir = Path(skills_dir)
    lock_path = Path(lock_path)
    problems: list = []
    try:
        lock = load_lock(lock_path)
    except FileNotFoundError:
        return [f"skills lock missing: {lock_path}"]
    except (json.JSONDecodeError, OSError) as exc:
        return [f"skills lock unreadable: {lock_path} ({exc})"]
    pinned = lock.get("skills", {}) if isinstance(lock, dict) else {}
    if not isinstance(pinned, dict):
        return [f"skills lock malformed: {lock_path} ('skills' is not a mapping)"]

    on_disk = {p.name: p for p in sorted(skills_dir.glob("*.md"))} if skills_dir.is_dir() else {}
    if not skills_dir.is_dir():
        problems.append(f"skills dir missing: {skills_dir}")

    for name, entry in sorted(pinned.items()):
        if not isinstance(entry, dict) or "computedHash" not in entry:
            problems.append(f"skill '{name}': lock entry malformed (want computedHash)")
            continue
        rel = entry.get("skillPath", f"skills/{name}.md")
        # skillPath is repo-relative ("skills/<name>.md"); resolve
        # against the skills dir's parent so custom dirs keep working.
        candidate = (skills_dir.parent / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        if candidate.is_file():
            target = candidate
        else:
            target = skills_dir / f"{name}.md"
        if not target.is_file():
            problems.append(f"skill '{name}': locked but file missing: {target}")
            continue
        actual = _sha256_file(target)
        if actual != entry["computedHash"]:
            problems.append(
                f"skill '{name}': hash mismatch: lock {entry['computedHash'][:12]}… "
                f"!= file {actual[:12]}… ({target})"
            )

    locked_files = set()
    for name, entry in pinned.items():
        if isinstance(entry, dict) and isinstance(entry.get("skillPath"), str):
            locked_files.add(Path(entry["skillPath"]).name)
        else:
            locked_files.add(f"{name}.md")
    for filename in sorted(on_disk):
        if filename not in locked_files:
            problems.append(f"skill file '{filename}' is not pinned in the lock")
    return problems


def refresh_lock(
    skills_dir: Path | str = DEFAULT_SKILLS_DIR,
    lock_path: Path | str = DEFAULT_LOCK_PATH,
) -> dict:
    """Re-pin every `*.md` in the skills dir (the bump procedure).

    Rewrites computedHash values in place, preserving entry order;
    new files are appended. Review the diff, then commit the lock —
    same discipline as `verify-skills.sh --repin`.
    """
    skills_dir = Path(skills_dir)
    lock_path = Path(lock_path)
    lock = load_lock(lock_path)
    skills = lock.setdefault("skills", {})
    for md_path in sorted(skills_dir.glob("*.md")):
        entry = skills.get(md_path.stem)
        if not isinstance(entry, dict):
            entry = {
                "source": "first-party",
                "sourceType": "repo",
                "skillPath": f"skills/{md_path.name}",
            }
            skills[md_path.stem] = entry
        entry["computedHash"] = _sha256_file(md_path)
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=2, sort_keys=True)
        f.write("\n")
    return lock


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "--repin":
        refresh_lock(lock_path=sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "--repin":
        refresh_lock()
    else:
        found = verify_skills()
        if found:
            print("first-party skills verification FAILED:")
            for line in found:
                print(f"  - {line}")
            sys.exit(1)
        print("first-party skills verification OK")

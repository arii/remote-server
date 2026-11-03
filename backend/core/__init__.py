"""Shared routines for the backend package.

Factored from existing conventions in buttons/server.py and scripts/:
- env/config loading via os.environ.get with defaults
- repo path resolution under a repos root
- structured JSON output via json.dumps
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def get_repos_dir() -> Path:
    """Repos root: env override, host path, then container bind path.

    The terminal/dev container bind-mounts the host repos dir at
    /srv/repos without a /home/ari/repos alias; fall through so
    discovery works in both containers.
    """
    configured = Path(os.environ.get("BUTTONS_REPOS_DIR", "/home/ari/repos"))
    if configured.is_dir():
        return configured
    sibling = Path("/srv/repos")
    if sibling.is_dir():
        return sibling
    return configured


def resolve_repo_path(repo: str) -> Path:
    name = repo.strip().removeprefix("arii/").replace("..", "").strip("/")
    return get_repos_dir() / name


def to_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def load_config(prefix: str = "BACKEND_") -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(prefix)}


def get_system_health() -> dict[str, Any]:
    """Read-only box health: disk, memory, container states.

    Port of the logic behind buttons/server.py _get_system_health:
    shutil disk usage, /proc/meminfo parsing, and
    `docker compose ps --format json` (read-only, degrades to [])."""
    import shutil
    import subprocess

    path = "/home/ari" if os.path.exists("/home/ari") else "/"
    try:
        du = shutil.disk_usage(path)
        total_gb = du.total / (1024**3)
        used_gb = du.used / (1024**3)
        free_gb = du.free / (1024**3)
        disk_pct = (du.used / du.total * 100) if du.total else 0
        disk_info: dict[str, Any] = {
            "total_gb": round(total_gb, 1),
            "used_gb": round(used_gb, 1),
            "free_gb": round(free_gb, 1),
            "percent": round(disk_pct, 1),
            "summary": f"{used_gb:.1f} / {total_gb:.1f} GB ({disk_pct:.0f}%)",
        }
    except Exception as exc:
        disk_info = {"error": str(exc)}

    try:
        mem: dict[str, int] = {}
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem[parts[0].strip()] = int(parts[1].split()[0])
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_kb = total_kb - avail_kb if total_kb >= avail_kb else 0
        total_g = total_kb / (1024**2)
        used_g = used_kb / (1024**2)
        avail_g = avail_kb / (1024**2)
        mem_pct = (used_kb / total_kb * 100) if total_kb else 0
        mem_info: dict[str, Any] = {
            "total_gb": round(total_g, 1),
            "used_gb": round(used_g, 1),
            "avail_gb": round(avail_g, 1),
            "percent": round(mem_pct, 1),
            "summary": f"{used_g:.1f} / {total_g:.1f} GB ({mem_pct:.0f}%)",
        }
    except Exception as exc:
        mem_info = {"error": str(exc)}

    containers: list[dict[str, Any]] = []
    try:
        stack = os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server")
        compose = ["docker", "compose", "-f", f"{stack}/docker-compose.yml"]
        env = {**os.environ, "HOME": "/home/ari"}
        proc = subprocess.run(
            compose + ["ps", "--format", "json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, text=True, env=env,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = proc.stdout.strip()
            items: list[Any] = []
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = parsed
                elif isinstance(parsed, dict):
                    items = [parsed]
            except Exception:
                for line in raw.splitlines():
                    if line.strip():
                        try:
                            items.append(json.loads(line.strip()))
                        except Exception:
                            pass
            for item in items:
                if not isinstance(item, dict):
                    continue
                service = item.get("Service") or item.get("Name") or "unknown"
                containers.append({
                    "name": item.get("Name", service),
                    "service": service,
                    "state": item.get("State") or "unknown",
                    "status": item.get("Status") or "",
                    "health": item.get("Health") or "",
                })
    except Exception:
        pass

    return {"disk": disk_info, "memory": mem_info, "containers": containers}


REPO_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


def validate_repo_name(repo: str) -> str:
    """Basename of a repo checkout; raises ValueError when invalid."""
    name = repo.strip().removeprefix("arii/").strip("/")
    if not name or "/" in name or not REPO_NAME_RE.match(name):
        raise ValueError(f"invalid repo name: {repo!r}")
    return name


def require_confirm(body, intent: str) -> None:
    """Confirm-tap guard for destructive routes.

    Raises HTTPException(400) unless the body carries
    {"intent": <intent>, "confirmed": True}. Accepts a dict or a
    pydantic model (or any object with intent/confirmed attrs).
    """
    if hasattr(body, "model_dump"):
        data = body.model_dump()
    elif isinstance(body, dict):
        data = body
    else:
        data = {
            "intent": getattr(body, "intent", None),
            "confirmed": getattr(body, "confirmed", None),
        }
    if not isinstance(data, dict) or data.get("intent") != intent \
            or data.get("confirmed") is not True:
        raise HTTPException(status_code=400, detail="confirmation required")


def _parse_gh_remote(url: str | None) -> str | None:
    """owner/repo from a git remote URL, or None (port of buttons/server.py)."""
    import re

    if not url:
        return None
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    m = re.match(r"^(?:git@github\.com:|https://github\.com/|"
                 r"ssh://git@github\.com/)(.+)$", url)
    if not m or not REPO_NAME_RE.match(m.group(1).split("/")[0]) \
            or "/" not in m.group(1):
        return None
    owner, _, repo_part = m.group(1).partition("/")
    if not owner or not repo_part or "/" in repo_part \
            or not REPO_NAME_RE.match(repo_part):
        return None
    return f"{owner}/{repo_part}"


def _git_run(run, cmd: list[str], path: Path, timeout: int = 30):
    import subprocess

    run = run or subprocess.run
    return run(
        ["git", "-c", f"safe.directory={path}", "-C", str(path)] + cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, text=True,
    )


def list_repos(repos_dir: Path | str | None = None,
               stack_dir: Path | str | None = None,
               run=None) -> list[dict[str, Any]]:
    """Every discovered checkout as {name, full, branch, clean}.

    Port of buttons/server.py _discover_repos, minus the td/gh
    runtime fields: the special stack checkout (remote-server)
    plus every repos-dir entry with a .git. Entries that fail
    git probing are skipped, never mapped to another repo; a
    checkout without a parseable GitHub origin keeps the
    default full name ``arii/<name>``. ``repos_dir``/``stack_dir``/
    ``run`` are injectable seams for tests.
    """
    import subprocess

    run = run or subprocess.run
    root = Path(repos_dir) if repos_dir is not None else get_repos_dir()
    stack = Path(stack_dir) if stack_dir is not None else Path(
        os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server"))

    candidates: list[tuple[str, Path]] = []
    stack_git = stack / ".git"
    if stack.is_dir() and (stack_git.is_dir() or stack_git.is_file()):
        candidates.append(("remote-server", stack))
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        entries = []
    for name in entries:
        if name == "remote-server" or not REPO_NAME_RE.match(name):
            continue
        path = root / name
        git_path = path / ".git"
        if not (git_path.is_dir() or git_path.is_file()):
            continue
        candidates.append((name, path))

    repos: list[dict[str, Any]] = []
    for name, path in candidates:
        try:
            origin = run(
                ["git", "-c", f"safe.directory={path}", "-C", str(path),
                 "config", "--get", "remote.origin.url"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True,
            )
            full = _parse_gh_remote(origin.stdout.strip()) \
                if origin.returncode == 0 else None
            branch_proc = _git_run(
                run, ["rev-parse", "--abbrev-ref", "HEAD"], path)
            branch = branch_proc.stdout.strip() \
                if branch_proc.returncode == 0 else ""
            status_proc = _git_run(
                run, ["status", "--short", "--branch"], path)
            if status_proc.returncode != 0:
                continue
            lines = [ln for ln in status_proc.stdout.splitlines()
                     if ln and not ln.startswith("##")]
            repos.append({"name": name, "full": full or f"arii/{name}",
                          "branch": branch, "clean": len(lines) == 0})
        except Exception:
            continue
    return sorted(repos, key=lambda r: r["name"])


def get_repo_status(repo: str) -> dict[str, Any]:
    """Read-only git status for one repo checkout.

    Returns {"repo", "branch", "short", "clean"}. Raises
    ValueError on a bad name, FileNotFoundError when the checkout
    (or its .git) is absent."""
    import subprocess

    name = validate_repo_name(repo)
    path = resolve_repo_path(name)
    if name == "remote-server" and not path.exists():
        stack = Path(os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server"))
        if stack.is_dir():
            path = stack
    git_dir = path / ".git"
    if not (git_dir.is_dir() or git_dir.is_file()):
        raise FileNotFoundError(f"unknown repo checkout: {name!r}")
    branch_proc = subprocess.run(
        ["git", "-c", f"safe.directory={path}", "-C", str(path),
         "rev-parse", "--abbrev-ref", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, text=True,
    )
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else ""
    status_proc = subprocess.run(
        ["git", "-c", f"safe.directory={path}", "-C", str(path),
         "status", "--short", "--branch"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, text=True,
    )
    if status_proc.returncode != 0:
        raise RuntimeError(
            (status_proc.stderr or "").strip().splitlines()[-1]
            if (status_proc.stderr or "").strip() else "git status failed")
    short = status_proc.stdout
    lines = [ln for ln in short.splitlines()
             if ln and not ln.startswith("##")]
    return {"repo": name, "branch": branch, "short": short,
            "clean": len(lines) == 0}

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
    `docker compose ps --format json` (read-only). A docker
    failure surfaces as ``containers_error`` (#530); only a
    successful empty listing reads as zero containers."""
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

    containers, containers_error = _compose_containers_result()

    return {"disk": disk_info, "memory": mem_info,
            "containers": containers,
            "containers_error": containers_error}


def _compose_containers_result(run=None) -> tuple[list[dict[str, Any]], str | None]:
    """Stack containers from `docker compose ps` (read-only).

    Returns (containers, error): ``error`` names the failure
    class (spawn/timeout/exit code/parse) so callers can tell
    "nothing running" from "couldn't ask docker" (#530).
    ``run`` injects tests (defaults to subprocess.run).
    """
    import subprocess

    run = run or subprocess.run
    stack = os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server")
    compose = ["docker", "compose", "-f", f"{stack}/docker-compose.yml"]
    # Inherit env: the compose plugin lives under the runtime
    # user's ~/.docker, and a HOME override blinds plugin
    # discovery (surfaces as an exit-code error below).
    try:
        proc = run(
            compose + ["ps", "--format", "json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, text=True,
        )
    except subprocess.TimeoutExpired:
        return [], "docker compose ps timed out after 10s"
    except Exception as exc:
        return [], f"docker compose ps failed: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        tail = err.splitlines()[-1] if err else "unknown error"
        return [], f"docker compose ps exited {proc.returncode}: {tail}"
    raw = (proc.stdout or "").strip()
    if not raw:
        return [], None
    items: list[Any] = []
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if parsed is None:
        for line in raw.splitlines():
            if line.strip():
                try:
                    items.append(json.loads(line.strip()))
                except Exception:
                    pass
        if not any(isinstance(item, dict) for item in items):
            return [], "docker compose ps output unparseable: " \
                f"{raw[:120]}"
    elif isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        items = [parsed]
    else:
        return [], "docker compose ps output unparseable: " \
            f"unexpected JSON {type(parsed).__name__}"
    containers: list[dict[str, Any]] = []
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
    return containers, None


def _compose_containers(run=None) -> list[dict[str, Any]]:
    """Stack containers from `docker compose ps` (read-only).

    Stats-path wrapper that keeps the degrade-to-[] contract;
    system health uses _compose_containers_result for the error.
    ``run`` injects tests (defaults to subprocess.run).
    """
    containers, _ = _compose_containers_result(run=run)
    return containers


def get_container_stats(run=None) -> dict[str, Any]:
    """Per-container state + live CPU/RAM for the drawer (#663).

    Joins compose membership/state with one-shot
    `docker stats --no-stream` (never a streaming daemon).
    Containers without a stats row (stopped) carry null usage;
    a stats failure keeps the state rows; compose failure
    degrades to []. ``run`` injects tests.
    """
    import subprocess

    run = run or subprocess.run
    containers = _compose_containers(run=run)
    usage: dict[str, dict[str, Any]] = {}
    try:
        proc = run(
            ["docker", "stats", "--no-stream", "--format",
             "{{json .}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, text=True,
        )
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line.strip())
                except Exception:
                    continue
                if not isinstance(item, dict) or "Name" not in item:
                    continue
                usage[item["Name"]] = {
                    "cpu_pct": _parse_cpu_pct(item.get("CPUPerc")),
                    "mem_used": _parse_mem_half(item.get("MemUsage"), 0),
                    "mem_limit": _parse_mem_half(item.get("MemUsage"), 1),
                }
    except Exception:
        pass
    rows = [{
        "name": c["name"], "service": c["service"],
        "state": c["state"],
        "cpu_pct": usage.get(c["name"], {}).get("cpu_pct"),
        "mem_used": usage.get(c["name"], {}).get("mem_used"),
        "mem_limit": usage.get(c["name"], {}).get("mem_limit"),
    } for c in containers]
    return {"containers": rows}


def _parse_cpu_pct(raw: Any) -> float | None:
    try:
        return float(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _parse_mem_half(raw: Any, half: int) -> str | None:
    if not isinstance(raw, str) or "/" not in raw:
        return None
    parts = [p.strip() for p in raw.split("/", 1)]
    return parts[half] if len(parts) == 2 and parts[half] else None


REPO_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")

BRANCH_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def validate_repo_name(repo: str) -> str:
    """Basename of a repo checkout; raises ValueError when invalid."""
    name = repo.strip().removeprefix("arii/").strip("/")
    if not name or "/" in name or not REPO_NAME_RE.match(name):
        raise ValueError(f"invalid repo name: {repo!r}")
    return name


def validate_action_branch(repo: str, branch, run=None) -> str:
    """Optional action branch (#708): "" when omitted or blank (the
    endpoint behaves exactly as today), otherwise the stripped name
    after an existence check against the repo checkout (git
    rev-parse refs/heads/<name>). Raises ValueError on a malformed
    name, an unknown repo, or a branch the checkout lacks. ``run``
    is the injectable subprocess.run seam for tests."""
    if branch is None:
        return ""
    if not isinstance(branch, str):
        raise ValueError(f"invalid branch name: {branch!r}")
    name = branch.strip()
    if not name:
        return ""
    if (not BRANCH_NAME_RE.match(name) or ".." in name
            or "//" in name or name.endswith((".lock", "/", "."))):
        raise ValueError(f"invalid branch name: {branch!r}")
    try:
        path = _checkout_path(validate_repo_name(repo))
    except FileNotFoundError:
        raise ValueError(f"unknown branch: {name!r}")
    proc = _git_run(run, ["rev-parse", "--verify", "--quiet",
                          f"refs/heads/{name}"], path)
    if proc.returncode != 0:
        raise ValueError(f"unknown branch: {name!r}")
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


def _checkout_path(name: str) -> Path:
    """Validated checkout dir; remote-server falls back to the stack
    dir. Raises FileNotFoundError when the checkout (or its .git)
    is absent."""
    path = resolve_repo_path(name)
    if name == "remote-server" and not path.exists():
        stack = Path(os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server"))
        if stack.is_dir():
            path = stack
    git_dir = path / ".git"
    if not (git_dir.is_dir() or git_dir.is_file()):
        raise FileNotFoundError(f"unknown repo checkout: {name!r}")
    return path


def get_checkout_path(repo: str) -> Path:
    """Validated checkout dir for repo-relative reads (#653).

    Same name validation and stack fallback as the status/diff
    paths; routers use it for @-mention resolution and file
    listing. Raises ValueError on a bad name, FileNotFoundError
    when the checkout (or its .git) is absent.
    """
    return _checkout_path(validate_repo_name(repo))


# @-mention file listing (#653): cap keeps the composer dropdown
# light; skip-dirs keep caches and machine state out of it.
REPO_FILES_CAP = 200
REPO_FILES_SKIP_DIRS = frozenset(
    {"node_modules", "__pycache__", "ledger", "traces"})


def list_repo_files(repo: str, prefix: str = "",
                    limit: int = REPO_FILES_CAP) -> dict[str, Any]:
    """Repo-relative file paths for @-mention completion (#653).

    Sorted os.walk over the validated checkout, skipping dotfiles/
    dot-dirs plus REPO_FILES_SKIP_DIRS, files only. ``prefix`` (a
    leading "/" is stripped) filters by startswith; a prefix
    containing ".." matches nothing (boundary rejection). Collects
    one past ``limit`` so ``truncated`` is exact. Returns {"repo",
    "prefix", "files", "truncated"}. Same error contract as
    get_repo_status.
    """
    name = validate_repo_name(repo)
    root = _checkout_path(name)
    want = (prefix or "").lstrip("/").strip()
    if ".." in want.split("/"):
        return {"repo": name, "prefix": want, "files": [],
                "truncated": False}
    files: list[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in REPO_FILES_SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, filename), root)
            if want and not rel.startswith(want):
                continue
            if not os.path.isfile(os.path.join(dirpath, filename)):
                continue
            if len(files) > limit:
                truncated = True
                break
            files.append(rel)
        if truncated:
            break
    if len(files) > limit:
        truncated = True
        files = files[:limit]
    files.sort()
    return {"repo": name, "prefix": want, "files": files,
            "truncated": truncated}


def _git_failed(proc, verb: str) -> RuntimeError:
    err = (proc.stderr or "").strip()
    detail = err.splitlines()[-1] if err else f"git {verb} failed"
    return RuntimeError(detail)


def get_repo_status(repo: str) -> dict[str, Any]:
    """Read-only git status for one repo checkout.

    Returns {"repo", "branch", "short", "clean"}. Raises
    ValueError on a bad name, FileNotFoundError when the checkout
    (or its .git) is absent."""
    import subprocess

    name = validate_repo_name(repo)
    path = _checkout_path(name)
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
        raise _git_failed(status_proc, "status")
    short = status_proc.stdout
    lines = [ln for ln in short.splitlines()
             if ln and not ln.startswith("##")]
    return {"repo": name, "branch": branch, "short": short,
            "clean": len(lines) == 0}


# Full-diff truncation cap for the diff modal (#663): the modal
# shows status + stat in full and flags a cut diff.
DIFF_CAP = 20000


def get_repo_diff(repo: str, run=None) -> dict[str, Any]:
    """Read-only status + diff for one repo checkout (#663).

    Returns {"repo", "branch", "clean", "status_short",
    "diff_stat", "diff", "truncated"}. Same error contract as
    get_repo_status. ``run`` injects tests.
    """
    import subprocess

    run = run or subprocess.run
    name = validate_repo_name(repo)
    path = _checkout_path(name)

    def git(*args):
        return run(
            ["git", "-c", f"safe.directory={path}", "-C", str(path),
             *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True,
        )

    branch_proc = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else ""
    status_proc = git("status", "--short")
    if status_proc.returncode != 0:
        raise _git_failed(status_proc, "status")
    stat_proc = git("diff", "--stat")
    if stat_proc.returncode != 0:
        raise _git_failed(stat_proc, "diff --stat")
    diff_proc = git("diff")
    if diff_proc.returncode != 0:
        raise _git_failed(diff_proc, "diff")
    short = status_proc.stdout
    diff = diff_proc.stdout
    truncated = len(diff) > DIFF_CAP
    return {"repo": name, "branch": branch,
            "clean": len([ln for ln in short.splitlines() if ln]) == 0,
            "status_short": short, "diff_stat": stat_proc.stdout,
            "diff": diff[:DIFF_CAP], "truncated": truncated}

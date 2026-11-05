"""Shared routines for the backend package.

Factored from existing conventions in buttons/server.py and scripts/:
- env/config loading via os.environ.get with defaults
- repo path resolution under a repos root
- structured JSON output via json.dumps
"""

from __future__ import annotations

import json
import os
import re
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


def _compose_argv() -> list[str]:
    """`docker compose -f <stack>/docker-compose.yml` prefix (#928).

    One canonical stack prefix for every compose call in this
    module (ps, restart); BACKEND_STACK_DIR override preserved.
    """
    stack = os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server")
    return ["docker", "compose", "-f", f"{stack}/docker-compose.yml"]


def _compose_containers_result(run=None) -> tuple[list[dict[str, Any]], str | None]:
    """Stack containers from `docker compose ps` (read-only).

    Returns (containers, error): ``error`` names the failure
    class (spawn/timeout/exit code/parse) so callers can tell
    "nothing running" from "couldn't ask docker" (#530).
    ``run`` injects tests (defaults to subprocess.run).
    """
    import subprocess

    run = run or subprocess.run
    compose = _compose_argv()
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
        "state": c["state"], "status": c.get("status", ""),
        "health": c.get("health", ""),
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


CONTAINER_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CONTAINER_LOGS_TAIL_MAX = 500

RESTART_TIMEOUT = 120

LOGS_TIMEOUT = 15


def _validate_service_name(service: str) -> str:
    """Compose service name; raises ValueError when malformed."""
    if not isinstance(service, str) \
            or not CONTAINER_SERVICE_RE.match(service):
        raise ValueError(f"invalid container service: {service!r}")
    return service


def _service_container_names(run=None) -> dict[str, str]:
    """Live service -> container Name map from `docker compose ps`.

    The authoritative allowlist for per-container reads and
    restarts: only stack members resolve, so callers can never
    address an arbitrary container. A compose failure degrades to
    {} (callers then report unknown service).
    """
    containers, _ = _compose_containers_result(run=run)
    names: dict[str, str] = {}
    for c in containers:
        service = c.get("service") or ""
        name = c.get("name") or ""
        if service and name:
            names.setdefault(service, name)
    return names


def get_container_logs(service: str, tail: int = 100,
                       run=None) -> dict[str, Any]:
    """Last ``tail`` lines of `docker logs` for one stack container.

    Powers the Status log slide-over (#928): read-only, one-shot
    (never a streaming daemon), stderr merged into the same lines.
    Raises ValueError on a malformed name or tail, KeyError on an
    unknown service, RuntimeError on docker failures.
    ``run`` injects tests (defaults to subprocess.run).
    """
    import subprocess

    run = run or subprocess.run
    _validate_service_name(service)
    if not isinstance(tail, int) \
            or not 1 <= tail <= CONTAINER_LOGS_TAIL_MAX:
        raise ValueError(
            f"invalid tail: {tail!r} (1-{CONTAINER_LOGS_TAIL_MAX})")
    names = _service_container_names(run=run)
    if service not in names:
        raise KeyError(service)
    try:
        proc = run(
            ["docker", "logs", "--tail", str(tail), names[service]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=LOGS_TIMEOUT, text=True,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"docker logs timed out after {LOGS_TIMEOUT}s")
    except Exception as exc:
        raise RuntimeError(f"docker logs failed: {exc}")
    if proc.returncode != 0:
        err = (proc.stdout or "").strip()
        if "no such container" in err.lower():
            raise KeyError(service)
        tail_line = err.splitlines()[-1] if err else "unknown error"
        raise RuntimeError(
            f"docker logs exited {proc.returncode}: {tail_line}")
    lines = (proc.stdout or "").splitlines()
    return {"service": service, "name": names[service],
            "lines": lines, "truncated": len(lines) >= tail}


def restart_container(service: str, run=None) -> dict[str, Any]:
    """`docker compose restart` for one stack container (#928).

    The plain restart path for services without a jobs-engine
    action (buttons/code-server keep their engine actions).
    Terminal is refused: recreating it drops live agent sessions
    with no resume path (#444). Raises ValueError on a malformed
    name or terminal, KeyError on an unknown service,
    RuntimeError on docker failures. ``run`` injects tests.
    """
    import subprocess

    run = run or subprocess.run
    _validate_service_name(service)
    if service == "terminal":
        raise ValueError(
            "terminal restart is not offered: live agent sessions "
            "cannot survive it (#444)")
    if service not in _service_container_names(run=run):
        raise KeyError(service)
    try:
        proc = run(
            _compose_argv() + ["restart", service],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=RESTART_TIMEOUT, text=True,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"docker compose restart timed out after "
            f"{RESTART_TIMEOUT}s")
    except Exception as exc:
        raise RuntimeError(f"docker compose restart failed: {exc}")
    if proc.returncode != 0:
        err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        tail_line = err.splitlines()[-1] if err else "unknown error"
        raise RuntimeError(
            f"docker compose restart exited {proc.returncode}: "
            f"{tail_line}")
    return {"ok": True, "service": service}


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


def _parse_ahead_behind(header: str) -> tuple[int, int]:
    """(ahead, behind) from a ``status --short --branch`` header.

    ``## main...origin/main [ahead 1, behind 2]``; a missing
    upstream or count reads as 0. Pure helper for list_repos (#937).
    """
    import re

    ahead = re.search(r"ahead (\d+)", header or "")
    behind = re.search(r"behind (\d+)", header or "")
    return (int(ahead.group(1)) if ahead else 0,
            int(behind.group(1)) if behind else 0)


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
    """Every discovered checkout as {name, full, branch, clean,
    ahead, behind, status_count}.

    Port of buttons/server.py _discover_repos, minus the td/gh
    runtime fields: the special stack checkout (remote-server)
    plus every repos-dir entry with a .git. Entries that fail
    git probing are skipped, never mapped to another repo; a
    checkout without a parseable GitHub origin keeps the
    default full name ``arii/<name>``. ``ahead``/``behind`` parse
    the branch header (#937); ``status_count`` counts dirty
    entries. ``repos_dir``/``stack_dir``/``run`` are injectable
    seams for tests.
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
            header = next(
                (ln for ln in status_proc.stdout.splitlines()
                 if ln.startswith("##")), "")
            ahead, behind = _parse_ahead_behind(header)
            lines = [ln for ln in status_proc.stdout.splitlines()
                     if ln and not ln.startswith("##")]
            repos.append({"name": name, "full": full or f"arii/{name}",
                          "branch": branch, "clean": len(lines) == 0,
                          "ahead": ahead, "behind": behind,
                          "status_count": len(lines)})
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

# Per-file summary cap for the diff modal (#927): the file list
# is a preview, the counts stay honest via files_truncated.
DIFF_FILES_SHOWN = 200

# Raw status lines kept in the payload (#927): --short already
# collapses untracked dirs, so this only bites on pathological
# trees; clean is always computed from the full output.
STATUS_SHORT_LINES_CAP = 500


def _numstat_counts(numstat_out: str) -> dict[str, tuple[int, int]]:
    """Added/deleted line counts per path from git diff --numstat."""
    counts: dict[str, tuple[int, int]] = {}
    for line in (numstat_out or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            counts[parts[2]] = (int(parts[0]), int(parts[1]))
        except ValueError:
            continue  # Binary ("-\t-\tpath") or garbage.
    return counts


def _parse_diff_files(status_out: str,
                      counts: dict[str, tuple[int, int]]
                      ) -> list[dict[str, Any]]:
    """Per-file summaries from status --short + numstat counts."""
    files: list[dict[str, Any]] = []
    for line in (status_out or "").splitlines():
        if len(line) < 4 or line[2] != " ":
            continue
        x, y, rest = line[0], line[1], line[3:]
        if not rest:
            continue
        count = counts.get(rest, (0, 0))
        if count == (0, 0) and " -> " in rest:
            count = counts.get(rest.split(" -> ", 1)[1], (0, 0))
        code = f"{x}{y}".strip()
        files.append({"path": rest, "status": code or "M",
                      "staged": x not in " ?!",
                      "unstaged": y not in " ?!",
                      "additions": count[0], "deletions": count[1]})
    return files


def get_repo_diff(repo: str, run=None) -> dict[str, Any]:
    """Read-only status + diff for one repo checkout (#663, #927).

    Returns {"repo", "branch", "clean", "status_short",
    "status_truncated", "diff_stat", "diff", "truncated", "files",
    "files_truncated}. The diff covers staged AND unstaged work
    (diff HEAD); files carries per-file summaries parsed from
    status --short plus numstat counts. Same error contract as
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

    def diff_head(*args):
        """diff HEAD with an unborn-HEAD fallback to plain diff."""
        proc = git("diff", "HEAD", *args)
        if proc.returncode == 0:
            return proc
        head = git("rev-parse", "--verify", "--quiet", "HEAD")
        if head.returncode == 0:
            raise _git_failed(proc, "diff HEAD")
        plain = git("diff", *args)
        if plain.returncode != 0:
            raise _git_failed(proc, "diff HEAD")
        return plain

    branch_proc = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else ""
    status_proc = git("status", "--short")
    if status_proc.returncode != 0:
        raise _git_failed(status_proc, "status")
    stat_proc = diff_head("--stat")
    diff_proc = diff_head()
    numstat_proc = diff_head("--numstat")
    short = status_proc.stdout or ""
    lines = [ln for ln in short.splitlines() if ln]
    status_lines = short.splitlines()
    status_truncated = len(status_lines) > STATUS_SHORT_LINES_CAP
    if status_truncated:
        short = "\n".join(status_lines[:STATUS_SHORT_LINES_CAP]) + "\n"
    diff = diff_proc.stdout or ""
    truncated = len(diff) > DIFF_CAP
    files = _parse_diff_files(status_proc.stdout,
                              _numstat_counts(numstat_proc.stdout))
    files_truncated = len(files) > DIFF_FILES_SHOWN
    return {"repo": name, "branch": branch, "clean": len(lines) == 0,
            "status_short": short, "status_truncated": status_truncated,
            "diff_stat": stat_proc.stdout, "diff": diff[:DIFF_CAP],
            "truncated": truncated, "files": files[:DIFF_FILES_SHOWN],
            "files_truncated": files_truncated}


# One-click git actions for the diff modal (#927): longer timeout
# for fetch/rebase/stash/clean, fixed-vocabulary details (raw git
# output is never quoted: it trips the History/autofile failure
# regexes and can echo remote URLs).
REPO_ACTION_TIMEOUT = 120
CONSOLE_STASH_PREFIX = "console diff-modal"


def _action_result(action: str, repo: str, ok: bool, clean: bool,
                   detail: str) -> dict[str, Any]:
    return {"ok": ok, "repo": repo, "action": action, "clean": clean,
            "detail": detail}


def _action_status_lines(run, path: Path) -> list[str] | None:
    """Current status lines, or None when git itself fails."""
    import subprocess

    try:
        proc = _git_run(run, ["status", "--short"], path)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return [ln for ln in (proc.stdout or "").splitlines() if ln]


def repo_stash(repo: str, run=None) -> dict[str, Any]:
    """Stash all local work incl. untracked files (#927).

    Returns {"ok", "repo", "action", "clean", "detail"}. Raises
    ValueError on a bad name, FileNotFoundError when the checkout
    is absent. ``run`` injects tests.
    """
    import subprocess
    import time

    name = validate_repo_name(repo)
    path = _checkout_path(name)
    try:
        pre = _action_status_lines(run, path)
        if pre is not None and not pre:
            return _action_result("stash", name, True, True,
                                   "already clean - nothing to stash")
        proc = _git_run(
            run, ["stash", "push", "-u", "-m",
                  f"{CONSOLE_STASH_PREFIX} {int(time.time())}"],
            path, timeout=REPO_ACTION_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return _action_result("stash", name, False, False,
                               "timed out stashing; retry")
    if proc.returncode != 0:
        out = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
        if "no local changes" in out:
            return _action_result("stash", name, True, True,
                                   "already clean - nothing to stash")
        return _action_result("stash", name, False, False,
                               "stash failed; stash or commit by hand, "
                               "then retry")
    post = _action_status_lines(run, path)
    return _action_result("stash", name, True,
                           post is not None and not post,
                           "changes stashed")


def repo_discard_untracked(repo: str, run=None) -> dict[str, Any]:
    """Remove untracked files/dirs, never ignored ones (#927).

    git clean -fd without -x: build output and env files stay.
    Same return/error contract as repo_stash.
    """
    import subprocess

    name = validate_repo_name(repo)
    path = _checkout_path(name)
    try:
        pre = _action_status_lines(run, path)
        untracked = [ln for ln in (pre or [])
                     if ln.startswith("??")] if pre is not None else None
        if untracked is not None and not untracked:
            return _action_result(
                "discard-untracked", name, True, not pre,
                "no untracked files to discard")
        proc = _git_run(run, ["clean", "-fd"], path,
                        timeout=REPO_ACTION_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return _action_result("discard-untracked", name, False, False,
                               "timed out discarding; retry")
    if proc.returncode != 0:
        return _action_result(
            "discard-untracked", name, False, False,
            "discard failed; remove untracked files by hand, then retry")
    removed = len([ln for ln in (proc.stdout or "").splitlines()
                   if ln.startswith("Removing ")])
    post = _action_status_lines(run, path)
    detail = f"discarded {removed} untracked path(s)" if removed \
        else "untracked files discarded"
    return _action_result("discard-untracked", name, True,
                           post is not None and not post, detail)


def repo_sync(repo: str, run=None) -> dict[str, Any]:
    """Fetch origin and rebase onto origin/main (#927).

    Refuses dirty trees with a plain message; a failed rebase is
    always aborted so the tree is left unchanged. Same
    return/error contract as repo_stash.
    """
    import subprocess

    name = validate_repo_name(repo)
    path = _checkout_path(name)
    try:
        pre = _action_status_lines(run, path)
        if pre:
            return _action_result(
                "sync", name, False, False,
                "tree has local changes; stash or commit before syncing")
        fetch = _git_run(run, ["fetch", "origin"], path,
                         timeout=REPO_ACTION_TIMEOUT)
        if fetch.returncode != 0:
            return _action_result("sync", name, False, pre is not None
                                   and not pre,
                                   "fetch failed; check network, then retry")
        verify = _git_run(run, ["rev-parse", "--verify", "--quiet",
                                "origin/main"], path)
        if verify.returncode != 0:
            return _action_result("sync", name, False, pre is not None
                                   and not pre,
                                   "origin/main not found; sync by hand")
        rebase = _git_run(run, ["rebase", "origin/main"], path,
                          timeout=REPO_ACTION_TIMEOUT)
        if rebase.returncode == 0:
            post = _action_status_lines(run, path)
            out = f"{rebase.stdout or ''}\n{rebase.stderr or ''}"
            detail = "already up to date with origin/main" \
                if "up to date" in out else "rebased onto origin/main"
            return _action_result("sync", name, True,
                                   post is not None and not post, detail)
        out = f"{rebase.stdout or ''}\n{rebase.stderr or ''}".lower()
        try:
            _git_run(run, ["rebase", "--abort"], path,
                     timeout=REPO_ACTION_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            pass
        post = _action_status_lines(run, path)
        detail = "rebase conflicted; aborted, tree unchanged - " \
            "resolve by hand" if "conflict" in out \
            else "rebase failed; aborted, tree unchanged - sync by hand"
        return _action_result("sync", name, False,
                               post is not None and not post, detail)
    except (subprocess.TimeoutExpired, OSError):
        return _action_result("sync", name, False, False,
                               "timed out syncing; retry")


# Top marker hits returned per metrics read; the count itself is
# complete (the strip shows "N TODOs", hits are a preview).
TODO_HITS_SHOWN = 8


def get_repo_metrics(repo: str, run=None) -> dict[str, Any]:
    """Read-only worklist metrics for one repo checkout (#897).

    Returns {"repo", "branch", "clean", "status_count",
    "todo_count", "todo_hits", "diff_stat", "diff_files"}. Same
    error contract as get_repo_status. ``run`` injects tests. All
    three reads are local and unmetered, so the console prefetches
    on every repo select with zero model turns.
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
    branch = branch_proc.stdout.strip() \
        if branch_proc.returncode == 0 else ""
    status_proc = git("status", "--short")
    if status_proc.returncode != 0:
        raise _git_failed(status_proc, "status")
    stat_proc = git("diff", "--stat")
    if stat_proc.returncode != 0:
        raise _git_failed(stat_proc, "diff --stat")
    short = status_proc.stdout
    stat = stat_proc.stdout
    diff_files = len([ln for ln in stat.splitlines() if "|" in ln])

    # Tracked files only: git grep is gitignore-aware, so vendored
    # build output (.pnpm-store, dist) never pollutes the count.
    todo_proc = git("grep", "-n", "-i", "-E",
                    r"\b(TODO|FIXME|XXX|HACK)\b", "--", ".")
    if todo_proc.returncode not in (0, 1):
        raise RuntimeError(
            f"todo grep failed: "
            f"{(todo_proc.stderr or '').strip()[:200]}")
    hits = []
    count = 0
    for line in (todo_proc.stdout or "").splitlines():
        if not line.strip():
            continue
        count += 1
        if len(hits) >= TODO_HITS_SHOWN:
            continue
        file_part, _, rest = line.partition(":")
        lineno, _, text = rest.partition(":")
        try:
            lineno = int(lineno.strip())
        except ValueError:
            continue
        hits.append({"file": file_part.strip(), "line": lineno,
                     "text": text.strip()[:120]})
    return {"repo": name, "branch": branch,
            "clean": len([ln for ln in short.splitlines()
                          if ln]) == 0,
            "status_count": len([ln for ln in short.splitlines()
                                 if ln]),
            "todo_count": count, "todo_hits": hits,
            "diff_stat": stat, "diff_files": diff_files}


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

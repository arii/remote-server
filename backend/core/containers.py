"""Container and box-health helpers (split from backend.core)."""

from __future__ import annotations

import json
import os
import re
from typing import Any


def get_system_health() -> dict[str, Any]:
    """Read-only box health: disk, memory, container states.

    Port of the logic behind buttons/server.py _get_system_health:
    shutil disk usage, /proc/meminfo parsing, and
    `docker compose ps --format json` (read-only). A docker
    failure surfaces as ``containers_error`` (#530); only a
    successful empty listing reads as zero containers."""
    import shutil

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

"""Container restart-loop detection (#1514).

Docker restarts a crashing container on its own, so a loop is
invisible in the console: nothing the operator clicked, nothing in
History. This module rides the shared schedule tick (no new daemon):
each tick it reads every stack container's RestartCount, compares it
with the count seen last tick, writes one ledger row per container
whose count grew, and keeps a one-hour window of those increases.
The attention queue reads the window to surface a loop.

State file: {container name: {"count": last RestartCount,
"recent": [[ts, restarts added], ...]}} at
BUTTONS_CONTAINER_RESTARTS_PATH. The first sighting of a container
only sets the baseline, so deploying this never replays old history.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from backend import ledger
from backend.core.containers import _compose_containers_result

DEFAULT_STATE_PATH = "/var/lib/buttons/container_restarts.json"
LOOP_WINDOW_SECONDS = 3600
LOOP_MIN_RESTARTS = 3
INSPECT_TIMEOUT = 10

_state_lock = threading.Lock()


def state_path() -> str:
    return os.environ.get("BUTTONS_CONTAINER_RESTARTS_PATH", DEFAULT_STATE_PATH)


def read_restart_counts(names: list[str], run=None) -> dict[str, int]:
    """Container name -> RestartCount via one `docker inspect`.

    Raises RuntimeError when docker cannot answer, so a docker
    outage is never mistaken for "no restarts". ``run`` injects
    tests (defaults to subprocess.run).
    """
    import subprocess

    if not names:
        return {}
    run = run or subprocess.run
    try:
        proc = run(
            ["docker", "inspect", "--format", "{{.Name}} {{.RestartCount}}", *names],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=INSPECT_TIMEOUT,
            text=True,
        )
    except Exception as exc:
        raise RuntimeError(f"docker inspect failed: {exc}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        tail = err.splitlines()[-1] if err else "unknown error"
        raise RuntimeError(f"docker inspect exited {proc.returncode}: {tail}")
    counts: dict[str, int] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        counts[parts[0].lstrip("/")] = int(parts[1])
    return counts


def observe(
    state: dict, counts: dict[str, int], services: dict[str, str], now: float
) -> tuple[dict, list[dict]]:
    """Pure step: previous state + current counts -> (new state,
    [{"name", "restarts"}] for containers whose count grew).

    A count that dropped means the container was recreated: it
    becomes the new baseline and records nothing. Containers no
    longer in ``counts`` are dropped from the state."""
    new_state: dict[str, Any] = {}
    grew: list[dict] = []
    cutoff = now - LOOP_WINDOW_SECONDS
    for name, count in counts.items():
        prev = state.get(name) if isinstance(state.get(name), dict) else None
        recent = []
        last = count
        if prev is not None:
            for row in prev.get("recent") or []:
                if (
                    isinstance(row, list)
                    and len(row) == 2
                    and isinstance(row[0], (int, float))
                    and isinstance(row[1], int)
                    and row[0] >= cutoff
                ):
                    recent.append(row)
            before = prev.get("count")
            if isinstance(before, int) and count > before:
                recent.append([now, count - before])
                grew.append({"name": name, "restarts": count - before})
        new_state[name] = {
            "count": last,
            "service": services.get(name, name),
            "recent": recent,
        }
    return new_state, grew


def _read_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> None:
    path = state_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True)
    os.replace(tmp, path)


def record_container_restarts(run=None, now: float | None = None) -> list[dict]:
    """One tick: write a History row per container that restarted
    since the last tick. Returns the rows' {"name", "restarts"}.

    Raises RuntimeError when docker cannot be read; the state is
    left untouched so the next tick compares against the same
    baseline."""
    now = time.time() if now is None else now
    containers, err = _compose_containers_result(run=run)
    if err:
        raise RuntimeError(err)
    services = {c["name"]: c["service"] for c in containers if c.get("name")}
    names = list(services)
    counts = read_restart_counts(names, run=run)
    with _state_lock:
        new_state, grew = observe(_read_state(), counts, services, now)
        _write_state(new_state)
    for g in grew:
        ledger.append(
            {
                "kind": "system",
                "event": f"container {g['name']} restarted on its own",
                "container": g["name"],
                "restarts": g["restarts"],
            }
        )
    return grew


def list_loops(now: float | None = None) -> list[dict]:
    """Containers with at least LOOP_MIN_RESTARTS restarts in the
    last hour: [{"name", "service", "restarts", "since"}], read from the state
    the tick keeps. [] when nothing is looping or no state exists."""
    now = time.time() if now is None else now
    cutoff = now - LOOP_WINDOW_SECONDS
    loops = []
    with _state_lock:
        state = _read_state()
    for name, row in sorted(state.items()):
        if not isinstance(row, dict):
            continue
        recent = [
            r
            for r in row.get("recent") or []
            if isinstance(r, list)
            and len(r) == 2
            and isinstance(r[0], (int, float))
            and isinstance(r[1], int)
            and r[0] >= cutoff
        ]
        total = sum(r[1] for r in recent)
        if total >= LOOP_MIN_RESTARTS:
            loops.append(
                {
                    "name": name,
                    "service": row.get("service") or name,
                    "restarts": total,
                    "since": min(r[0] for r in recent),
                }
            )
    return loops

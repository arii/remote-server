"""briefing + ledger router: ports of the buttons endpoints.

Same routes/verbs as buttons/server.py, under the backend's /api
prefix convention (buttons serves /buttons/api/*; this backend serves
/api/* like its sibling routers):

- GET /api/briefing?repo=  structured briefing passthrough
- GET /api/status           backend status (health + empty run lists)
- GET /api/ledger?kind=&limit=  newest-first audit entries
- POST /api/ledger/clear    truncate + tombstone

Briefing reads are NOT ledgered: reads are not actions.
"""

import json
import os
import subprocess
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.core import get_system_health, validate_repo_name
from backend.exec.engine import stack_dir
from backend.ledger import LEDGER_KINDS, clear as ledger_clear
from backend.ledger import read as ledger_read

router = APIRouter(prefix="/api", tags=["briefing"])


class JulesSection(BaseModel):
    model_config = ConfigDict(extra="allow")

    items: list = Field(default_factory=list)
    other: int = 0
    unscoped: int = 0
    unavailable: str | None = None


class Briefing(BaseModel):
    """Structured briefing payload: shape of analyze() in
    core_cli/briefing_summary.py. Extra keys allowed so new briefing
    sections pass through instead of 500ing."""

    model_config = ConfigDict(extra="allow")

    repo: str = ""
    attention: list = Field(default_factory=list)
    attention_with_markers: list = Field(default_factory=list)
    prs: list = Field(default_factory=list)
    prs_err: str = ""
    reviews_lines: list = Field(default_factory=list)
    reviews_err: dict = Field(default_factory=dict)
    board: list = Field(default_factory=list)
    board_lines: list = Field(default_factory=list)
    board_err: str = ""
    conflicts: list = Field(default_factory=list)
    conflicts_lines: list = Field(default_factory=list)
    conf_err: str = ""
    overlaps: list = Field(default_factory=list)
    overlaps_lines: list = Field(default_factory=list)
    failing_runs: list = Field(default_factory=list)
    workflow_lines: list = Field(default_factory=list)
    runs_err: str = ""
    jules: JulesSection = Field(default_factory=JulesSection)
    jules_lines: list = Field(default_factory=list)


class BackendStatus(BaseModel):
    """Backend status: mirrors the shape of /buttons/api/status
    (runs, webhooks, health) but honest — this backend owns no run
    queue, so runs/webhooks are empty and no activity is faked."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    service: str = "backend"
    runs: list = Field(default_factory=list)
    webhooks: dict = Field(default_factory=dict)
    health: dict = Field(default_factory=dict)


class LedgerList(BaseModel):
    entries: list = Field(default_factory=list)


class LedgerClearResult(BaseModel):
    ok: bool = True


# TTL cache with stale-while-revalidate: the gh fan-out behind
# briefing_summary takes ~6s and every uncached visit blocks on it.
# Fresh entries serve instantly; stale entries serve instantly and
# refresh on a daemon thread; entries past MAX_STALE recompute
# synchronously so a broken refresher can never pin ancient data.
# Tests reset _BRIEFING_CACHE/_BRIEFING_REFRESHING directly.
BRIEFING_CACHE_TTL = 60.0
BRIEFING_CACHE_MAX_STALE = 1800.0
_BRIEFING_CACHE: dict[str, tuple[float, dict]] = {}
_BRIEFING_REFRESHING: set[str] = set()
_BRIEFING_LOCK = threading.Lock()


def _store_briefing(repo: str, data: dict) -> float:
    ts = time.time()
    with _BRIEFING_LOCK:
        _BRIEFING_CACHE[repo] = (ts, data)
        _BRIEFING_REFRESHING.discard(repo)
    return ts


def _refresh_briefing(repo: str, gh: str) -> None:
    """Background SWR refresh: failures keep the stale entry."""
    try:
        data = _run_briefing(repo, gh)
    except Exception:
        with _BRIEFING_LOCK:
            _BRIEFING_REFRESHING.discard(repo)
        return
    _store_briefing(repo, data)


def _revalidate_briefing(repo: str, gh: str) -> None:
    with _BRIEFING_LOCK:
        if repo in _BRIEFING_REFRESHING:
            return
        _BRIEFING_REFRESHING.add(repo)
    thread = threading.Thread(
        target=_refresh_briefing, args=(repo, gh), daemon=True)
    thread.start()


def get_briefing(name: str, gh: str) -> dict:
    """Injectable seam: briefing payload with SWR caching.

    Every response carries freshness markers (cached/stale/
    generated_at) that the Briefing model passes through via
    extra="allow" so the view can show a refresh indicator.
    Copies annotate per-request markers; the cached entry itself
    is never mutated.
    """
    now = time.time()
    hit = _BRIEFING_CACHE.get(name)
    if hit is not None:
        age = now - hit[0]
        if age < BRIEFING_CACHE_TTL:
            return {**hit[1], "cached": True, "stale": False,
                    "generated_at": hit[0]}
        if age < BRIEFING_CACHE_MAX_STALE:
            _revalidate_briefing(name, gh)
            return {**hit[1], "cached": True, "stale": True,
                    "generated_at": hit[0]}
    data = _run_briefing(name, gh)
    ts = _store_briefing(name, data)
    return {**data, "cached": False, "stale": False,
            "generated_at": ts}


def _run_briefing(repo: str, gh: str) -> dict:
    script = os.path.join(os.path.dirname(__file__),
                          "..", "..", "..", "core_cli",
                          "briefing_summary.py")
    script = os.path.normpath(script)
    # cwd = live checkout: td discovers project_config.json upward from
    # cwd, and the image workdir (/app) has none. Script stays baked.
    proc = subprocess.run(
        ["python3", script, "--briefing-json",
         "--repo", repo, "--gh", gh],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=180, text=True, cwd=stack_dir())
    # The script exits 1 when no main source worked (no PRs + no
    # board), but its stdout JSON is still complete and carries the
    # structured errs/empties the app renders. Only fail when stdout
    # is unusable.
    try:
        return json.loads(proc.stdout)
    except ValueError:
        pass
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or "").strip().splitlines()[-1]
            if proc.stderr.strip() else "briefing failed")
    raise RuntimeError("briefing returned invalid JSON")


@router.get("/briefing", response_model=Briefing)
def briefing(repo: str = "remote-server") -> dict:
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Fixture override, same as buttons: BUTTONS_FIXTURE_BRIEFING or a
    # fixture-briefing.json next to the repo checkouts.
    fixture_path = os.environ.get("BUTTONS_FIXTURE_BRIEFING", "")
    if not fixture_path:
        candidate = os.path.join(
            os.environ.get("BUTTONS_REPOS_DIR", "/home/ari/repos"),
            "fixture-briefing.json")
        if os.path.exists(candidate):
            fixture_path = candidate
    if fixture_path and os.path.exists(fixture_path):
        try:
            with open(fixture_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data["repo"] = name
                # Fixtures bypass the cache (fast already) but carry
                # the same freshness markers for a uniform contract.
                data["cached"] = False
                data["stale"] = False
                data["generated_at"] = time.time()
            return data
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"fixture briefing failed: {exc}")

    try:
        return get_briefing(name, f"arii/{name}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:500])


@router.get("/status", response_model=BackendStatus)
def status() -> dict:
    return {"ok": True, "service": "backend", "runs": [],
            "webhooks": {}, "health": get_system_health()}


@router.get("/ledger", response_model=LedgerList)
def ledger(kind: str = "all", limit: int = 50) -> dict:
    if kind != "all" and kind not in LEDGER_KINDS:
        raise HTTPException(status_code=400, detail="unknown kind")
    limit = max(1, min(200, limit))
    return {"entries": ledger_read(kind, limit)}


@router.post("/ledger/clear", response_model=LedgerClearResult)
def ledger_clear_route() -> dict:
    if ledger_clear():
        return {"ok": True}
    raise HTTPException(status_code=500, detail="ledger clear failed")

"""inbox router: aggregated triage inbox feed.

GET /api/inbox?repo= returns one ranked feed for a triage inbox:
{repo, updated_at, attention:[...], merge_ready:{count, top:[...]},
 runs:{active, recent:[...]}}.

Reuses existing seams only (no rewrite, no new deps):
- attention items: attention.attention (its own injectable seams)
- merge-ready PRs: pulls.prs rows filtered by merge_block_reason,
  a port of the frontend mergeBlockReason predicate
  (frontend/src/reviewTypes.ts)
- runs: the jobs registry (same records as GET /api/jobs)

All source readers are module-level functions so tests can
monkeypatch them (injectable seams).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.api.cache import SWRCache
from backend.api.routers import attention, pulls
from backend.api.routers.attention import AttentionItem
from backend.api.routers.jobs import registry
from backend.core.git_helpers import list_repos
from backend.core.validation import validate_repo_name

router = APIRouter(prefix="/api", tags=["inbox"])

RECENT_RUNS_LIMIT = 5
MERGE_READY_TOP = 3

# Rollup pseudo-repo: GET /api/inbox?repo=all aggregates the
# attention queue across every known checkout (issue #610).
# Every other endpoint still treats its repo param as one checkout.
ALL_REPOS = "all"

# Short-TTL SWR cache (#888): the attention + PR fan-out behind the
# feed (~1.2-2.1s) runs on every Now tab switch without it. Keys are
# validated short repo names plus ALL_REPOS for the rollup, which
# composes cached per-repo entries. Mutations that change the lists
# (pr_action, deferred rows, webhook pulls) invalidate via
# invalidate_inbox_cache. Tests reset _INBOX_CACHE directly.
INBOX_CACHE_TTL = 45.0
INBOX_CACHE_MAX_STALE = 300.0
_INBOX_CACHE = SWRCache(ttl=INBOX_CACHE_TTL,
                        max_stale=INBOX_CACHE_MAX_STALE)


def invalidate_inbox_cache(repo: str | None = None) -> None:
    """Drop cached feeds: one repo (short or full name) plus the
    all-repos rollup that composes per-repo entries — or everything
    when repo is None. Never raises."""
    if repo is None:
        _INBOX_CACHE.clear()
        return
    short = str(repo).split("/")[-1].strip()
    if short:
        _INBOX_CACHE.invalidate(short, ALL_REPOS)


def list_repo_names() -> list:
    """Injectable seam: known checkout names in picker order."""
    return [r.get("name", "") for r in list_repos()
            if isinstance(r, dict) and r.get("name")]


def list_attention_items(repo: str) -> list:
    """Injectable seam: attention items in ranked feed order."""
    return attention.attention(repo).get("items", [])


def list_open_prs(repo: str) -> list:
    """Injectable seam: open PR rows shaped by the pulls router."""
    return pulls.prs(repo).get("prs", [])


def list_runs() -> list:
    """Injectable seam: run records, newest first (jobs registry)."""
    with registry._lock:
        return sorted(registry.runs.values(),
                      key=lambda r: r.get("seq", 0), reverse=True)


def merge_block_reason(row: dict) -> str | None:
    """Port of frontend mergeBlockReason (reviewTypes.ts), applied
    to pulls.prs row dicts. Gates only on known-bad state.
    Sync guard (#553): mergeReadiness.fixtures.json pins this port
    to the TS original via exact-equality tests on both sides —
    update the table with any semantic change."""
    if row.get("isDraft"):
        return "is a draft"
    review = row.get("reviewDecision") or ""
    if review != "APPROVED":
        return f"review is {review or 'unapproved'}"
    ci = row.get("ciStatus") or ""
    if ci != "passing":
        return f"CI is {ci or 'unknown'}"
    mergeable = row.get("mergeable") or ""
    if mergeable != "MERGEABLE":
        return f"merge state is {mergeable or 'unknown'}"
    return None


class MergeReadySummary(BaseModel):
    count: int = 0
    top: list = Field(default_factory=list)


class ActiveRun(BaseModel):
    id: str = ""
    action: str = ""
    started: float | None = None


class FinishedRun(BaseModel):
    id: str = ""
    action: str = ""
    exit: int | None = None


class RunsSummary(BaseModel):
    active: ActiveRun | None = None
    recent: list[FinishedRun] = Field(default_factory=list)


class InboxFeed(BaseModel):
    repo: str = ""
    updated_at: float = 0.0
    attention: list[AttentionItem] = Field(default_factory=list)
    merge_ready: MergeReadySummary = Field(
        default_factory=MergeReadySummary)
    runs: RunsSummary = Field(default_factory=RunsSummary)
    # Freshness markers (#888, same shape as briefing): True on
    # cache hits so the view can show a refresh indicator. Additive
    # only — older clients ignore the new keys.
    cached: bool = False
    stale: bool = False
    generated_at: float = 0.0


def _runs_summary() -> dict:
    """Active + recent runs from the jobs registry, shared by the
    per-repo and rollup builders."""
    runs = [r for r in list_runs() if isinstance(r, dict)]
    active = None
    for r in runs:
        if not r.get("done"):
            active = {"id": r.get("id", ""),
                      "action": r.get("action", ""),
                      "started": r.get("started_at")}
            break
    recent = [{"id": r.get("id", ""), "action": r.get("action", ""),
               "exit": r.get("exit_code")}
              for r in runs if r.get("done")][:RECENT_RUNS_LIMIT]
    return {"active": active, "recent": recent}


def _build_inbox(name: str) -> dict:
    """Uncached per-repo feed assembly."""
    items = list_attention_items(name)
    ready = [r for r in list_open_prs(name)
             if isinstance(r, dict) and merge_block_reason(r) is None]
    return {
        "repo": name,
        "updated_at": time.time(),
        "attention": items,
        "merge_ready": {
            "count": len(ready),
            "top": [{"number": r.get("number"),
                     "title": r.get("title", "")}
                    for r in ready[:MERGE_READY_TOP]],
        },
        "runs": _runs_summary(),
    }


def get_inbox(name: str) -> dict:
    """Injectable seam: per-repo feed with SWR caching + markers."""
    return _INBOX_CACHE.read(name, lambda: _build_inbox(name))


def _cached_or_live_attention(name: str) -> list:
    """Per-repo attention from the inbox cache when present, else a
    direct read. The rollup composes cached per-repo entries without
    ever triggering full per-repo builds (which would pay the PR
    fan-out the rollup skips by design)."""
    feed = _INBOX_CACHE.peek(name)
    if feed is not None:
        return feed.get("attention", [])
    return list_attention_items(name)


def _build_rollup() -> dict:
    """Uncached all-repos attention rollup (issue #610): fan out per
    repo, dedup by item id (legacy records without a repo default
    to each queried repo, so they repeat across lists), keep repo
    order. Each repo's list carries only its own items (issue
    #723). Merge-ready stays a per-repo concept: empty here by
    design (rollup is Now attention only)."""
    seen: set = set()
    items = []
    for name in list_repo_names():
        for item in _cached_or_live_attention(name):
            if not isinstance(item, dict):
                continue
            iid = item.get("id", "")
            if iid in seen:
                continue
            seen.add(iid)
            items.append(item)
    return {
        "repo": ALL_REPOS,
        "updated_at": time.time(),
        "attention": items,
        "merge_ready": {"count": 0, "top": []},
        "runs": _runs_summary(),
    }


def rollup_inbox() -> dict:
    """All-repos rollup with SWR caching under the ALL_REPOS key."""
    return _INBOX_CACHE.read(ALL_REPOS, _build_rollup)


@router.get("/inbox", response_model=InboxFeed)
def inbox(repo: str = "remote-server") -> dict:
    if repo == ALL_REPOS:
        return rollup_inbox()
    try:
        name = validate_repo_name(repo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return get_inbox(name)

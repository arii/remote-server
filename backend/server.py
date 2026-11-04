"""Minimal FastAPI application entrypoint."""

import os
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api.routers import agent, agents, attention, briefing, chat, deferred, inbox, issues, jobs, notify, ops, pulls, repo, system

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEEP_AGENT = os.path.join(REPO_ROOT, "deep-agent")
if _DEEP_AGENT not in sys.path:
    sys.path.insert(0, _DEEP_AGENT)

app = FastAPI(title="remote-server backend")

app.include_router(agent.router)
app.include_router(agents.router)
app.include_router(briefing.router)
app.include_router(repo.router)
app.include_router(repo.repos_router)
app.include_router(system.router)
app.include_router(jobs.router)
app.include_router(notify.router)
app.include_router(chat.router)
app.include_router(ops.router)
app.include_router(pulls.router)
app.include_router(issues.router)
app.include_router(attention.router)
app.include_router(inbox.router)
app.include_router(deferred.router)


def _index_state() -> str:
    """RAG index readiness for /health. Never raises: 'unknown' when the
    deep-agent tree is unavailable (partial checkouts)."""
    try:
        import retrieval

        return retrieval.index_status()
    except Exception:
        return "unknown"


@app.get("/health")
def health() -> dict:
    return {"ok": True, "index": _index_state()}


_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend", "dist")
if os.path.isdir(_DIST):
    # Production image: serve the built SPA under every non-API path.
    # Dev checkouts have no dist/ (untracked build output) and skip this.
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")

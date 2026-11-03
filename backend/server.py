"""Minimal FastAPI application entrypoint."""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api.routers import agent, agents, attention, briefing, chat, deferred, inbox, issues, jobs, notify, ops, pulls, repo, system

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


@app.get("/health")
def health() -> dict:
    return {"ok": True}


_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend", "dist")
if os.path.isdir(_DIST):
    # Production image: serve the built SPA under every non-API path.
    # Dev checkouts have no dist/ (untracked build output) and skip this.
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")

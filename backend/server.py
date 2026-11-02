"""Minimal FastAPI application entrypoint."""

from fastapi import FastAPI

from backend.api.routers import agents, attention, briefing, chat, issues, jobs, notify, ops, pulls, repo, system

app = FastAPI(title="remote-server backend")

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


@app.get("/health")
def health() -> dict:
    return {"ok": True}

"""Minimal FastAPI application entrypoint."""

from fastapi import FastAPI

from backend.api.routers import agents, jobs, notify, repo, system

app = FastAPI(title="remote-server backend")

app.include_router(agents.router)
app.include_router(repo.router)
app.include_router(system.router)
app.include_router(jobs.router)
app.include_router(notify.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}

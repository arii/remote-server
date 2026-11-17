"""Minimal FastAPI application entrypoint."""

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import skill_store
from backend.api.routers import (
    agent,
    agent_control,
    agents,
    attention,
    briefing,
    browser_drive,
    browser_ext,
    browser_skills,
    browser_tasks,
    browser_view,
    chat,
    deferred,
    dispatch,
    inbox,
    issues,
    jobs,
    notify,
    openwebui_shell,
    ops,
    pulls,
    repo,
    system,
)


@asynccontextmanager
async def _lifespan(app):
    """Start the shared schedule tick on boot (#822).

    Repair/action/digest/budget jobs all ride one 60s ticker, which
    used to start lazily on the first schedule creation — so a fresh
    server (or one that only ever consumed defaults) never fired
    anything, including the notify seeds. Starting it here makes the
    tick unconditional; _ensure_schedule_ticker stays idempotent so
    the lazy call sites remain harmless.

    Also precaches the briefing snapshot on a daemon thread (#929):
    first Briefing visits land on a warm cache instead of blocking
    on cold generation. Best-effort; boot never waits for it.

    Also verifies first-party and saved browser skill pins: a
    mismatch is logged loudly and recorded on
    `app.state.skills_verification_errors`, but boot continues —
    fail-closed would brick the console on any skill edit, so the
    first-party lock update rides the same PR as the skill change.
    """
    try:
        issues._ensure_schedule_ticker()
    except Exception:
        pass
    try:
        import skills_lock

        skill_problems = skills_lock.verify_skills()
    except Exception as exc:
        skill_problems = [f"first-party skill verifier error: {exc}"]
    try:
        skill_problems.extend(skill_store.verify_skills())
    except Exception as exc:
        skill_problems.append(f"browser skill verifier error: {exc}")
    app.state.skills_verification_errors = skill_problems
    attention.publish_skills_verification_errors(skill_problems)
    if skill_problems:
        print(
            "SKILLS VERIFICATION FAILED:\n"
            + "\n".join(f"  - {line}" for line in skill_problems),
            file=sys.stderr,
            flush=True,
        )
    try:
        import threading

        thread = threading.Thread(target=briefing.warm_briefing_cache, daemon=True)
        thread.start()
    except Exception:
        pass
    yield


app = FastAPI(title="remote-server backend", lifespan=_lifespan)

app.include_router(agent.router)
app.include_router(agent_control.router)
app.include_router(browser_drive.router)
app.include_router(browser_ext.router)
app.include_router(browser_skills.router)
app.include_router(browser_tasks.router)
app.include_router(agents.router)
app.include_router(briefing.router)
app.include_router(repo.router)
app.include_router(repo.repos_router)
app.include_router(system.router)
app.include_router(jobs.router)
app.include_router(notify.router)
app.include_router(openwebui_shell.router)
app.include_router(chat.router)
app.include_router(ops.router)
app.include_router(pulls.router)
app.include_router(issues.router)
app.include_router(attention.router)
app.include_router(inbox.router)
app.include_router(deferred.router)
app.include_router(dispatch.router)

# CDP-direct viewer (raw ASGI: the baked FastAPI/Starlette combo 403s
# websocket routes, so this mounts as plain ASGI, not a router).
app.mount("/cdp-stream", browser_view.create_app(), name="cdp_viewer")


def _index_state() -> str:
    """RAG index readiness for /health. Never raises: 'unknown' when the
    deep-agent tree is unavailable (partial checkouts)."""
    try:
        import retrieval

        return retrieval.index_status()
    except Exception:
        return "unknown"


def _index_chunks():
    """Embedding row count for /health. Never raises: None when the
    deep-agent tree is unavailable or the sqlite is unreadable."""
    try:
        import retrieval

        return retrieval.index_chunks()
    except Exception:
        return None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "index": _index_state(), "index_chunks": _index_chunks()}


# Built SPA dir: entry points run with cwd=repo root (/app in the image),
# so abspath() of the literal is exact with no __file__ climbing.
_DIST = os.path.abspath(os.path.join("frontend", "dist"))


def mount_spa(target, dist_dir=_DIST):
    """Serve the built SPA under every non-API path (#959).

    Extracted so tests mount a fixture dist through the same call
    the production image uses: StaticFiles with html=True serves
    manifest.json/icons by mimetype and falls back to index.html.
    """
    target.mount("/", StaticFiles(directory=dist_dir, html=True), name="spa")


if os.path.isdir(_DIST):
    # Production image only. Dev checkouts have no dist/ (untracked
    # build output) and skip this.
    mount_spa(app)

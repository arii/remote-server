"""agent router: PTY-backed interactive agent sessions (issue #320).

- POST     /api/agent/spawn  start a provider CLI in a PTY -> {session_id}
- WEBSOCKET /api/agent/ws/{session_id}  text frames both ways
- POST     /api/agent/abort  kill + reap a session

Websocket framing: client->server text is stdin, server->client text
is stdout; either side closing reaps the session. Spawning is a
hold-to-confirm destructive action ({intent: "agent-spawn",
confirmed: True} like the other destructive routes) because agent
CLIs are free-text shells; abort is the free escape hatch.
"""

# Agent sessions live OUTSIDE the jobs single-flight engine: interactive
# PTY lifecycles (many concurrent, user-driven stdin/stdout) vs batch runs
# (one at a time through backend.exec.engine).

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend.exec import agent_pty

router = APIRouter(prefix="/api/agent", tags=["agent"])

SPAWN_INTENT = "agent-spawn"
READ_POLL = 0.25


class SpawnRequest(BaseModel):
    provider: str = ""
    repo: str = "remote-server"
    intent: str = ""
    confirmed: bool = False


class SpawnResult(BaseModel):
    session_id: str


class AbortRequest(BaseModel):
    session_id: str = ""


class AbortResult(BaseModel):
    ok: bool = True
    exit: int | None = None


class ProviderList(BaseModel):
    providers: list[str] = []


def _repo_target(repo: str) -> tuple[str, str]:
    """Validate a repo name; return (name, cwd). Raises ValueError."""
    from backend.core import get_repo_status, resolve_repo_path, \
        validate_repo_name

    try:
        name = validate_repo_name(repo)
        get_repo_status(name)
    except (ValueError, FileNotFoundError, RuntimeError):
        raise ValueError("unknown repo")
    path = resolve_repo_path(name)
    if name == "remote-server" and not (path / ".git").exists():
        stack = Path(os.environ.get("BACKEND_STACK_DIR",
                                    "/home/ari/remote-server"))
        if (stack / ".git").exists():
            return name, str(stack)
    return name, str(path)


@router.get("/providers", response_model=ProviderList)
def agent_providers() -> dict:
    return {"providers": list(agent_pty.PROVIDERS)}


@router.post("/spawn", response_model=SpawnResult)
async def spawn_agent(body: SpawnRequest) -> dict:
    from backend.core import require_confirm

    require_confirm(body, SPAWN_INTENT)
    if body.provider not in agent_pty.PROVIDERS:
        raise HTTPException(status_code=400, detail="unknown provider")
    try:
        name, cwd = _repo_target(body.repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown repo")
    try:
        sid = await agent_pty.manager.spawn(body.provider, name, cwd)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except OSError:
        raise HTTPException(status_code=500, detail="spawn failed")
    return {"session_id": sid}


@router.post("/abort", response_model=AbortResult)
async def abort_agent(body: AbortRequest) -> dict:
    try:
        code = await agent_pty.manager.abort(body.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session")
    return {"ok": True, "exit": code}


@router.websocket("/ws/{session_id}")
async def agent_ws(websocket: WebSocket, session_id: str) -> None:
    if agent_pty.manager.get(session_id) is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    stop = asyncio.Event()

    async def _stdin_pump() -> None:
        try:
            while not stop.is_set():
                text = await websocket.receive_text()
                try:
                    await agent_pty.manager.write(session_id, text)
                except (KeyError, RuntimeError):
                    break
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()

    pump = asyncio.create_task(_stdin_pump())
    try:
        while not stop.is_set():
            try:
                chunk = await agent_pty.manager.read(
                    session_id, timeout=READ_POLL)
            except asyncio.TimeoutError:
                continue
            except KeyError:
                break
            if chunk is None:
                break
            try:
                await websocket.send_text(chunk)
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        stop.set()
        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        try:
            await agent_pty.manager.abort(session_id)
        except KeyError:
            pass
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass

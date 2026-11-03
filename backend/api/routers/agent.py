"""agent router: headless provider-CLI turns (issue #320).

- POST /api/agent/turn   one headless turn, SSE (token/done/error)
- POST /api/agent/abort  kill the in-flight turn for a session
- GET  /api/agent/providers  allowlisted providers

Turn framing (same shape as /api/chat/stream): ``event:
<name>\\ndata: <json>\\n\\n`` with token {token}, done {text,
session_id}, error {error}. Starting a conversation (no session_id)
is a hold-to-confirm destructive action ({intent: "agent-spawn",
confirmed: True}) because provider CLIs run tools; follow-up turns
and abort are free.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.api.routers.chat import SSE_HEADERS, sse_frame
from backend.exec import agent_turn

router = APIRouter(prefix="/api/agent", tags=["agent"])

SPAWN_INTENT = "agent-spawn"


class TurnRequest(BaseModel):
    provider: str = ""
    repo: str = "remote-server"
    session_id: str = ""
    prompt: str = ""
    intent: str = ""
    confirmed: bool = False


class AbortRequest(BaseModel):
    session_id: str = ""


class AbortResult(BaseModel):
    ok: bool = True
    killed: bool = False


class ProviderStatus(BaseModel):
    name: str = ""
    available: bool = False
    checked_at: int = 0
    detail: str = ""


class ProviderList(BaseModel):
    providers: list[str] = []
    status: list[ProviderStatus] = []


def _repo_target(repo: str) -> tuple[str, str]:
    """Validate a repo name; return (name, terminal-side cwd).

    Raises ValueError. The cwd feeds `compose exec --workdir` into
    the terminal container, so it must be the /srv bind-mount path
    even when the buttons-side resolver returns /home/ari paths.
    """
    from backend.core import get_repo_status, validate_repo_name

    try:
        name = validate_repo_name(repo)
        get_repo_status(name)
    except (ValueError, FileNotFoundError, RuntimeError):
        raise ValueError("unknown repo")
    return name, agent_turn.terminal_cwd(name)


@router.get("/providers", response_model=ProviderList)
async def agent_providers() -> dict:
    status = await agent_turn.provider_status(
        agent_turn.terminal_cwd("remote-server"))
    return {"providers": list(agent_turn.PROVIDERS),
            "status": status}


async def _turn_events(provider: str, repo: str, cwd: str,
                       session_id: str, prompt: str,
                       continued: bool):
    text: list[str] = []
    try:
        async for chunk in agent_turn.manager.run_turn(
                provider, repo, cwd, session_id, prompt, continued):
            text.append(chunk)
            yield sse_frame("token", {"token": chunk})
    except ValueError:
        yield sse_frame("error", {"error": "unknown provider"})
        return
    except OSError:
        yield sse_frame("error", {"error": "turn failed to start"})
        return
    result = agent_turn.manager.pop_result(session_id) or {}
    if result.get("timed_out"):
        yield sse_frame("error", {
            "error": "turn timed out after "
                     f"{agent_turn.TURN_TIMEOUT // 60}m"})
        return
    if result.get("truncated"):
        yield sse_frame("done", {"text": "".join(text),
                                 "session_id": session_id,
                                 "truncated": True})
        return
    if result.get("exit") != 0:
        detail = (result.get("stderr") or "").strip().split("\n")
        detail = detail[-1] if detail and detail[-1] else \
            f"exit {result.get('exit')}"
        yield sse_frame("error", {"error": detail[:500]})
        return
    yield sse_frame("done", {"text": "".join(text),
                             "session_id": session_id})


@router.post("/turn")
async def agent_turn_route(body: TurnRequest) -> StreamingResponse:
    from backend.core import require_confirm

    if body.provider not in agent_turn.PROVIDERS:
        raise HTTPException(status_code=400, detail="unknown provider")
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="empty prompt")
    continued = bool(body.session_id)
    if not continued:
        require_confirm(body, SPAWN_INTENT)
    try:
        name, cwd = _repo_target(body.repo)
    except ValueError:
        raise HTTPException(status_code=400, detail="unknown repo")
    sid = body.session_id or agent_turn.new_session_id()
    return StreamingResponse(
        _turn_events(body.provider, name, cwd, sid,
                     body.prompt.strip(), continued),
        media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/abort", response_model=AbortResult)
async def abort_agent(body: AbortRequest) -> dict:
    if not body.session_id:
        raise HTTPException(status_code=400,
                            detail="invalid session_id")
    killed = await agent_turn.manager.abort(body.session_id)
    return {"ok": True, "killed": killed}

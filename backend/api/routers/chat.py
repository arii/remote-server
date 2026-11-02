"""chat router: interactive chat (SSE) + ask/suggest background runs.

Ports of the buttons/server.py handlers, under the backend's /api
prefix convention (buttons serves /buttons/api/*; this backend serves
/api/* like its sibling routers):

- GET/POST /api/chat/stream  SSE stream of a chat session
- POST     /api/chat/abort   abort an active chat session
- POST     /api/ask          start an ask background run, return {id}
- POST     /api/suggest      start a suggest background run, return {id}
- GET      /api/pi-progress/{rid}  live tail of a background run

SSE framing is identical to buttons: one frame per event,
``event: <name>\\ndata: <json>\\n\\n``. Event names are the same:
``phase`` (prefill/tool_call/generating/done/aborted/error),
``token`` ({token}), ``done`` ({text, aborted, elapsed}).

Ask/suggest runs are reads-only background work (like briefing
reads): starting one is not a ledger entry, but chat start/finish/
abort are, exactly like the original.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.core import validate_repo_name
from backend.ledger import append as ledger_append

router = APIRouter(prefix="/api", tags=["chat"])

CHAT_MAX_PROMPT = 3000
ASK_MAX_CHARS = 2000
PI_PROGRESS_TAIL = 6000
RID_RE = re.compile(r"^[0-9a-f]{12}$")
TOOL_HINTS = ("read", "find", "grep", "search", "list", "check",
              "file", "code", "where", "log")

chat_sessions: dict = {}
chat_sessions_lock = threading.Lock()
pi_runs: dict = {}
pi_runs_lock = threading.Lock()


def sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def get_repo_info(repo: str) -> dict:
    """Validate a repo name and return its routing info.

    Raises ValueError("unknown repo") unless the name is a valid
    checkout basename. Tests monkeypatch this to avoid touching
    real checkouts.
    """
    name = validate_repo_name(repo)
    from backend.core import get_repo_status

    try:
        get_repo_status(name)
    except (ValueError, FileNotFoundError, RuntimeError):
        raise ValueError("unknown repo")
    return {"gh": f"arii/{name}", "td_cwd": f"/srv/repos/{name}"}


def spawn_chat_process(cmd: str):
    """Spawn the chat agent subprocess. Injectable for tests."""
    return subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def run_pi_pipeline(kind: str, repo: str, instruction: str | None,
                    log_path: str) -> dict:
    """Run the full ask/suggest pipeline; return the result dict.

    Default backend implementation: the buttons pipeline shells out
    to the terminal container (docker compose exec), a transport this
    backend does not own. Override this in deployment; tests
    monkeypatch it. The error path matches buttons (last line,
    capped at 500 chars).
    """
    raise RuntimeError("pi pipeline not configured")


def _pi_progress_path(rid: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"pi-progress-{rid}.log")


def _pi_start(kind: str, repo: str,
              instruction: str | None = None) -> tuple[str | None, str | None]:
    try:
        get_repo_info(repo)
    except ValueError:
        return None, "unknown repo"
    if kind == "ask":
        if (not isinstance(instruction, str) or not instruction.strip()
                or len(instruction) > ASK_MAX_CHARS):
            return None, "bad instruction"
        instruction = instruction.strip()
    elif kind != "suggest":
        return None, "unknown kind"
    rid = uuid.uuid4().hex[:12]
    with pi_runs_lock:
        now = time.time()
        for k in [k for k, v in pi_runs.items()
                  if v.get("done") and now - v.get("finished_at", now)
                  > 3600]:
            try:
                os.remove(_pi_progress_path(k))
            except Exception:
                pass
            del pi_runs[k]
        pi_runs[rid] = {"id": rid, "kind": kind, "repo": repo, "done": False,
                        "started_at": now, "finished_at": None,
                        "instruction": instruction or (
                            "Suggest next actions" if kind == "suggest"
                            else ""),
                        "result": None, "error": None}
    with open(_pi_progress_path(rid), "w"):
        pass

    def _thread():
        path = _pi_progress_path(rid)
        try:
            with open(path, "a") as f:
                f.write("Investigating with local agent…\n"
                        if kind == "ask"
                        else "Fetching briefing…\n")
            result = run_pi_pipeline(kind, repo, instruction, path)
            with pi_runs_lock:
                r = pi_runs.get(rid)
                if r is not None:
                    r["result"] = result
                    r["done"] = True
                    r["finished_at"] = time.time()
        except Exception as exc:
            with pi_runs_lock:
                r = pi_runs.get(rid)
                if r is not None:
                    r["error"] = str(exc)[:500]
                    r["done"] = True
                    r["finished_at"] = time.time()
            try:
                with open(path, "a") as f:
                    f.write(f"\nPi failed: {exc}\n")
            except Exception:
                pass

    threading.Thread(target=_thread, daemon=True).start()
    return rid, None


def _abort_session(session_id: str) -> tuple[bool, str | None]:
    with chat_sessions_lock:
        sess = chat_sessions.get(session_id)
        if not sess:
            return False, "session not found"
        sess["aborted"] = True
        proc = sess.get("proc")
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        return True, None


def _chat_events(prompt: str, repo: str, session_id: str):
    try:
        info = get_repo_info(repo)
    except ValueError:
        yield sse_frame("error", {"error": "unknown repo"})
        return
    if not isinstance(prompt, str) or not prompt.strip():
        yield sse_frame("error", {"error": "empty prompt"})
        return
    prompt = prompt.strip()[:CHAT_MAX_PROMPT]

    with chat_sessions_lock:
        chat_sessions[session_id] = {
            "id": session_id, "repo": repo, "prompt": prompt,
            "started_at": time.time(), "aborted": False, "proc": None,
        }
    ledger_append({"kind": "chat", "session_id": session_id, "repo": repo,
                   "prompt": prompt[:150], "event": "chat started"})
    start_time = time.time()
    yield sse_frame("phase", {"phase": "prefill",
                              "message": "Analyzing context and preparing request..."})
    b64 = base64.b64encode(prompt.encode()).decode()
    cmd = ("cd /srv/remote-server/deep-agent && "
           f".venv/bin/python ask_investigator.py {info['gh']} {b64}")
    if any(k in prompt.lower() for k in TOOL_HINTS):
        time.sleep(0.05)
        yield sse_frame("phase", {"phase": "tool_call",
                                  "message": "Inspecting repository context with tools..."})
    try:
        proc = spawn_chat_process(cmd)
        with chat_sessions_lock:
            if session_id in chat_sessions:
                chat_sessions[session_id]["proc"] = proc
                if chat_sessions[session_id]["aborted"]:
                    proc.terminate()
    except Exception as exc:
        yield sse_frame("phase", {"phase": "error", "error": str(exc)})
        yield sse_frame("error", {"error": str(exc)})
        with chat_sessions_lock:
            chat_sessions.pop(session_id, None)
        return
    yield sse_frame("phase", {"phase": "generating",
                              "message": "Streaming response from model..."})
    full_response: list[str] = []
    is_aborted = False
    code = 0
    try:
        if proc.stdout:
            for chunk in iter(lambda: proc.stdout.read(16), ""):
                with chat_sessions_lock:
                    sess = chat_sessions.get(session_id)
                    if sess and sess.get("aborted"):
                        is_aborted = True
                        break
                full_response.append(chunk)
                yield sse_frame("token", {"token": chunk})
        proc.wait()
        code = proc.returncode
    except Exception as exc:
        code = 1
        yield sse_frame("error", {"error": str(exc)})
    with chat_sessions_lock:
        sess = chat_sessions.pop(session_id, None)
        if sess and sess.get("aborted"):
            is_aborted = True
    elapsed = round(time.time() - start_time, 2)
    if is_aborted:
        yield sse_frame("phase", {"phase": "aborted",
                                  "message": "Generation aborted by user.",
                                  "elapsed": elapsed})
        yield sse_frame("done", {"text": "".join(full_response),
                                 "aborted": True, "elapsed": elapsed})
        ledger_append({"kind": "chat", "session_id": session_id,
                       "repo": repo, "event": "chat aborted",
                       "elapsed": elapsed})
    else:
        yield sse_frame("phase", {"phase": "done",
                                  "message": "Complete ✓",
                                  "elapsed": elapsed})
        yield sse_frame("done", {"text": "".join(full_response),
                                 "aborted": False, "elapsed": elapsed})
        ledger_append({"kind": "chat", "session_id": session_id,
                       "repo": repo, "event": "chat finished",
                       "exit": code, "elapsed": elapsed})


class ChatStreamRequest(BaseModel):
    prompt: str = ""
    repo: str = "remote-server"
    session_id: str = ""


class ChatAbortRequest(BaseModel):
    session_id: str = ""


class ChatAbortResult(BaseModel):
    ok: bool = True
    session_id: str = ""


class AskRequest(BaseModel):
    repo: str = "remote-server"
    instruction: str = ""


class SuggestRequest(BaseModel):
    repo: str = "remote-server"


class RunStarted(BaseModel):
    id: str = Field(default_factory=lambda: "")


class PiProgress(BaseModel):
    done: bool = False
    output: str = ""
    result: dict | None = None
    error: str | None = None


@router.get("/chat/stream")
def chat_stream_get(prompt: str = "", repo: str = "remote-server",
                    session_id: str = "") -> StreamingResponse:
    sid = session_id or uuid.uuid4().hex[:12]
    return StreamingResponse(_chat_events(prompt, repo, sid),
                             media_type="text/event-stream")


@router.post("/chat/stream")
def chat_stream_post(body: ChatStreamRequest) -> StreamingResponse:
    sid = body.session_id or uuid.uuid4().hex[:12]
    return StreamingResponse(_chat_events(body.prompt, body.repo, sid),
                             media_type="text/event-stream")


@router.post("/chat/abort", response_model=ChatAbortResult)
def chat_abort(body: ChatAbortRequest) -> dict:
    if not body.session_id or not isinstance(body.session_id, str):
        raise HTTPException(status_code=400, detail="invalid session_id")
    ok, err = _abort_session(body.session_id)
    if not ok:
        raise HTTPException(status_code=404, detail=err or "abort failed")
    ledger_append({"kind": "chat", "session_id": body.session_id,
                   "event": "chat aborted via api"})
    return {"ok": True, "session_id": body.session_id}


@router.post("/ask", response_model=RunStarted)
def ask(body: AskRequest) -> dict:
    rid, err = _pi_start("ask", body.repo, body.instruction)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"id": rid}


@router.post("/suggest", response_model=RunStarted)
def suggest(body: SuggestRequest) -> dict:
    rid, err = _pi_start("suggest", body.repo)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"id": rid}


@router.get("/pi-progress/{rid}", response_model=PiProgress)
def pi_progress(rid: str) -> dict:
    if not RID_RE.match(rid):
        raise HTTPException(status_code=404, detail="unknown run")
    with pi_runs_lock:
        r = pi_runs.get(rid)
    if r is None:
        raise HTTPException(status_code=404, detail="unknown run")
    try:
        with open(_pi_progress_path(rid)) as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - PI_PROGRESS_TAIL))
            tail = f.read()
    except Exception:
        tail = ""
    out: dict = {"done": r["done"], "output": tail}
    if r["done"]:
        if r["error"]:
            out["error"] = r["error"]
        elif r["result"]:
            out["result"] = r["result"]
    return out

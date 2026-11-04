"""chat router: interactive chat (SSE) + ask/suggest background runs.

Ports of the buttons/server.py handlers, under the backend's /api
prefix convention (buttons serves /buttons/api/*; this backend serves
/api/* like its sibling routers):

- GET/POST /api/chat/stream  SSE stream of a chat session
- GET      /api/chat/route-hint  pre-send backend + cost hint (ladder probe only)
- POST     /api/chat/abort   abort an active chat session
- POST     /api/chat/resume  decide a pending approval, return the turn result
- POST     /api/ask          start an ask background run, return {id}
- POST     /api/suggest      start a suggest background run, return {id}
- GET      /api/pi-progress/{rid}  live tail of a background run

SSE framing is identical to buttons: one frame per event,
``event: <name>\\ndata: <json>\\n\\n``. Event names are the same:
``phase`` (prefill/tool_call/generating/done/aborted/error),
``token`` ({token}), ``done`` ({text, aborted, elapsed}).
A turn that needs approval adds ``approval`` ({session_id,
action_requests, proposals}) and sets ``approval_pending`` on
``done``; the client decides via POST /api/chat/resume
({session_id, decision, message?}) -> 202 {resumed, text, exit,
approval?}. Errors: 400 bad shape, 404 unknown session,
409 no pending approval / turn in progress.

Chat turns spawn deep-agent/deep_chat.py (exit 0 done, 1 failed
with Sorry line on stdout, 2 server bug, 3 approval needed);
ask/suggest spawn deep-agent/ask_investigator.py and parse its
REPORT/FINAL/PROPOSE shapes.

Ask/suggest runs are reads-only background work (like briefing
reads): starting one is not a ledger entry, but chat start/finish/
abort are, exactly like the original.
"""

import base64
import json
import os
import re
import subprocess
import sys
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
pending_resumes: dict = {}
pending_resumes_lock = threading.Lock()

REPO_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

sys.path.insert(0, os.path.join(REPO_ROOT, "deep-agent"))

import model_router  # noqa: E402
import pricing  # noqa: E402


def deep_agent_dir() -> str:
    return os.environ.get("DEEP_AGENT_DIR",
                           os.path.join(REPO_ROOT, "deep-agent"))


def deep_agent_python() -> str:
    return os.environ.get(
        "DEEP_AGENT_VENV",
        os.path.join(deep_agent_dir(), ".venv", "bin", "python"))


def deep_agent_script(name: str) -> str:
    return os.path.join(deep_agent_dir(), name)


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


def spawn_chat_process(argv: list):
    """Spawn the chat agent subprocess. Injectable for tests.

    Stdout only: the agent contract keeps banners and trace paths
    on stderr, and merging them would leak machine lines into the
    streamed reply tokens.
    """
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )


def spawn_pipeline_process(argv: list):
    """Spawn the ask/suggest agent subprocess. Injectable for tests."""
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )


def validate_action_request(args: dict) -> tuple[bool, str]:
    """Validate one action_request via deep_chat --validate-proposal.

    Stdlib-only; runs under the server python. Injectable for tests.
    Returns (ok, proposal_text_or_reject_reason).
    """
    raw = base64.b64encode(json.dumps(args).encode()).decode()
    try:
        proc = subprocess.run(
            [sys.executable, deep_agent_script("deep_chat.py"),
             "--validate-proposal", raw],
            capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return False, f"REJECTED: validator failed: {exc}"
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, "REJECTED: validator output unreadable"
    return bool(out.get("ok")), str(out.get("text", ""))


def run_resume_spawn(gh: str, thread: str, verdict: str,
                     message_b64: str | None) -> tuple[int, str]:
    """Run deep_chat --resume synchronously. Injectable for tests."""
    argv = [deep_agent_python(), deep_agent_script("deep_chat.py"),
           gh, thread, "--resume", verdict]
    if message_b64:
        argv.append(message_b64)
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=600)
    return proc.returncode, proc.stdout


def _interrupt_payload(chunks: list[str]) -> dict | None:
    for line in reversed("".join(chunks).splitlines()):
        if line.startswith("INTERRUPT:"):
            try:
                return json.loads(line[len("INTERRUPT:"):])
            except ValueError:
                return None
    return None


def approval_requests(value) -> list[dict]:
    """Extract validatable action_request args from an interrupt value."""
    if isinstance(value, dict):
        reqs = value.get("action_requests")
        if isinstance(reqs, list):
            out = []
            for r in reqs:
                if not isinstance(r, dict):
                    continue
                args = r.get("args", r)
                if isinstance(args, dict):
                    out.append(args)
            return out
        if "kind" in value:
            return [value]
    return []


def strip_agent_markers(text: str) -> str:
    """Drop machine lines (INTERRUPT blobs, trace paths) from replies."""
    kept = [ln for ln in text.splitlines()
            if not ln.startswith("INTERRUPT:") and not ln.startswith("trace:")]
    return "\n".join(kept).strip()


def _parse_propose_block(lines: list[str]) -> dict | None:
    for i, line in enumerate(lines):
        if line.startswith("PROPOSE "):
            kind = line[len("PROPOSE "):].strip()
            body: dict = {"kind": kind}
            acc: list[str] = []
            for rest in lines[i + 1:]:
                if rest.strip() == "END PROPOSE":
                    break
                if rest.startswith("TITLE:"):
                    body["title"] = rest[len("TITLE:"):].strip()
                elif rest.startswith("COMMAND:"):
                    body["command"] = rest[len("COMMAND:"):].strip()
                elif rest.startswith("BODY:"):
                    tail = rest[len("BODY:"):].strip()
                    if tail:
                        acc.append(tail)
                else:
                    acc.append(rest)
            if acc:
                body["body"] = "\n".join(acc).strip()
            return body
    return None


def run_pi_pipeline(kind: str, repo: str, instruction: str | None,
                    log_path: str) -> dict:
    """Run ask/suggest via ask_investigator; return the result dict.

    Streams progress lines to log_path (the pi-progress tail) and
    parses the REPORT/FINAL/PROPOSE shapes. The error path matches
    buttons (last line, capped at 500 chars).
    """
    venv = deep_agent_python()
    script = deep_agent_script("ask_investigator.py")
    if not (os.path.isfile(venv) and os.path.isfile(script)):
        raise RuntimeError("agent runtime not installed")
    info = get_repo_info(repo)
    prompt = (instruction.strip() if kind == "ask" and instruction
              else "Suggest the next actions for this repo as td commands.")
    b64 = base64.b64encode(prompt.encode()).decode()
    proc = spawn_pipeline_process([venv, script, info["gh"], b64])
    report: list[str] = []
    in_report = False
    final_lines: list[str] = []
    last = ""
    assert proc.stdout is not None
    with open(log_path, "a") as log:
        for line in proc.stdout:
            last = line.rstrip("\n")
            log.write(line)
            log.flush()
            if last.startswith("REPORT"):
                in_report = True
                continue
            if last.startswith("FINAL:"):
                in_report = False
                final_lines.append(last[len("FINAL:"):].strip())
                continue
            if in_report:
                report.append(last.strip())
            elif final_lines:
                final_lines.append(last)
    code = proc.wait()
    if code != 0:
        raise RuntimeError((last or f"ask failed (exit {code})")[:500])
    return {"reply": "\n".join(final_lines).strip(),
            "report": [r for r in report if r],
            "proposal": _parse_propose_block(final_lines)}


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


def _approval_events(session_id: str, repo: str, info: dict,
                     chunks: list[str], elapsed: float):
    blob = _interrupt_payload(chunks)
    ivs = (blob.get("interrupts") or []) if blob else []
    value = ivs[0].get("value") if ivs else None
    requests = approval_requests(value)
    if not requests:
        yield sse_frame("phase", {"phase": "error",
                                  "error": "approval requested but no proposal found",
                                  "elapsed": elapsed})
        yield sse_frame("error",
                        {"error": "approval requested but no proposal found"})
        ledger_append({"kind": "chat", "session_id": session_id,
                       "repo": repo, "event": "chat failed",
                       "error": "approval requested but no proposal found",
                       "exit": 3, "elapsed": elapsed})
        return
    checked = [validate_action_request(a) for a in requests]
    if not all(ok for ok, _ in checked):
        reason = next(t for ok, t in checked if not ok)
        rej = base64.b64encode(b"invalid proposal").decode()
        try:
            run_resume_spawn(info["gh"], session_id, "reject", rej)
        except Exception:
            pass
        yield sse_frame("phase", {"phase": "error",
                                  "error": f"proposal rejected: {reason}",
                                  "elapsed": elapsed})
        yield sse_frame("error", {"error": f"proposal rejected: {reason}"})
        ledger_append({"kind": "chat", "session_id": session_id,
                       "repo": repo, "event": "chat auto-rejected",
                       "error": reason, "elapsed": elapsed})
        return
    proposals = [t for _, t in checked]
    with pending_resumes_lock:
        pending_resumes[session_id] = {
            "gh": info["gh"], "repo": repo, "value": value,
            "proposals": proposals, "pending": True, "busy": False,
        }
    yield sse_frame("approval", {"session_id": session_id, **value,
                                 "proposals": proposals})
    yield sse_frame("done", {"text": strip_agent_markers("".join(chunks)),
                             "aborted": False, "elapsed": elapsed,
                             "approval_pending": True})
    ledger_append({"kind": "chat", "session_id": session_id,
                   "repo": repo, "event": "chat approval requested",
                   "elapsed": elapsed})


def _resume_decision(session_id: str, decision: str,
                     message: str) -> tuple[dict | None, int, str]:
    """Validate + run one resume. Returns (body, http_status, error)."""
    with pending_resumes_lock:
        st = pending_resumes.get(session_id)
        if st is None:
            return None, 404, "unknown session"
        if st.get("busy"):
            return None, 409, "turn in progress"
        if not st.get("pending"):
            return None, 409, "no pending approval"
        st["busy"] = True
        gh = st["gh"]
    msg = message.strip() if decision == "reject" else ""
    if decision == "reject" and not msg:
        msg = "Declined by reviewer."
    msg_b64 = base64.b64encode(msg.encode()).decode() if msg else None
    try:
        code, out = run_resume_spawn(gh, session_id, decision, msg_b64)
    except Exception as exc:
        with pending_resumes_lock:
            pending_resumes[session_id]["busy"] = False
        return None, 500, f"resume failed: {exc}"
    if code == 2:
        with pending_resumes_lock:
            pending_resumes[session_id]["busy"] = False
        return None, 500, "agent runtime misconfigured (exit 2)"
    if code not in (0, 1, 3):
        with pending_resumes_lock:
            pending_resumes[session_id]["busy"] = False
        return None, 500, f"agent exited {code}"
    text = strip_agent_markers(out)
    if code == 1 and not text:
        # Blank exit-1: nothing to show (e.g. the approval was already
        # consumed) — a stale/duplicate resume, not a server failure.
        with pending_resumes_lock:
            st = pending_resumes[session_id]
            st["busy"] = False
            st["pending"] = False
        return None, 409, "no pending approval"
    body: dict = {"ok": True, "resumed": True, "session_id": session_id,
                  "text": text, "exit": code}
    with pending_resumes_lock:
        st = pending_resumes[session_id]
        st["busy"] = False
        if code == 3:
            blob = _interrupt_payload([out])
            ivs = (blob.get("interrupts") or []) if blob else []
            value = ivs[0].get("value") if ivs else None
            requests = approval_requests(value)
            if requests:
                checked = [validate_action_request(a) for a in requests]
                if all(ok for ok, _ in checked):
                    st["pending"] = True
                    st["value"] = value
                    st["proposals"] = [t for _, t in checked]
                    body["approval"] = {"session_id": session_id, **value,
                                        "proposals": st["proposals"]}
                    return body, 202, ""
            # Uninterpretable re-interrupt: auto-reject once (bounded)
            # so the thread never strands behind an unshowable card.
            rej = base64.b64encode(b"unreadable proposal").decode()
            try:
                code2, out2 = run_resume_spawn(gh, session_id,
                                               "reject", rej)
            except Exception:
                code2, out2 = 1, ""
            st["pending"] = False
            ledger_append({"kind": "chat", "session_id": session_id,
                           "repo": st["repo"],
                           "event": "chat auto-rejected",
                           "error": "follow-up approval unreadable"})
            if code2 == 3:
                blob2 = _interrupt_payload([out2])
                ivs2 = (blob2.get("interrupts") or []) if blob2 else []
                value2 = ivs2[0].get("value") if ivs2 else None
                requests2 = approval_requests(value2)
                if requests2:
                    checked2 = [validate_action_request(a)
                                for a in requests2]
                    if all(ok for ok, _ in checked2):
                        st["pending"] = True
                        st["value"] = value2
                        st["proposals"] = [t for _, t in checked2]
                        body["text"] = strip_agent_markers(out2)
                        body["exit"] = code2
                        body["approval"] = {"session_id": session_id,
                                            **value2,
                                            "proposals": st["proposals"]}
                        return body, 202, ""
            body["text"] = (strip_agent_markers(out2)
                            or "The agent asked for approval in a form I "
                               "can't display, so I rejected it. Ask again "
                               "if still needed.")
            body["exit"] = code2
            body.pop("approval", None)
            return body, 202, ""
        st["pending"] = False
    done_word = "approved" if decision == "approve" else "rejected"
    ledger_append({"kind": "chat", "session_id": session_id,
                   "repo": st["repo"], "event": f"chat {done_word}"})
    return body, 202, ""


# Composer backend picker: per-turn override for one stream. None
# means Auto — no flag, today's ladder exactly (env contract
# applies). local/cloud map to the router's strict prefers, the
# one-turn equivalents of OLLAMA_STRICT=1 / NEBIUS_ONLY=1.
CHAT_PREFER = {"auto": None, "local": "ollama", "cloud": "nebius"}


def _chat_events(prompt: str, repo: str, session_id: str,
                 prefer: str = "auto"):
    try:
        info = get_repo_info(repo)
    except ValueError:
        yield sse_frame("error", {"error": "unknown repo"})
        return
    if not isinstance(prompt, str) or not prompt.strip():
        yield sse_frame("error", {"error": "empty prompt"})
        return
    want = prefer.strip().lower() if isinstance(prefer, str) else "auto"
    if want not in CHAT_PREFER:
        yield sse_frame("error", {"error": "bad prefer"})
        return
    prompt = prompt.strip()[:CHAT_MAX_PROMPT]
    venv = deep_agent_python()
    script = deep_agent_script("deep_chat.py")
    if not (os.path.isfile(venv) and os.path.isfile(script)):
        yield sse_frame("error", {"error": "agent runtime not installed"})
        return

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
    argv = [venv, script, info["gh"], session_id, b64]
    if CHAT_PREFER[want]:
        argv.append(f"--prefer={CHAT_PREFER[want]}")
    if any(k in prompt.lower() for k in TOOL_HINTS):
        time.sleep(0.05)
        yield sse_frame("phase", {"phase": "tool_call",
                                  "message": "Inspecting repository context with tools..."})
    try:
        proc = spawn_chat_process(argv)
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
        ledger_append({"kind": "chat", "session_id": session_id,
                       "repo": repo, "event": "chat failed",
                       "error": str(exc)[:500]})
        return
    full_response: list[str] = []
    is_aborted = False
    code = 0
    try:
        yield sse_frame("phase", {"phase": "generating",
                                  "message": "Streaming response from model..."})
        if proc.stdout:
            for chunk in iter(lambda: proc.stdout.read(16), ""):
                with chat_sessions_lock:
                    sess = chat_sessions.get(session_id)
                    if sess and sess.get("aborted"):
                        is_aborted = True
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                full_response.append(chunk)
                yield sse_frame("token", {"token": chunk})
        proc.wait()
        code = proc.returncode
    except GeneratorExit:
        # Client disconnected mid-stream: GeneratorExit is not an
        # Exception, so without this the LLM child keeps running
        # detached (SSE zombie, #294). Kill it, drop the session.
        try:
            proc.terminate()
        except Exception:
            pass
        with chat_sessions_lock:
            chat_sessions.pop(session_id, None)
        raise
    except Exception as exc:
        try:
            proc.terminate()
        except Exception:
            pass
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
        return
    if code == 2:
        yield sse_frame("phase", {"phase": "error",
                                  "error": "agent runtime misconfigured (exit 2)",
                                  "elapsed": elapsed})
        yield sse_frame("error",
                        {"error": "agent runtime misconfigured (exit 2)"})
        ledger_append({"kind": "chat", "session_id": session_id,
                       "repo": repo, "event": "chat failed",
                       "error": "agent runtime misconfigured (exit 2)",
                       "exit": 2, "elapsed": elapsed})
        return
    if code == 3:
        yield from _approval_events(session_id, repo, info,
                                    full_response, elapsed)
        return
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
    prefer: str = "auto"


class RouteHint(BaseModel):
    ok: bool = True
    provider: str = ""
    model: str = ""
    metered: bool = False
    rate_in_per_1m: float | None = None
    rate_out_per_1m: float | None = None
    error: str = ""


class ChatAbortRequest(BaseModel):
    session_id: str = ""


class ChatAbortResult(BaseModel):
    ok: bool = True
    session_id: str = ""


class ChatResumeRequest(BaseModel):
    session_id: str = ""
    decision: str = ""
    message: str = ""


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


SSE_HEADERS = {
    # Canonical SSE hygiene: no proxy (oauth2-proxy, caches) may
    # buffer or cache the event body; each frame flushes downstream.
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@router.get("/chat/stream")
def chat_stream_get(prompt: str = "", repo: str = "remote-server",
                    session_id: str = "",
                    prefer: str = "auto") -> StreamingResponse:
    sid = session_id or uuid.uuid4().hex[:12]
    return StreamingResponse(_chat_events(prompt, repo, sid, prefer),
                             media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.post("/chat/stream")
def chat_stream_post(body: ChatStreamRequest) -> StreamingResponse:
    sid = body.session_id or uuid.uuid4().hex[:12]
    return StreamingResponse(_chat_events(body.prompt, body.repo, sid,
                                          body.prefer),
                             media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.get("/chat/route-hint", response_model=RouteHint)
def chat_route_hint(prefer: str = "auto") -> dict:
    """Pre-send backend + cost hint for the composer line.

    Resolves the same ladder the next turn would take (Ollama
    reachability probe only — never a model call) and pairs it
    with the pricing card rate. Failures degrade to ok:false with
    the router's reason; the composer shows it as signal, never a
    send block.
    """
    want = prefer.strip().lower() if isinstance(prefer, str) else "auto"
    if want not in CHAT_PREFER:
        raise HTTPException(status_code=400, detail="bad prefer")
    try:
        spec = model_router.resolve_llm(prefer=CHAT_PREFER[want])
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    label = f"{spec['provider']}:{spec['model']}"
    metered = not label.startswith(pricing.FREE_PREFIX)
    rate = pricing.rate_for(label) if metered else None
    return {
        "ok": True,
        "provider": spec["provider"],
        "model": spec["model"],
        "metered": metered,
        "rate_in_per_1m": rate[0] if rate else None,
        "rate_out_per_1m": rate[1] if rate else None,
        "error": "",
    }


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


@router.post("/chat/resume", status_code=202)
def chat_resume(body: ChatResumeRequest) -> dict:
    if not body.session_id or not isinstance(body.session_id, str):
        raise HTTPException(status_code=400, detail="invalid session_id")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="invalid decision")
    message = body.message if isinstance(body.message, str) else ""
    result, status, err = _resume_decision(body.session_id,
                                           body.decision, message)
    if result is None:
        raise HTTPException(status_code=status, detail=err)
    return result


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

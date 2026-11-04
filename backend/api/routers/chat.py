"""chat router: interactive chat (SSE).

Ports of the buttons/server.py handlers, under the backend's /api
prefix convention (buttons serves /buttons/api/*; this backend serves
/api/* like its sibling routers):

- GET/POST /api/chat/stream  SSE stream of a chat session
  (POST resolves @-mention file pins server-side, #653; GET
  leaves mentions literal so attached content never rides a URL)
- GET      /api/chat/route-hint  pre-send backend + cost hint (ladder probe only)
- POST     /api/chat/abort   abort an active chat session
- POST     /api/chat/resume  decide a pending approval, return the turn result
- GET      /api/pi-progress/{rid}  live tail of a background run
  (legacy: /api/ask + /api/suggest were removed in #548 so no
  run can start anymore; kept because the jobs view polls it
  as a fallback)

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
with Sorry line on stdout, 2 server bug, 3 approval needed).

Chat start/finish/abort are ledger entries, exactly like the
original.
"""

import base64
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core import get_checkout_path, validate_repo_name
from backend.ledger import append as ledger_append

router = APIRouter(prefix="/api", tags=["chat"])

CHAT_MAX_PROMPT = 3000
PI_PROGRESS_TAIL = 6000
# Bounded stderr tail kept per spawned agent child (#534), mirroring
# agent_turn.STDERR_TAIL. Crash tracebacks survive in server-side
# records; the tail never streams to clients (see #449).
SPAWN_STDERR_TAIL = 4000
SPAWN_STDERR_CHUNK = 65536
# A hung deep_chat.py must not hold an SSE stream forever: past this
# many seconds the turn is killed and the stream ends with an error
# event, mirroring agent_turn.TURN_TIMEOUT.
CHAT_TURN_TIMEOUT = 600
# Bounded wait after SIGTERM before SIGKILL (agent_turn.KILL_GRACE).
CHAT_KILL_GRACE = 5
# Abandoned approval cards must not leak forever: entries older than
# this are swept opportunistically, mirroring RESULT_TTL.
PENDING_RESUME_TTL = 3600
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

    Stdout streams to the client; stderr is piped (never merged)
    and drained by StderrTail into a bounded server-side tail, so
    crash tracebacks survive in records without leaking machine
    lines into the streamed reply tokens.
    """
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


class StderrTail:
    """Drain a child stderr pipe on a daemon thread; keep a tail.

    Started right after spawn while the caller consumes stdout, so
    a chatty child can never fill the pipe and stall the parent
    (the #439 fill-then-hang class). tail() joins the drainer and
    returns the last SPAWN_STDERR_TAIL chars ("" when the child
    exposes no stderr pipe, e.g. test fakes).
    """

    def __init__(self, proc, limit: int = SPAWN_STDERR_TAIL) -> None:
        self._limit = limit
        self._tail = ""
        self._lock = threading.Lock()
        err = getattr(proc, "stderr", None)
        if err is None:
            self._thread = None
        else:
            self._thread = threading.Thread(
                target=self._drain, args=(err,), daemon=True)
            self._thread.start()

    def _drain(self, err) -> None:
        try:
            while True:
                chunk = err.read(SPAWN_STDERR_CHUNK)
                if not chunk:
                    break
                with self._lock:
                    self._tail = (self._tail + chunk)[-self._limit:]
        except Exception:
            pass
        finally:
            try:
                err.close()
            except Exception:
                pass

    def tail(self, timeout: float | None = None) -> str:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            return self._tail


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


_MACHINE_LINE_PREFIXES = ("INTERRUPT:", "trace:")
_MACHINE_PREFIX_LEN = max(len(p) for p in _MACHINE_LINE_PREFIXES)


class _TokenScrubber:
    """Incremental strip_agent_markers for streamed tokens (#449).

    Drops machine lines (INTERRUPT blobs, trace paths) as 16-char
    stdout chunks arrive, so streamed tokens never flash protocol
    internals. Only the first _MACHINE_PREFIX_LEN chars of each
    line are held for the keep/drop decision; the rest of a kept
    line passes through untouched, so normal text still streams
    live. feed() returns the emittable text ("" when nothing is
    ready); flush() releases the trailing partial line under the
    same rule. Raw chunks must still be kept separately:
    _interrupt_payload parses interrupts from unstripped text
    (strip after parse).
    """

    def __init__(self) -> None:
        self._buf = ""
        self._at_line_start = True
        self._dropping = False

    @staticmethod
    def _is_machine(line: str) -> bool:
        return line.startswith(_MACHINE_LINE_PREFIXES)

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        for ch in chunk:
            if self._dropping:
                if ch == "\n":
                    self._dropping = False
                    self._at_line_start = True
                continue
            if not self._at_line_start:
                out.append(ch)
                if ch == "\n":
                    self._at_line_start = True
                continue
            self._buf += ch
            if ch == "\n":
                if not self._is_machine(self._buf):
                    out.append(self._buf)
                self._buf = ""
                continue
            if len(self._buf) >= _MACHINE_PREFIX_LEN:
                if self._is_machine(self._buf):
                    self._dropping = True
                else:
                    out.append(self._buf)
                    self._at_line_start = False
                self._buf = ""
        return "".join(out)

    def flush(self) -> str:
        tail, self._buf = self._buf, ""
        self._dropping = False
        self._at_line_start = True
        if tail and not self._is_machine(tail):
            return tail
        return ""


def _pi_progress_path(rid: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"pi-progress-{rid}.log")


def _stop_proc(proc) -> None:
    """Terminate, escalate to kill, reap bounded. Never raises.

    Sync mirror of agent_turn._stop_proc: SIGTERM, wait
    CHAT_KILL_GRACE, SIGKILL, wait again. Returns at once when the
    child already exited.
    """
    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=CHAT_KILL_GRACE)
    except Exception:
        pass
    try:
        alive = proc.poll() is None
    except Exception:
        alive = True
    if alive:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=CHAT_KILL_GRACE)
        except Exception:
            pass


def _sweep_pending_resumes(now: float | None = None) -> int:
    """Evict approval entries older than PENDING_RESUME_TTL.

    Busy entries (a resume call in flight) are spared so a slow
    decide never loses its row mid-spawn. Entries without a stamp
    predate the TTL and count as stale.
    """
    now = time.time() if now is None else now
    with pending_resumes_lock:
        stale = [sid for sid, st in pending_resumes.items()
                 if not st.get("busy")
                 and now - st.get("created_at", 0) > PENDING_RESUME_TTL]
        for sid in stale:
            pending_resumes.pop(sid, None)
    return len(stale)


def _abort_session(session_id: str) -> tuple[bool, str | None]:
    with chat_sessions_lock:
        sess = chat_sessions.get(session_id)
        if not sess:
            return False, "session not found"
        sess["aborted"] = True
        proc = sess.get("proc")
    if proc is not None:
        # Bounded wait + SIGKILL escalation outside the lock, so a
        # SIGTERM-ignoring child is still reaped instead of leaving
        # the abort a silent lie.
        _stop_proc(proc)
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
            "created_at": time.time(),
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
    _sweep_pending_resumes()
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
                    st["created_at"] = time.time()
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
                        st["created_at"] = time.time()
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


# @-mention file attach (#653): the composer inserts plain "@path"
# text; POST turns resolve it server-side into fenced content
# blocks ahead of the user text. GET turns leave mentions literal
# (per #459, attached content must never ride a query string).
MENTION_MAX_FILES = 3
MENTION_MAX_FILE_BYTES = 8192
MENTION_TAIL_MARKER = (
    f"\n… [truncated to fit {CHAT_MAX_PROMPT} chars: attachments first]")
MENTION_RE = re.compile(r"(?:^|(?<=[\s(\[{>\"']))@([A-Za-z0-9_.\-/]+)")
MENTION_STRIP_TRAILING = ".,;:!?)]}\"'"
MENTION_SKIPPED_LINE = ("[+{extra} more @-mention(s) skipped: "
                        "max {max_files} files per send]")


def parse_mentions(text: str) -> list[str]:
    """Ordered unique repo-relative @-mention paths in text (#653).

    A mention is "@" at the start or after whitespace/an opener,
    followed by path chars; trailing sentence punctuation is not
    part of the path, and a leading "/" reads repo-relative.
    Mid-word "@" (user@example.com) never matches.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in MENTION_RE.finditer(text or ""):
        rel = match.group(1).rstrip(MENTION_STRIP_TRAILING).lstrip("/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def read_mention_file(root, rel: str) -> tuple[str | None, str | None]:
    """Read one pinned file under a checkout root (#653).

    Returns (text, None) on success or (None, reason) with reason
    in {"invalid path", "not found", "not a file", "unreadable"}.
    Symlink escapes resolve outside the root and read invalid;
    content caps at MENTION_MAX_FILE_BYTES with a marker.
    """
    cleaned = rel.strip().lstrip("/")
    parts = cleaned.split("/")
    if (not cleaned or any(p in ("", ".", "..") for p in parts)
            or os.path.isabs(rel.strip())):
        return None, "invalid path"
    base = os.path.realpath(str(root))
    full = os.path.realpath(os.path.join(base, cleaned))
    if full != base and not full.startswith(base + os.sep):
        return None, "invalid path"
    if not os.path.lexists(full):
        return None, "not found"
    if not os.path.isfile(full):
        return None, "not a file"
    try:
        with open(full, "rb") as handle:
            data = handle.read(MENTION_MAX_FILE_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if b"\x00" in data:
        return None, "unreadable"
    if len(data) > MENTION_MAX_FILE_BYTES:
        try:
            size = os.path.getsize(full)
        except OSError:
            size = MENTION_MAX_FILE_BYTES + 1
        text = data[:MENTION_MAX_FILE_BYTES].decode("utf-8",
                                                   errors="replace")
        return (f"{text}\n… [truncated: showing first "
                f"{MENTION_MAX_FILE_BYTES} of {size} bytes]"), None
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "unreadable"


def inject_mentions(prompt: str, repo: str) -> str:
    """Prepend resolved @-mention blocks ahead of the prompt (#653).

    Budget rule: attachments eat the CHAT_MAX_PROMPT budget first;
    combined head + user text past the cap cuts from the tail (the
    user text in every realistic case) with MENTION_TAIL_MARKER.
    Missing/unreadable/escaping pins become visible "[@path:
    reason]" lines and the turn still sends. No mentions, or a repo
    without a readable checkout, returns the plain capped prompt.
    """
    mentions = parse_mentions(prompt)
    if not mentions:
        return prompt[:CHAT_MAX_PROMPT]
    try:
        root = get_checkout_path(repo)
    except (ValueError, FileNotFoundError):
        return prompt[:CHAT_MAX_PROMPT]
    blocks: list[str] = []
    for rel in mentions[:MENTION_MAX_FILES]:
        text, reason = read_mention_file(root, rel)
        if reason is None:
            blocks.append(f"--- @{rel} ---\n{text}")
        else:
            blocks.append(f"[@{rel}: {reason}]")
    extra = len(mentions) - MENTION_MAX_FILES
    if extra > 0:
        blocks.append(MENTION_SKIPPED_LINE.format(
            extra=extra, max_files=MENTION_MAX_FILES))
    combined = "\n\n".join(blocks) + "\n\n" + prompt
    if len(combined) <= CHAT_MAX_PROMPT:
        return combined
    return (combined[:CHAT_MAX_PROMPT - len(MENTION_TAIL_MARKER)]
            + MENTION_TAIL_MARKER)


def _chat_events(prompt: str, repo: str, session_id: str,
                 prefer: str = "auto",
                 resolve_mentions: bool = False):
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
    # POST turns pin @-mention files ahead of the user text (#653);
    # GET turns keep mentions literal (never resolved off a URL).
    # The ledger keeps the user's own words, not injected content.
    user_text = prompt.strip()[:CHAT_MAX_PROMPT]
    prompt = (inject_mentions(user_text, repo) if resolve_mentions
              else user_text)
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
    _sweep_pending_resumes()
    ledger_append({"kind": "chat", "session_id": session_id, "repo": repo,
                   "prompt": user_text[:150], "event": "chat started"})
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
    drained = StderrTail(proc)
    full_response: list[str] = []
    scrubber = _TokenScrubber()
    is_aborted = False
    timed_out = False
    code = 0
    err_tail = ""
    # A hung child must not hold the stream forever: stdout drains on
    # a daemon reader thread into a queue, and the loop takes each
    # chunk with the remaining turn budget (CHAT_TURN_TIMEOUT total,
    # mirroring agent_turn.TURN_TIMEOUT). On expiry the child is
    # killed and the stream ends with an error event.
    deadline = start_time + CHAT_TURN_TIMEOUT
    _EOF = object()
    read_q: queue.Queue = queue.Queue()

    def _read_stdout() -> None:
        try:
            if proc.stdout is None:
                return
            for chunk in iter(lambda: proc.stdout.read(16), ""):
                read_q.put(chunk)
        except Exception as exc:
            read_q.put(exc)
        finally:
            read_q.put(_EOF)

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()
    try:
        yield sse_frame("phase", {"phase": "generating",
                                  "message": "Streaming response from model..."})
        while True:
            with chat_sessions_lock:
                sess = chat_sessions.get(session_id)
                if sess and sess.get("aborted"):
                    is_aborted = True
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
            budget = deadline - time.time()
            if budget <= 0:
                timed_out = True
                break
            try:
                chunk = read_q.get(timeout=budget)
            except queue.Empty:
                timed_out = True
                break
            if chunk is _EOF:
                break
            if isinstance(chunk, Exception):
                raise chunk
            full_response.append(chunk)
            clean = scrubber.feed(chunk)
            if clean:
                yield sse_frame("token", {"token": clean})
        tail = scrubber.flush()
        if tail:
            yield sse_frame("token", {"token": tail})
        if timed_out or is_aborted:
            _stop_proc(proc)
        else:
            try:
                proc.wait(timeout=max(deadline - time.time(),
                                      CHAT_KILL_GRACE))
            except Exception:
                timed_out = True
                _stop_proc(proc)
        code = proc.returncode
        err_tail = drained.tail(timeout=CHAT_KILL_GRACE)
    except GeneratorExit:
        # Client disconnected mid-stream: GeneratorExit is not an
        # Exception, so without this the LLM child keeps running
        # detached (SSE zombie, #294). Kill it, drop the session.
        _stop_proc(proc)
        with chat_sessions_lock:
            chat_sessions.pop(session_id, None)
        raise
    except Exception as exc:
        _stop_proc(proc)
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
        yield sse_frame("done", {"text": strip_agent_markers(
            "".join(full_response)),
                                 "aborted": True, "elapsed": elapsed})
        ledger_append({"kind": "chat", "session_id": session_id,
                       "repo": repo, "event": "chat aborted",
                       "elapsed": elapsed})
        return
    if timed_out:
        msg = f"chat timed out after {CHAT_TURN_TIMEOUT}s"
        yield sse_frame("phase", {"phase": "error",
                                  "error": msg,
                                  "elapsed": elapsed})
        yield sse_frame("error", {"error": msg})
        record = {"kind": "chat", "session_id": session_id,
                  "repo": repo, "event": "chat failed",
                  "error": msg, "exit": code, "elapsed": elapsed}
        if err_tail:
            record["stderr"] = err_tail
        ledger_append(record)
        return
    if code == 2:
        yield sse_frame("phase", {"phase": "error",
                                  "error": "agent runtime misconfigured (exit 2)",
                                  "elapsed": elapsed})
        yield sse_frame("error",
                        {"error": "agent runtime misconfigured (exit 2)"})
        record = {"kind": "chat", "session_id": session_id,
                  "repo": repo, "event": "chat failed",
                  "error": "agent runtime misconfigured (exit 2)",
                  "exit": 2, "elapsed": elapsed}
        if err_tail:
            record["stderr"] = err_tail
        ledger_append(record)
        return
    if code == 1:
        # Exit-1 contract: deep_chat.py printed a Sorry line on stdout
        # (cli.sorry). Surface that human-readable text as the error,
        # never a Complete checkmark (#447). Empty stdout (e.g. an
        # unserializable interrupt blob) still fails, with a fallback.
        text = strip_agent_markers("".join(full_response))
        msg = text if text else "chat failed (exit 1)"
        yield sse_frame("phase", {"phase": "error",
                                  "error": msg,
                                  "elapsed": elapsed})
        yield sse_frame("error", {"error": msg})
        record = {"kind": "chat", "session_id": session_id,
                  "repo": repo, "event": "chat failed",
                  "error": msg,
                  "exit": 1, "elapsed": elapsed}
        if err_tail:
            record["stderr"] = err_tail
        ledger_append(record)
        return
    if code == 3:
        yield from _approval_events(session_id, repo, info,
                                    full_response, elapsed)
        return
    else:
        yield sse_frame("phase", {"phase": "done",
                                  "message": "Complete ✓",
                                  "elapsed": elapsed})
        yield sse_frame("done", {"text": strip_agent_markers(
            "".join(full_response)),
                                 "aborted": False, "elapsed": elapsed})
        record = {"kind": "chat", "session_id": session_id,
                  "repo": repo, "event": "chat finished",
                  "exit": code, "elapsed": elapsed}
        if code != 0 and err_tail:
            record["stderr"] = err_tail
        ledger_append(record)


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
                                          body.prefer,
                                          resolve_mentions=True),
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

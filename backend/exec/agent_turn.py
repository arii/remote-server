"""Headless provider-CLI turns (issue #320).

Runs one allowlisted provider CLI per turn in print mode and streams
its stdout back as text. Interactive TUIs over PTY were replaced:
they render raw ANSI with no terminal emulator and cannot take the
keystrokes TUIs need, so the drawer shows a chat transcript instead.

Provider argv (flags verified 2026-11-03 in the terminal container;
binaries and credentials live in terminal, buttons execs into it):
- muse  `muse exec --session-id <uuid> <prompt>` (stdout is the
  answer, logs go to stderr; stable --session-id continues)
- agy   `agy -p <prompt>` first turn, `agy --continue -p <prompt>`
  after (`--continue` resumes the most recent conversation)
Excluded: pi (hangs in `-p` with zero output), copilot/codex/gemini/
claude/aider/opencode (no binaries).

No other provider CLI flags are passed: everything above was probed
live, so there are no invented flags to go stale. The
docker/compose wrapper flags are ours.

Cutoff flip (issue #399): the drawer defaults to providers[0], and
ordered_providers() reads AGENT_DEFAULT_PROVIDER per request, so the
on-call flips muse->agy with env only (buttons redeploy, no code).
A muse-local fallback was ruled out: the local provider refuses
non-macOS platforms, verified by probe.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
import uuid

from backend.exec.engine import compose_argv, exec_argv
from backend.ledger import append as ledger_append

PROVIDERS = ("muse", "agy")

# Drawer default selector (cutoff flip): first entry wins. Read per
# request so a buttons redeploy with the env set takes effect.
DEFAULT_PROVIDER_ENV = "AGENT_DEFAULT_PROVIDER"


def ordered_providers() -> tuple:
    """Provider order for the drawer. Unknown/empty env keeps the
    default muse-first order (fail safe)."""
    first = os.environ.get(DEFAULT_PROVIDER_ENV, "muse").strip().lower()
    if first not in PROVIDERS:
        return PROVIDERS
    return (first,) + tuple(p for p in PROVIDERS if p != first)

READ_CHUNK = 65536
STDERR_TAIL = 4000

# A runaway CLI must not stream forever: past this many stdout chars
# the turn is killed and the done event carries truncated=true.
TURN_OUTPUT_MAX = 262144
STDERR_READ_MAX = 65536

# A hung CLI must not hold a turn forever either: past this many
# seconds the turn is killed and the stream ends with an error event.
# Observed turns finish in under a minute; ten minutes is headroom.
TURN_TIMEOUT = 600
KILL_GRACE = 5


async def _stop_proc(proc: subprocess.Popen) -> None:
    """Terminate, escalate to kill, wait bounded. Never raises and
    returns at once when the process already exited."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait),
                               KILL_GRACE)
    except (asyncio.TimeoutError, Exception):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(
                asyncio.to_thread(proc.wait), KILL_GRACE)
        except (asyncio.TimeoutError, Exception):
            pass


TERMINAL_REPOS = "/srv/repos"
TERMINAL_STACK = "/srv/remote-server"


def terminal_cwd(name: str) -> str:
    """Repo checkout path as seen inside the terminal container.

    Turns exec into terminal, whose bind-mounts live at /srv; the
    buttons-side resolver may return host-style /home/ari paths
    that do not exist there (same rule as chat.py td_cwd).
    """
    if name == "remote-server":
        return TERMINAL_STACK
    return f"{TERMINAL_REPOS}/{name}"


def turn_argv(provider: str, cwd: str, session_id: str, prompt: str,
               continued: bool) -> list:
    """Argv for one headless turn. Injectable seam: tests patch this
    to return a fake command so no real agent binary runs under test.

    Runs `docker compose exec -T terminal <provider> ...`: stdin is
    closed (DEVNULL) so a CLI that reads stdin fails fast instead of
    hanging the turn.
    """
    if provider == "muse":
        cli: list[str] = ["muse", "exec", "--session-id", session_id,
                          prompt]
    elif provider == "agy":
        cli = ["agy"] + (["--continue"] if continued else []) + \
            ["-p", prompt]
    else:
        raise ValueError("unknown provider")
    return compose_argv("exec", "-T", "--workdir", cwd, "terminal",
                        *cli)


def _first_line(text: str, limit: int) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def turn_decisions(provider: str, prompt: str, continued: bool,
                   code: int | None, started: float, tail: str,
                   err_tail: str, truncated: bool,
                   timed_out: bool) -> tuple[str, str | None]:
    """Decision block + failure reason for one agent-turn record.

    Returns (detail, error): detail is the what+why lines History
    expands; error is a human sentence, set only on failure so
    History summaries stay clean on success.
    """
    asked = _first_line(prompt, 150)
    session = "continued session" if continued else "fresh session"
    lines = [f"asked: {asked}" if asked else "asked: (empty prompt)",
             f"ran: {provider} turn ({session})"]
    elapsed = round(time.time() - started, 1)
    error = None
    if timed_out:
        lines.append(f"verdict: timed out after {TURN_TIMEOUT}s")
        lines.append(f"why: no turn may hold longer than {TURN_TIMEOUT}s")
        error = f"{provider} turn timed out after {TURN_TIMEOUT}s"
    elif code != 0:
        reason = _first_line(err_tail, 200) or "no stderr recorded"
        lines.append(f"verdict: exit {code} in {elapsed}s")
        lines.append(f"why: {reason}")
        error = f"{provider} turn failed (exit {code}): {reason}"
    else:
        said = _first_line(tail, 200)
        lines.append(f"verdict: exit 0 in {elapsed}s")
        lines.append(f"said: {said}" if said else "said: (no output)")
        if truncated:
            lines.append("note: output capped at "
                         f"{TURN_OUTPUT_MAX} chars")
    return "\n".join(lines), error


def new_session_id() -> str:
    """Mint a session id. Full UUID: `muse exec --session-id`
    rejects anything else (12-hex ids fail the turn with a CLI
    usage error). Continued turns reuse the minted id."""
    return str(uuid.uuid4())


# Availability probing (issue #397): the picker must show dead
# providers before the user fires. Only a real (trivial) turn proves
# API access — `muse usage` exits 0 even with no_allowance, and
# --version only proves install — so the probe fires one tiny turn
# per provider. Results cache briefly; each probe orphans one CLI
# session (fresh uuid / new agy conversation), so TTL stays long.
PROBE_TTL = 300
PROBE_TIMEOUT = 25
PROBE_PROMPT = "Reply with exactly: ok"

_probe_cache: dict = {}
_probe_lock = threading.Lock()


def clear_probe_cache() -> None:
    """Test reset for the availability cache."""
    with _probe_lock:
        _probe_cache.clear()


def _run_probe(argv: list, timeout: int):
    """Blocking probe run (executed off the event loop)."""
    return subprocess.run(argv, input=None, capture_output=True,
                          text=True, timeout=timeout)


async def probe_provider(provider: str, cwd: str) -> dict:
    """Probe one provider: binary present, then a trivial turn.

    Returns {name, available, checked_at, detail}. Unknown provider
    raises ValueError like turn_argv.
    """
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    checked_at = int(time.time())
    binary = exec_argv("command -v %s" % provider)
    try:
        found = await asyncio.to_thread(_run_probe, binary, 15)
    except Exception as exc:
        return {"name": provider, "available": False,
                "checked_at": checked_at,
                "detail": "binary check failed: %s" % exc}
    if found.returncode != 0:
        return {"name": provider, "available": False,
                "checked_at": checked_at, "detail": "binary missing"}
    argv = turn_argv(provider, cwd, new_session_id(), PROBE_PROMPT,
                     False)
    try:
        completed = await asyncio.to_thread(_run_probe, argv,
                                            PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"name": provider, "available": False,
                "checked_at": checked_at,
                "detail": "probe timed out"}
    except Exception as exc:
        return {"name": provider, "available": False,
                "checked_at": checked_at,
                "detail": "probe error: %s" % exc}
    if completed.returncode != 0:
        tail = (completed.stderr or "").strip().split("\n")
        detail = tail[-1][:200] if tail and tail[-1] else \
            "exit %s" % completed.returncode
        return {"name": provider, "available": False,
                "checked_at": checked_at, "detail": detail}
    return {"name": provider, "available": True,
            "checked_at": checked_at, "detail": "ok"}


async def provider_status(cwd: str) -> list:
    """Availability for every provider, cached PROBE_TTL seconds.

    Stale providers probe concurrently; the endpoint awaits them so
    the picker never renders a blind default.
    """
    now = time.time()
    with _probe_lock:
        fresh = {name: entry for name, (at, entry)
                 in _probe_cache.items()
                 if now - at < PROBE_TTL}
    todo = [p for p in PROVIDERS if p not in fresh]
    probed = await asyncio.gather(
        *(probe_provider(p, cwd) for p in todo))
    with _probe_lock:
        for entry in probed:
            _probe_cache[entry["name"]] = (time.time(), entry)
        return [_probe_cache[p][1] for p in PROVIDERS]


class AgentTurnManager:
    """Runs headless turns and aborts the in-flight one per session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, subprocess.Popen] = {}
        self._results: dict[str, dict] = {}

    def _register(self, session_id: str,
                  proc: subprocess.Popen) -> None:
        with self._lock:
            self._running[session_id] = proc

    def _unregister(self, session_id: str) -> None:
        with self._lock:
            self._running.pop(session_id, None)

    async def run_turn(self, provider: str, repo: str, cwd: str,
                       session_id: str, prompt: str, continued: bool,
                       argv: list | None = None):
        """Yield stdout text chunks for one turn. Afterwards
        pop_result(session_id) holds {exit, stderr, truncated,
        timed_out}. `argv` overrides the allowlist lookup (tests)."""
        cmd = list(argv) if argv is not None else turn_argv(
            provider, cwd, session_id, prompt, continued)
        started = time.time()
        proc = await asyncio.to_thread(
            subprocess.Popen, cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        self._register(session_id, proc)
        out_text: list[str] = []
        out_len = 0
        truncated = False
        timed_out = False
        err_tail = ""
        deadline = started + TURN_TIMEOUT
        try:
            assert proc.stdout is not None
            while True:
                budget = deadline - time.time()
                if budget <= 0:
                    timed_out = True
                    break
                try:
                    chunk = await asyncio.wait_for(
                        asyncio.to_thread(proc.stdout.read,
                                          READ_CHUNK),
                        budget)
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                if not chunk:
                    break
                room = TURN_OUTPUT_MAX - out_len
                out_len += len(chunk)
                if out_len > TURN_OUTPUT_MAX:
                    truncated = True
                    if room > 0:
                        out_text.append(chunk[:room])
                        yield chunk[:room]
                    break
                out_text.append(chunk)
                yield chunk
            await _stop_proc(proc)
            if proc.stderr is not None:
                try:
                    err = proc.stderr.read(STDERR_READ_MAX + 1)
                    err_tail = (err or "")[-STDERR_TAIL:]
                except Exception:
                    err_tail = ""
        finally:
            self._unregister(session_id)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
        code = proc.poll()
        tail = "".join(out_text)[-STDERR_TAIL:]
        detail, error = turn_decisions(
            provider, prompt, continued, code, started, tail,
            err_tail, truncated, timed_out)
        record = {"kind": "agent-turn", "session_id": session_id,
                  "provider": provider, "repo": repo,
                  "prompt": prompt[:150], "exit": code,
                  "duration_s": round(time.time() - started, 2),
                  "tail": tail, "stderr": err_tail,
                  "truncated": truncated, "timed_out": timed_out,
                  "detail": detail}
        if error is not None:
            record["error"] = error
        ledger_append(record)
        with self._lock:
            self._results[session_id] = {"exit": code,
                                         "stderr": err_tail,
                                         "truncated": truncated,
                                         "timed_out": timed_out}

    def pop_result(self, session_id: str) -> dict | None:
        """Completion record for a finished turn, once."""
        with self._lock:
            return self._results.pop(session_id, None)

    async def abort(self, session_id: str) -> bool:
        """Kill the in-flight turn for a session. Returns True when a
        process was killed, False when nothing was running."""
        with self._lock:
            proc = self._running.get(session_id)
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return True


manager = AgentTurnManager()

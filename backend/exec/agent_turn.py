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
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
import uuid

from backend.exec.engine import compose_argv
from backend.ledger import append as ledger_append

PROVIDERS = ("muse", "agy")

READ_CHUNK = 65536
STDERR_TAIL = 4000

# A runaway CLI must not stream forever: past this many stdout chars
# the turn is killed and the done event carries truncated=true.
TURN_OUTPUT_MAX = 262144
STDERR_READ_MAX = 65536


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


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


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
        pop_result(session_id) holds {exit, stderr, truncated}.
        `argv` overrides the allowlist lookup (tests)."""
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
        err_tail = ""
        try:
            assert proc.stdout is not None
            while True:
                chunk = await asyncio.to_thread(
                    proc.stdout.read, READ_CHUNK)
                if not chunk:
                    break
                room = TURN_OUTPUT_MAX - out_len
                out_len += len(chunk)
                if out_len > TURN_OUTPUT_MAX:
                    truncated = True
                    if room > 0:
                        out_text.append(chunk[:room])
                        yield chunk[:room]
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                out_text.append(chunk)
                yield chunk
            await asyncio.to_thread(proc.wait)
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
        ledger_append({"kind": "agent-turn", "session_id": session_id,
                       "provider": provider, "repo": repo,
                       "exit": code,
                       "duration_s": round(time.time() - started, 2),
                       "tail": "".join(out_text)[-STDERR_TAIL:],
                       "stderr": err_tail, "truncated": truncated})
        with self._lock:
            self._results[session_id] = {"exit": code,
                                         "stderr": err_tail,
                                         "truncated": truncated}

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

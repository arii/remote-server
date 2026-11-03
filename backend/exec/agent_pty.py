"""PTY-backed interactive agent runner (issue #320).

Spawns one allowlisted provider CLI per repo checkout in its own PTY,
streams stdout chunks to the caller, accepts stdin writes, and reaps
the child on abort or disconnect. The API is async; blocking PTY
reads run on one daemon thread per session.

Provider allowlist (binaries verified 2026-11-03 in the terminal
container; buttons holds no provider CLIs, so spawn execs into
terminal via compose under buttons' local PTY):
- muse  /root/.local/bin/muse  Muse Code 1.4.14,
  `muse [OPTIONS] [PROMPT]` (bare argv starts the interactive TUI)
- agy   /root/.local/bin/agy   1.4.12 (bare argv starts a session)
- pi    /usr/local/bin/pi      1.4.0,
  `pi [options] [--] [@files...] [messages...]` (bare argv is interactive)
Excluded: copilot/gh-copilot (no binary; the `gh copilot` extension is
not installed), codex, gemini, claude, aider, opencode (no binaries).

No provider CLI flags are passed: every provider starts its
interactive session with a bare argv, so there are no invented flags
to go stale. The docker/compose wrapper flags are ours.
"""

from __future__ import annotations

import asyncio
import os
import pty
import select
import signal
import subprocess
import threading
import time
import uuid

from backend.exec.engine import compose_argv
from backend.ledger import append as ledger_append

PROVIDERS = ("muse", "agy", "pi")

# Transcript ring per session; the ledger keeps only the tail.
TRANSCRIPT_MAX = 100_000
LEDGER_TAIL = 4000

# Finished sessions linger for re-attach reads, then spawn() prunes
# them so spawn-without-attach can't leak registry entries.
SESSION_TTL = 3600

READ_CHUNK = 65536


def provider_argv(provider: str, cwd: str) -> list:
    """Argv for an allowlisted provider. Injectable seam: tests patch
    this to return a fake command (e.g. ["/bin/cat"]) so no real
    agent binary runs under test.

    Runs `docker compose exec terminal <provider>` under the local
    PTY (stdin is a TTY there, so the daemon allocates the remote
    end): binaries and credentials live in terminal, not buttons.
    The preflight keeps the honest 500 (not installed) instead of a
    stillborn session.
    """
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    # `command` is a shell builtin: wrap in sh -c. provider is
    # allowlist-validated above, so no injection through the -c string.
    probe = subprocess.run(
        compose_argv("exec", "-T", "terminal", "sh", "-c",
                     f"command -v {provider}"),
        capture_output=True, text=True, timeout=30)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError("provider not installed")
    return compose_argv("exec", "--workdir", cwd, "terminal",
                        provider)


class AgentPtyManager:
    """Owns live agent PTY sessions: spawn, stdin, stdout, reap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict = {}

    async def spawn(self, provider: str, repo: str, cwd: str,
                    argv: list | None = None) -> str:
        """Spawn a provider CLI in a PTY rooted at cwd. Returns the
        session id. `argv` overrides the allowlist lookup (tests)."""
        cmd = list(argv) if argv is not None \
            else provider_argv(provider, cwd)
        now = time.time()
        with self._lock:
            for sid in [k for k, s in self._sessions.items()
                        if s["done"]
                        and now - s.get("finished_at", now) > SESSION_TTL]:
                del self._sessions[sid]
        master, slave = pty.openpty()
        try:
            # No cwd: it names the repo path inside terminal (the
            # exec --workdir), which need not exist in buttons; the
            # docker CLI runs anywhere. TERM falls back so remote
            # TUIs get a sane default.
            proc = subprocess.Popen(
                cmd, stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=os.setsid, close_fds=True,
                env={**os.environ, "TERM": os.environ.get("TERM")
                     or "xterm-256color"})
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        sid = uuid.uuid4().hex[:12]
        sess = {
            "id": sid, "provider": provider, "repo": repo,
            "cwd": cwd, "proc": proc, "master_fd": master,
            "transcript": "", "queue": asyncio.Queue(),
            "loop": asyncio.get_running_loop(),
            "lock": threading.Lock(), "reader": None,
            "done": False, "exit": None,
            "started_at": now, "finished_at": None,
        }
        reader = threading.Thread(
            target=self._reader_loop, args=(sess,), daemon=True)
        sess["reader"] = reader
        with self._lock:
            self._sessions[sid] = sess
        reader.start()
        return sid

    @staticmethod
    def _bind_loop(sess: dict) -> None:
        """Point delivery at the calling loop. Production runs one
        loop (a no-op); test portals run one loop per request, so the
        WS loop rebinds before its first read/write."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop is not sess["loop"] and not loop.is_closed():
            sess["loop"] = loop

    def get(self, session_id: str) -> dict | None:
        """Session info, or None when unknown."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            return {"session_id": sess["id"],
                    "provider": sess["provider"], "repo": sess["repo"],
                    "done": sess["done"], "exit": sess["exit"]}

    async def write(self, session_id: str, text: str) -> None:
        """Write client stdin to the session PTY. Raises KeyError when
        unknown, RuntimeError when the session is finished."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if sess["done"]:
            raise RuntimeError("session closed")
        self._bind_loop(sess)
        try:
            os.write(sess["master_fd"],
                     text.encode("utf-8", errors="replace"))
        except OSError:
            raise RuntimeError("session closed")

    async def read(self, session_id: str,
                   timeout: float | None = None) -> str | None:
        """Next stdout chunk, or None at EOF. Raises KeyError when
        unknown. A finished session with a drained queue returns None
        at once instead of hanging on a consumed sentinel."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if sess["done"] and sess["queue"].empty():
            return None
        self._bind_loop(sess)
        if timeout is None:
            return await sess["queue"].get()
        try:
            return await asyncio.wait_for(sess["queue"].get(),
                                          timeout)
        except asyncio.TimeoutError:
            raise

    async def abort(self, session_id: str) -> int:
        """Kill the child, reap the session, append the ledger entry.
        Raises KeyError when unknown. Returns the exit code."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        self._bind_loop(sess)
        proc = sess["proc"]
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        sess["reader"].join(timeout=10)
        code = self._finish(sess, proc.poll())
        with self._lock:
            self._sessions.pop(session_id, None)
        return code

    def _deliver(self, sess: dict, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace")
        with sess["lock"]:
            sess["transcript"] += text
            if len(sess["transcript"]) > TRANSCRIPT_MAX:
                sess["transcript"] = sess["transcript"][-TRANSCRIPT_MAX:]
        try:
            sess["loop"].call_soon_threadsafe(
                sess["queue"].put_nowait, text)
        except RuntimeError:
            pass  # loop closed; transcript still keeps the tail

    def _drain(self, sess: dict) -> None:
        """Non-blocking drain of output still buffered after exit."""
        fd = sess["master_fd"]
        try:
            os.set_blocking(fd, False)
        except OSError:
            return
        try:
            while True:
                try:
                    data = os.read(fd, READ_CHUNK)
                except (BlockingIOError, OSError):
                    return
                if not data:
                    return
                self._deliver(sess, data)
        finally:
            try:
                os.set_blocking(fd, True)
            except OSError:
                pass

    def _reader_loop(self, sess: dict) -> None:
        master = sess["master_fd"]
        proc = sess["proc"]
        while True:
            try:
                ready, _, _ = select.select([master], [], [], 0.25)
            except (OSError, ValueError):
                break
            if ready:
                try:
                    data = os.read(master, READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                self._deliver(sess, data)
            if proc.poll() is not None:
                self._drain(sess)
                break
        self._finish(sess, proc.poll())

    def _finish(self, sess: dict, exit_code: int | None) -> int:
        """Mark done, ledger once, wake readers. Idempotent: the reader
        thread and abort() race here and exactly one ledgers."""
        with sess["lock"]:
            if sess["done"]:
                return sess["exit"]
            sess["done"] = True
            sess["exit"] = exit_code if exit_code is not None else -1
            sess["finished_at"] = time.time()
            tail = sess["transcript"][-LEDGER_TAIL:]
        try:
            os.close(sess["master_fd"])
        except OSError:
            pass
        try:
            sess["loop"].call_soon_threadsafe(
                sess["queue"].put_nowait, None)
        except RuntimeError:
            pass
        ledger_append({"kind": "agent", "session_id": sess["id"],
                       "provider": sess["provider"], "repo": sess["repo"],
                       "exit": sess["exit"], "tail": tail})
        return sess["exit"]


manager = AgentPtyManager()

"""Headless provider-CLI turns (issue #320).

Runs one allowlisted provider CLI per turn in print mode and streams
its stdout back as text. Interactive TUIs over PTY were replaced:
they render raw ANSI with no terminal emulator and cannot take the
keystrokes TUIs need, so the drawer shows a chat transcript instead.

Provider argv (flags verified 2026-11-03/04 in the terminal container;
binaries and credentials live in terminal, buttons execs into it):
- muse  `muse exec --session-id <uuid> <prompt>` (stdout is the
  answer, logs go to stderr; stable --session-id continues)
- agy   `agy --output-format stream-json -p <prompt>` first turn,
  `agy --conversation <cid> --output-format stream-json -p <prompt>`
  after (each turn's init event reports its conversation_id, which
  the server pins per session; `--continue` is never used — it
  resumes the global most-recent conversation across sessions,
  issue #440)
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
import json
import os
import subprocess
import tempfile
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

# A stored turn result must not live forever: entries that no route
# ever pops (abandoned streams, issue #535) are swept past this age,
# mirroring the pi_runs 3600s eviction in chat.py. The pop-on-complete
# path runs milliseconds after the store, so the TTL never bites it.
RESULT_TTL = 3600

# Server-known session registry bound (issue #442): follow-ups may
# arrive long after the first turn, so entries never expire by age —
# the registry evicts oldest-first past this many ids instead.
KNOWN_SESSIONS_MAX = 10000


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


def _read_stderr_tail(err_file) -> str:
    """Last STDERR_TAIL chars of captured stderr. Reads from the
    end of the capture so a verbose CLI still shows its final
    error detail; small captures behave exactly as before."""
    try:
        err_file.seek(0, os.SEEK_END)
        size = err_file.tell()
        err_file.seek(max(0, size - (STDERR_READ_MAX + 1)))
        raw = err_file.read(STDERR_READ_MAX + 1)
    except Exception:
        return ""
    return raw.decode("utf-8", "replace")[-STDERR_TAIL:]


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
               continued: bool,
               conversation_id: str | None = None) -> list:
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
        # Session pinning (issue #440): every agy turn runs in
        # stream-json so its init event reports the conversation_id,
        # and follow-ups resume that id — never `--continue`, which
        # resumes the global most-recent conversation. A continued
        # turn with no recorded id (the first turn never reported
        # one) runs fresh: a new conversation, still no leak.
        cli = ["agy"]
        if continued and conversation_id:
            cli += ["--conversation", conversation_id]
        cli += ["--output-format", "stream-json", "-p", prompt]
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


# Availability probing (issues #397, #499): the picker must show dead
# providers before the user fires, but the signal has to be fast,
# free, and non-poisoning — so the probe checks binary presence
# only, never a live turn. A live turn per probe cost ~10s cold,
# spent model money, and orphaned an agy conversation. A
# binary-present CLI that fails still surfaces via the failure
# guidance card, which is the honest place for it. Results cache
# briefly to avoid exec spam.
PROBE_TTL = 300
PROBE_TIMEOUT = 15

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
    """Probe one provider by binary presence only — never a turn.

    Returns {name, available, checked_at, detail}. Unknown provider
    raises ValueError like turn_argv. The cwd arg is kept so callers
    don't change; presence doesn't depend on it.
    """
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    checked_at = int(time.time())
    binary = exec_argv("command -v %s" % provider)
    try:
        found = await asyncio.to_thread(_run_probe, binary,
                                        PROBE_TIMEOUT)
    except Exception as exc:
        return {"name": provider, "available": False,
                "checked_at": checked_at,
                "detail": "binary check failed: %s" % exc}
    if found.returncode != 0:
        return {"name": provider, "available": False,
                "checked_at": checked_at, "detail": "binary missing"}
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


class AgyStreamParser:
    """Incremental stream-json parser for agy turns (issue #440).

    Fed raw stdout chunks; returns answer-text pieces. Captures the
    turn's conversation_id from the init event (falling back to any
    event carrying one) so follow-ups pin this session's
    conversation. Answer text is the agent_response text_deltas,
    which concatenate to the result response (verified live); the
    result envelope itself is swallowed so it never double-yields.
    Lines that are not JSON pass through as text, so the argv test
    seam (/bin/echo) and unexpected CLI output keep streaming
    instead of vanishing.
    """

    def __init__(self) -> None:
        self._buf = ""
        self.conversation_id: str | None = None
        self._saw_answer = False

    def feed(self, chunk: str) -> list[str]:
        """Parse one stdout chunk into answer-text pieces."""
        self._buf += chunk
        lines = self._buf.split("\n")
        self._buf = lines.pop()
        pieces: list[str] = []
        for line in lines:
            pieces.extend(self._one(line, True))
        return pieces

    def flush(self) -> list[str]:
        """Parse the trailing unterminated line at EOF."""
        if not self._buf:
            return []
        line, self._buf = self._buf, ""
        return self._one(line, False)

    def _one(self, line: str, terminated: bool) -> list[str]:
        if not line.strip():
            return []
        try:
            event = json.loads(line)
        except ValueError:
            return self._passthrough(line, terminated)
        if not isinstance(event, dict):
            return self._passthrough(line, terminated)
        if self.conversation_id is None:
            self.conversation_id = self._event_cid(event)
        if event.get("event") == "step_update":
            update = event.get("step_update", {})
            if isinstance(update, dict) and \
                    update.get("step_type") == "agent_response":
                delta = update.get("text_delta", "")
                if isinstance(delta, str) and delta:
                    self._saw_answer = True
                    return [delta]
            return []
        if event.get("event") == "result":
            result = event.get("result", {})
            if isinstance(result, dict) and not self._saw_answer:
                response = result.get("response", "")
                if isinstance(response, str) and response:
                    self._saw_answer = True
                    return [response]
        return []

    @staticmethod
    def _event_cid(event: dict) -> str | None:
        """Conversation id carried by one event, if any."""
        for node in (event, event.get("step_update"),
                     event.get("result")):
            if isinstance(node, dict):
                cid = node.get("conversation_id")
                if isinstance(cid, str) and cid:
                    return cid
        return None

    def _passthrough(self, line: str, terminated: bool) -> list[str]:
        self._saw_answer = True
        return [line + "\n" if terminated else line]


class TurnSuperseded(Exception):
    """A newer turn claimed the session; this turn lost single-flight
    (issue #441) and must not store a result or pop another turn's."""


class AgentTurnManager:
    """Runs headless turns and aborts the in-flight one per session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, subprocess.Popen] = {}
        self._results: dict[str, dict] = {}
        # Single-flight generation per session (issue #441): every
        # run_turn bumps the counter under the lock, so at most one
        # turn owns the session. Older generations raise
        # TurnSuperseded instead of unregistering, storing, or
        # popping a newer turn's record.
        self._generation: dict[str, int] = {}
        # Sessions the server has run a turn for (issue #442). Only
        # these may skip the spawn confirm gate; a client-minted id
        # the server never issued must confirm like a new spawn.
        # Insertion-ordered dict used as a bounded FIFO set.
        self._known: dict[str, None] = {}
        # Agy conversation per session (issue #440): follow-ups
        # resume this id via --conversation, never the global
        # --continue. Bounded FIFO like _known; entries for live
        # sessions are re-recorded every turn.
        self._agy: dict[str, str] = {}
        self._result_at: dict[str, float] = {}

    def _register(self, session_id: str,
                  proc: subprocess.Popen) -> None:
        with self._lock:
            self._running[session_id] = proc

    def _unregister(self, session_id: str,
                    proc: subprocess.Popen) -> None:
        # Identity-guarded (issue #441): a superseded turn finishing
        # after its successor registered must not free the slot.
        with self._lock:
            if self._running.get(session_id) is proc:
                self._running.pop(session_id, None)

    def _is_current(self, session_id: str,
                    generation: int) -> bool:
        with self._lock:
            return self._generation.get(session_id) == generation

    def _remember(self, session_id: str) -> None:
        with self._lock:
            self._known[session_id] = None
            while len(self._known) > KNOWN_SESSIONS_MAX:
                self._known.pop(next(iter(self._known)))

    def is_known(self, session_id: str) -> bool:
        """True when the server ran a turn for this session id."""
        with self._lock:
            return session_id in self._known

    def agy_conversation(self, session_id: str) -> str | None:
        """Recorded agy conversation for a session, if any."""
        with self._lock:
            return self._agy.get(session_id)

    def _remember_agy(self, session_id: str, cid: str) -> None:
        with self._lock:
            self._agy[session_id] = cid
            while len(self._agy) > KNOWN_SESSIONS_MAX:
                self._agy.pop(next(iter(self._agy)))

    async def run_turn(self, provider: str, repo: str, cwd: str,
                       session_id: str, prompt: str, continued: bool,
                       argv: list | None = None):
        """Yield stdout text chunks for one turn. Afterwards
        pop_result(session_id) holds {exit, stderr, truncated,
        timed_out}. `argv` overrides the allowlist lookup (tests).

        Single-flight per session (issue #441): claiming the
        session aborts the registered prior turn, and the retired
        turn raises TurnSuperseded instead of storing — so
        pop_result never returns another turn's record."""
        conversation_id = (self.agy_conversation(session_id)
                           if provider == "agy" else None)
        cmd = list(argv) if argv is not None else turn_argv(
            provider, cwd, session_id, prompt, continued,
            conversation_id=conversation_id)
        started = time.time()
        # Stderr lands in a temp file, never a pipe (issue #439): a
        # pipe drained only after the turn deadlocks once the child
        # writes past the ~64KB buffer, burning the full
        # TURN_TIMEOUT. The file removes the pipe entirely, so
        # stderr can never stall the turn.
        err_file = tempfile.TemporaryFile(
            prefix="agent-turn-stderr-")
        try:
            proc = await asyncio.to_thread(
                subprocess.Popen, cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=err_file,
                text=True)
        except Exception:
            try:
                err_file.close()
            except Exception:
                pass
            raise
        # Claim after a successful spawn, so a turn that fails to
        # start leaves the prior turn untouched. The bump retires
        # the in-flight turn; its generator raises TurnSuperseded
        # instead of storing over this turn's result.
        with self._lock:
            prior = self._running.get(session_id)
            generation = self._generation.get(session_id, 0) + 1
            self._generation[session_id] = generation
        if prior is not None and prior.poll() is None:
            await _stop_proc(prior)
        if not self._is_current(session_id, generation):
            # A third turn claimed the session while this one was
            # stopping the prior: never register or stream — reap
            # the just-spawned process and report like the victim.
            await _stop_proc(proc)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
            try:
                err_file.close()
            except Exception:
                pass
            raise TurnSuperseded(session_id)
        self._register(session_id, proc)
        self._remember(session_id)
        out_text: list[str] = []
        out_len = 0
        truncated = False
        timed_out = False
        superseded = False
        err_tail = ""
        deadline = started + TURN_TIMEOUT
        # Agy turns stream NDJSON (issue #440); muse chunks pass
        # through untouched.
        parser = AgyStreamParser() if provider == "agy" else None
        try:
            assert proc.stdout is not None
            while True:
                if not self._is_current(session_id, generation):
                    superseded = True
                    break
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
                if parser is not None:
                    pieces = (parser.feed(chunk) if chunk
                              else parser.flush())
                else:
                    pieces = [chunk] if chunk else []
                for piece in pieces:
                    room = TURN_OUTPUT_MAX - out_len
                    out_len += len(piece)
                    if out_len > TURN_OUTPUT_MAX:
                        truncated = True
                        if room > 0:
                            out_text.append(piece[:room])
                            yield piece[:room]
                        break
                    out_text.append(piece)
                    yield piece
                if not chunk or truncated:
                    break
            if parser is not None and parser.conversation_id and \
                    self._is_current(session_id, generation):
                # Generation-guarded like the result store below: a
                # retired turn must not overwrite its successor's
                # conversation pin.
                self._remember_agy(session_id,
                                   parser.conversation_id)
            await _stop_proc(proc)
            err_tail = _read_stderr_tail(err_file)
        except GeneratorExit:
            # Drop-on-abandon (issue #535): a closed SSE stream
            # must not leave a result behind for this session.
            # Sync-only (no yield/await) then re-raise, so the
            # finally below still reaps the process and closes
            # the pipes. Generation-guarded (issue #441): a
            # superseded turn closed after its successor stored
            # must not drop the newer turn's result.
            with self._lock:
                if self._generation.get(session_id) == generation:
                    self._results.pop(session_id, None)
                    self._result_at.pop(session_id, None)
            raise
        finally:
            # Abandonment (GeneratorExit from a closed SSE stream, task
            # cancel) lands here too: the loop's _stop_proc runs only
            # on normal exit, so stop again — it returns at once when
            # the process already exited. Without this the CLI runs to
            # TURN_TIMEOUT with nobody reading (chat precedent: the
            # GeneratorExit kill, "SSE zombie, #294").
            self._unregister(session_id, proc)
            await _stop_proc(proc)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
            try:
                err_file.close()
            except Exception:
                pass
        if superseded or not self._is_current(session_id,
                                                 generation):
            # Retired while streaming (or while a successor claimed
            # during teardown): no ledger, no store — the owning
            # turn's record is authoritative, and raising here keeps
            # this stream from popping it.
            raise TurnSuperseded(session_id)
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
        # Sweep outside the store lock below: _sweep_results
        # takes the same (non-reentrant) lock itself.
        self._sweep_results()
        with self._lock:
            current = (self._generation.get(session_id)
                       == generation)
            if current:
                self._results[session_id] = {"exit": code,
                                             "stderr": err_tail,
                                             "truncated": truncated,
                                             "timed_out": timed_out}
                self._result_at[session_id] = time.time()
        if not current:
            # A successor claimed during the ledger write: the turn
            # ran, but its record must not overwrite the owner's.
            raise TurnSuperseded(session_id)

    def _sweep_results(self, now: float | None = None) -> None:
        """Drop result entries older than RESULT_TTL.

        Backstop for results no route ever pops (issue #535):
        the store site sweeps opportunistically, so _results
        stays bounded without a background thread.
        """
        at = time.time() if now is None else now
        with self._lock:
            stale = [sid for sid, ts in self._result_at.items()
                     if at - ts > RESULT_TTL]
            for sid in stale:
                self._results.pop(sid, None)
                self._result_at.pop(sid, None)

    def pop_result(self, session_id: str) -> dict | None:
        """Completion record for a finished turn, once."""
        with self._lock:
            self._result_at.pop(session_id, None)
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

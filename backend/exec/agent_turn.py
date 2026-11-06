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
# Real implementation tasks routinely exceed ten minutes (issue
# #838), so the budget is thirty minutes, overridable per deploy
# via AGENT_TURN_TIMEOUT (seconds) — buttons redeploy, no code,
# same env-only flip as AGENT_DEFAULT_PROVIDER.
TURN_TIMEOUT = 1800
TURN_TIMEOUT_ENV = "AGENT_TURN_TIMEOUT"
KILL_GRACE = 5


def turn_timeout() -> int:
    """Effective per-turn budget in seconds.

    The env override wins when it parses to a positive int;
    unset, blank, garbage, and non-positive values fall back to
    TURN_TIMEOUT. Tests patch TURN_TIMEOUT directly (env unset),
    so the fallback keeps that seam working.
    """
    raw = os.environ.get(TURN_TIMEOUT_ENV, "").strip()
    if not raw:
        return TURN_TIMEOUT
    try:
        value = int(raw)
    except ValueError:
        return TURN_TIMEOUT
    return value if value > 0 else TURN_TIMEOUT


# Seconds before the deadline the turn warns the drawer (issue
# #838): the stream yields a TurnWarning the router renders as a
# `warning` SSE event, so the user sees the end coming instead of
# a bare stall. No warning fires when the budget is shorter than
# the lead (patched-down test timeouts).
TURN_WARNING_LEAD = 120


class TurnWarning(str):
    """Zero-text stream marker for the pre-timeout warning.

    A str subclass so any consumer that joins chunks is
    unaffected (it contributes ""); the router checks isinstance
    and renders `warning` {seconds_left} instead of a token frame.
    """

    def __new__(cls, seconds_left: int):
        self = super().__new__(cls, "")
        self.seconds_left = seconds_left
        return self


class TurnProgress(str):
    """Zero-text stream marker for live progress (issue #1021).

    Tool calls and phase changes the drawer shows while the turn
    runs, so a live session is not a black box until the final
    text lands. A str subclass so any consumer that joins chunks
    is unaffected (it contributes ""); the router checks
    isinstance and renders `progress` {text} instead of a token
    frame.
    """

    def __new__(cls, text: str):
        self = super().__new__(cls, "")
        self.text = text
        return self


def tool_step_line(update: dict) -> str:
    """One live-progress line for a tool step, or "".

    Name plus the first scalar argument, capped — the drawer
    shows what the agent is doing, not full parameter dumps.
    """
    name = update.get("tool_name", "")
    info = update.get("tool_info")
    params = (info.get("parameters")
              if isinstance(info, dict) else None)
    arg = ""
    if isinstance(params, dict):
        for value in params.values():
            if isinstance(value, str) and value.strip():
                arg = value.strip().splitlines()[0][:100]
                break
    line = "tool %s" % name if name else "tool"
    return "%s %s" % (line, arg) if arg else line

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


# File-hygiene guardrail (issue #838): every provider prompt
# carries this, so a turn never leaves unrelated files (favicon
# rewrites, drive-by edits) modified or committed. run_turn
# applies it to the CLI argv only — the ledger keeps the raw user
# prompt, and the argv test seam bypasses it by construction.
FILE_HYGIENE_GUARDRAIL = (
    "Before finishing: run `git status --short` and revert every "
    "file this task did not require you to change — unrelated "
    "edits must never be left modified or committed."
)


def guard_prompt(prompt: str) -> str:
    """Append the file-hygiene guardrail to a provider prompt."""
    if not prompt:
        return FILE_HYGIENE_GUARDRAIL
    return "%s\n\n%s" % (prompt, FILE_HYGIENE_GUARDRAIL)


# Spec-task skill injection (issue #1034): ad-hoc provider turns run
# skill-less, so spec requests drift from the canonical template and
# fail td validate-issue downstream. When the prompt asks for a spec,
# append the canonical template; every other prompt passes through
# untouched, and a missing template file degrades to no-op.
SPEC_TASK_MARKERS = ("design spec", "spec issue", "write a spec",
                     "write_spec_issue", "spec template")
SPEC_SKILL_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "deep-agent", "skills", "write_spec_issue.md"))


def is_spec_task(prompt: str) -> bool:
    """Whether the prompt asks for a design spec."""
    lowered = (prompt or "").lower()
    return any(m in lowered for m in SPEC_TASK_MARKERS)


def spec_template() -> str:
    """Canonical spec template text, or '' when unreadable."""
    try:
        with open(SPEC_SKILL_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def spec_prompt(prompt: str) -> str:
    """Append the spec template to spec-task prompts only."""
    if not prompt or not is_spec_task(prompt):
        return prompt
    template = spec_template()
    if not template:
        return prompt
    return "%s\n\nFollow this spec template exactly:\n\n%s" % (
        prompt, template)


def _first_line(text: str, limit: int) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def turn_decisions(provider: str, prompt: str, continued: bool,
                   code: int | None, started: float, tail: str,
                   err_tail: str, truncated: bool,
                   timed_out: bool,
                   timeout: int | None = None) -> tuple[str, str | None]:
    """Decision block + failure reason for one agent-turn record.

    Returns (detail, error): detail is the what+why lines History
    expands; error is a human sentence, set only on failure so
    History summaries stay clean on success. `timeout` is the
    effective budget (env override); None reads TURN_TIMEOUT.
    """
    limit = TURN_TIMEOUT if timeout is None else timeout
    asked = _first_line(prompt, 150)
    session = "continued session" if continued else "fresh session"
    lines = [f"asked: {asked}" if asked else "asked: (empty prompt)",
             f"ran: {provider} turn ({session})"]
    elapsed = round(time.time() - started, 1)
    error = None
    if timed_out:
        lines.append(f"verdict: timed out after {limit}s")
        lines.append(f"why: no turn may hold longer than {limit}s")
        error = f"{provider} turn timed out after {limit}s"
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


# Bounded rescue (issue #838): when a turn times out, its partial
# work is committed + pushed in the SAME workdir before death —
# never left as uncommitted timeout debris. Best-effort and
# capped: the rescue never extends a dead turn past this.
RESCUE_TIMEOUT = 60
RESCUE_COMMIT_MESSAGE = "wip: partial agent turn (timed out)"


def _rescue_exec(cwd: str, args: list, timeout: float):
    """Run one git argv in the terminal-container workdir."""
    argv = compose_argv("exec", "-T", "--workdir", cwd, "terminal",
                       "git", *args)
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)


def rescue_partial_work(cwd: str, timeout: float = RESCUE_TIMEOUT,
                        _exec=None) -> dict:
    """Commit + push timeout debris. Never raises.

    Runs in the turn's own workdir: records the branch, commits a
    wip snapshot when the tree is dirty, pushes to that branch when
    it is a real (non-detached) branch. Returns {"branch",
    "commit", "committed", "pushed"} with Nones/False on every
    failure path. `timeout` caps the whole rescue; `_exec(args,
    timeout)` overrides the terminal-exec runner (tests).
    """
    record: dict = {"branch": None, "commit": None,
                    "committed": False, "pushed": False}
    run = _exec if _exec is not None else \
        (lambda args, budget: _rescue_exec(cwd, args, budget))
    deadline = time.monotonic() + timeout

    def budget() -> float | None:
        remaining = deadline - time.monotonic()
        return remaining if remaining > 0 else None

    try:
        remaining = budget()
        if remaining is None:
            return record
        branch = run(["rev-parse", "--abbrev-ref", "HEAD"],
                     remaining)
        if branch.returncode == 0:
            record["branch"] = branch.stdout.strip() or None
        remaining = budget()
        if remaining is None:
            return record
        status = run(["status", "--porcelain"], remaining)
        if status.returncode != 0:
            return record
        if not status.stdout.strip():
            remaining = budget()
            if remaining is None:
                return record
            head = run(["rev-parse", "HEAD"], remaining)
            if head.returncode == 0:
                record["commit"] = head.stdout.strip() or None
            return record
        remaining = budget()
        if remaining is None:
            return record
        if run(["add", "-A"], remaining).returncode != 0:
            return record
        remaining = budget()
        if remaining is None:
            return record
        if run(["commit", "-m", RESCUE_COMMIT_MESSAGE],
               remaining).returncode != 0:
            return record
        record["committed"] = True
        remaining = budget()
        if remaining is None:
            return record
        head = run(["rev-parse", "HEAD"], remaining)
        if head.returncode == 0:
            record["commit"] = head.stdout.strip() or None
        branch_name = record["branch"]
        if not branch_name or branch_name == "HEAD":
            return record
        remaining = budget()
        if remaining is None:
            return record
        push = run(["push", "origin", branch_name], remaining)
        record["pushed"] = push.returncode == 0
        return record
    except Exception:
        return record


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
            if isinstance(update, dict):
                if update.get("step_type") == "agent_response":
                    delta = update.get("text_delta", "")
                    if isinstance(delta, str) and delta:
                        self._saw_answer = True
                        return [delta]
                # Live progress (issue #1021): tool starts surface
                # as zero-text markers; completions stay silent so
                # one call reads as one line, not two.
                if update.get("step_type") == "tool" and \
                        update.get("state") == "ACTIVE":
                    line = tool_step_line(update)
                    if line:
                        return [TurnProgress(line)]
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
        # Live transcript buffer per session (issue #856): every
        # streamed piece lands here as well as on the wire, so a
        # reloaded console reattaches and resumes the turn's text
        # via session_snapshot(). Holds the current turn only — a
        # new claim resets it — bounded by TURN_OUTPUT_MAX like the
        # wire stream, and swept with results past RESULT_TTL.
        self._live_text: dict[str, list[str]] = {}
        self._live_len: dict[str, int] = {}
        self._live_at: dict[str, float] = {}
        # Durable finish record per session: the same verdict the
        # owning stream pops from _results, kept past the pop so a
        # reattach learns exit/truncated/timed_out after the fact.
        self._finished: dict[str, dict] = {}

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

    def is_running(self, session_id: str) -> bool:
        """True when a turn is currently in flight for this session."""
        with self._lock:
            proc = self._running.get(session_id)
            return proc is not None and proc.poll() is None

    def _record_live(self, session_id: str, generation: int,
                     text: str) -> None:
        # Generation-guarded like the result store: a retired turn
        # must not append to its successor's buffer.
        if not text:
            return
        with self._lock:
            if self._generation.get(session_id) != generation:
                return
            length = self._live_len.get(session_id, 0)
            room = TURN_OUTPUT_MAX - length
            if room <= 0:
                return
            self._live_text.setdefault(session_id, []).append(
                text[:room])
            self._live_len[session_id] = length + len(text[:room])
            self._live_at[session_id] = time.time()

    def note_guidance(self, session_id: str, code: str | None,
                      message: str, detail: str = "") -> None:
        """Attach the router's failure guidance to the finish record.

        _turn_events calls this when a turn fails with a known
        signature, so a reattached console renders the same
        guidance card the live stream would have shown.
        """
        with self._lock:
            record = self._finished.get(session_id)
            if record is not None:
                record["guidance_code"] = code
                record["guidance_message"] = message
                record["guidance_detail"] = detail

    def session_snapshot(self, session_id: str) -> dict:
        """Reattach state for one session (issue #856).

        Returns {session_id, known, running, transcript,
        transcript_truncated, finished}: transcript is the current
        turn's streamed text so far, finished is the durable
        verdict (exit/stderr/truncated/timed_out/timeout/rescue,
        plus guidance_* when the router attached it) or None while
        the turn is still in flight. Unknown ids report
        known=False with empty text and no verdict.
        """
        self._sweep_results()
        with self._lock:
            proc = self._running.get(session_id)
            finished = self._finished.get(session_id)
            return {
                "session_id": session_id,
                "known": session_id in self._known,
                "running": proc is not None and proc.poll() is None,
                "transcript": "".join(
                    self._live_text.get(session_id, [])),
                "transcript_truncated": self._live_len.get(
                    session_id, 0) >= TURN_OUTPUT_MAX,
                "finished": dict(finished) if finished is not None
                else None,
            }

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
        timed_out, timeout, rescue}. `argv` overrides the allowlist
        lookup (tests). The stream may also yield a TurnWarning
        marker once, TURN_WARNING_LEAD seconds before the deadline.

        Single-flight per session (issue #441): claiming the
        session aborts the registered prior turn, and the retired
        turn raises TurnSuperseded instead of storing — so
        pop_result never returns another turn's record."""
        conversation_id = (self.agy_conversation(session_id)
                           if provider == "agy" else None)
        send = guard_prompt(spec_prompt(prompt))
        cmd = list(argv) if argv is not None else turn_argv(
            provider, cwd, session_id, send, continued,
            conversation_id=conversation_id)
        started = time.time()
        timeout = turn_timeout()
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
            # Fresh turn, fresh live buffer (issue #856): the
            # transcript holds the current turn only, and the prior
            # turn's verdict no longer describes this session.
            self._live_text[session_id] = []
            self._live_len[session_id] = 0
            self._live_at[session_id] = time.time()
            self._finished.pop(session_id, None)
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
        rescue: dict | None = None
        deadline = started + timeout
        # Warning instant, or None when the budget is shorter than
        # the lead (patched-down test timeouts never warn).
        warn_at = (started + timeout - TURN_WARNING_LEAD
                   if timeout > TURN_WARNING_LEAD else None)
        warned = False
        # Agy turns stream NDJSON (issue #440); muse chunks pass
        # through untouched.
        parser = AgyStreamParser() if provider == "agy" else None
        try:
            assert proc.stdout is not None
            while True:
                if not self._is_current(session_id, generation):
                    superseded = True
                    break
                now = time.time()
                budget = deadline - now
                if budget <= 0:
                    timed_out = True
                    break
                if warn_at is not None and not warned \
                        and now >= warn_at:
                    warned = True
                    yield TurnWarning(
                        max(0, int(deadline - now)))
                    continue
                # Cap the read wait at the warning instant so a
                # silent turn still wakes in time to warn; the loop
                # top re-checks the deadline afterwards.
                wait = budget
                if warn_at is not None and not warned:
                    wait = min(budget,
                               max(0.001, warn_at - now))
                try:
                    chunk = await asyncio.wait_for(
                        asyncio.to_thread(proc.stdout.read,
                                          READ_CHUNK),
                        wait)
                except asyncio.TimeoutError:
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    continue
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
                            self._record_live(session_id, generation,
                                              piece[:room])
                            yield piece[:room]
                        break
                    out_text.append(piece)
                    self._record_live(session_id, generation, piece)
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
            if timed_out:
                # Bounded rescue (issue #838): the CLI is dead so
                # its files are stable — snapshot them now. Runs
                # only on timeout (never on abort, success, or
                # supersede) and never raises.
                try:
                    rescue = await asyncio.to_thread(
                        rescue_partial_work, cwd)
                except Exception:
                    rescue = None
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
            err_tail, truncated, timed_out, timeout=timeout)
        if timed_out and rescue is not None:
            detail += "\nsalvaged: branch=%s commit=%s pushed=%s" % (
                rescue.get("branch"), rescue.get("commit"),
                rescue.get("pushed"))
        record = {"kind": "agent-turn", "session_id": session_id,
                  "provider": provider, "repo": repo,
                  "prompt": prompt[:150], "exit": code,
                  "duration_s": round(time.time() - started, 2),
                  "tail": tail, "stderr": err_tail,
                  "truncated": truncated, "timed_out": timed_out,
                  "timeout": timeout, "rescue": rescue,
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
                                             "timed_out": timed_out,
                                             "timeout": timeout,
                                             "rescue": rescue}
                self._result_at[session_id] = time.time()
                # Durable verdict for reattach (issue #856): the
                # owning stream pops _results, but this copy stays
                # so a reloaded console still learns the outcome.
                self._finished[session_id] = {
                    "exit": code, "stderr": err_tail,
                    "truncated": truncated,
                    "timed_out": timed_out, "timeout": timeout,
                    "rescue": rescue}
                self._live_at[session_id] = time.time()
        if not current:
            # A successor claimed during the ledger write: the turn
            # ran, but its record must not overwrite the owner's.
            raise TurnSuperseded(session_id)

    def _sweep_results(self, now: float | None = None) -> None:
        """Drop result entries older than RESULT_TTL.

        Backstop for results no route ever pops (issue #535):
        the store site sweeps opportunistically, so _results
        stays bounded without a background thread. Live buffers
        and finish records (issue #856) sweep on the same TTL;
        running sessions are never swept.
        """
        at = time.time() if now is None else now
        with self._lock:
            stale = [sid for sid, ts in self._result_at.items()
                     if at - ts > RESULT_TTL]
            for sid in stale:
                self._results.pop(sid, None)
                self._result_at.pop(sid, None)
            stale_live = [sid for sid, ts in self._live_at.items()
                          if at - ts > RESULT_TTL
                          and sid not in self._running]
            for sid in stale_live:
                self._live_text.pop(sid, None)
                self._live_len.pop(sid, None)
                self._live_at.pop(sid, None)
                self._finished.pop(sid, None)

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

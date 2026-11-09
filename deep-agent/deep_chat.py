"""Multi-turn chat slice on create_deep_agent (spike).

Usage:
  deep_chat.py owner/repo <thread-id> <b64-prompt>
  deep_chat.py owner/repo <thread-id> --resume <approve|reject> [b64-message]
  deep_chat.py --validate-proposal <b64-json-proposal>

What this proves over a stateless one-shot on create_agent:

- create_deep_agent with a persistent SqliteSaver checkpointer keyed
  by thread_id, so conversation memory AND HITL pause state survive
  across one-shot processes (the server spawns per turn / per tap).
- 13 custom tools reusing the shared helpers (s5tools read-only
  file tools + retrieval RAG + one gated propose_action + one
  allowlisted run_repo_command for verify-after-reason + one keyless
  search_web for upstream discovery + one SSRF-guarded fetch_url for
  upstream docs + one search_turns past-turn recall + one pr_read +
  browser_snapshot/browser_act/browser_notes driving the shared
  cdp-stream session), plus 4 opt-in visual impact tools
  (run_ux_capture, diff_versions, ask_diff, lighthouse_read) via
  build_tools(..., impact_tools=True) (#849).
- interrupt_on={"propose_action": approve/reject} mirrors the buttons
  Approve/Reject taps; resume is Command(resume={"decisions": [...]}).

Stdout contract (turn/resume modes): PURE answer text, token chunks
printed as they stream, so the server's existing SSE chunk reader
works unchanged. On a HITL pause the last stdout line is exactly:

  INTERRUPT: {"thread_id": ..., "interrupts": [...]}

and the exit code is 3. Progress/diagnostics go to stderr. Every
invocation is ledgered.

Stdlib-only at import (like s5tools/model_router): langchain,
deepagents, and retrieval import lazily inside the functions that
need them, so system python can import the validators and run
--validate-proposal. Building/running the agent needs the venv.

Harness note: create_deep_agent also installs its built-in tools
(write_todos, ls/read_file/write_file/edit_file/glob/grep, task).
Those run on the default ephemeral StateBackend — scratch state that
never touches disk or the checkout. gate_chat_harness removes task
from the model (ungated subagent spend) and denies virtual-fs writes
(scratch the model could mistake for repo state); reads stay. The
only repo-write path is propose_action, and it only formats a
proposal; execution stays server-side behind the existing validated
endpoints.
"""

import base64
import json
import os
import re
import sys
import time

import cli

cli.bootstrap()

import s5tools  # noqa: E402
import model_router  # noqa: E402
import ledger  # noqa: E402
import observe  # noqa: E402
import runs  # noqa: E402
import tracing  # noqa: E402
import intent  # noqa: E402
import skills_registry  # noqa: E402
import decisions  # noqa: E402
import impact  # noqa: E402
import cobrowse_handoff  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
THREAD_DB = os.path.join(HERE, ".chat_threads", "chat.db")

THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PROMPT = 3000
# Trace middleware nodes consume budget (~4 steps per tool call,
# measured); 40 preserves room for ~9 tool calls per turn.
RECURSION_LIMIT = 40
# Tool-call budget per turn (ToolMessages in thread state). Past this
# the model sees only propose_action plus a nudge: looper models that
# would investigate to the recursion cutoff must propose-or-answer
# instead, and the cutoff becomes near-unreachable (#307). Legit
# repo turns use 1-3 tools; a browser errand needs notes + snapshot +
# several acts, so 8 splits the difference. Counts whole-thread
# history, so a post-approval continuation stays narrowed (it only
# needs to summarize).
CHAT_TOOL_BUDGET = 8
BUDGET_SPENT_NUDGE = (
    "Tool budget spent. Either call propose_action once with the "
    "exact change and stop, or answer from what you already gathered. "
    "No other tools are available."
)
# Sliding window over checkpoint history, applied at turn start.
# Checkpoints append indefinitely; without this long threads blow
# the context window. ~5-10 turns of history.
MAX_HISTORY_MESSAGES = 40
# Tool results carried into the salvage pass after a recursion cutoff.
SALVAGE_TOOL_TAIL = 12

# Propose-only maintainer sweep (roadmap item 7): read-only checks
# the agent can actually run, findings via propose_action (which
# always needs human approval). Scheduling is a deploy concern;
# this is the runnable mode.
SUPERVISOR_BRIEF = (
    "You are the repo maintainer on a scheduled sweep. Work read-only "
    "with list_dir/grep_repo/read_repo_file; track the sweep with "
    "write_todos. Checks: (1) TODO/FIXME/XXX/HACK markers — report "
    "file:line for each; (2) docs drift — docs/*.md paths that no "
    "longer exist in the checkout. Then either report all-clean or "
    "call propose_action(kind='issue') once per real finding (title + "
    "file:line evidence). Propose, never fix; end with a summary."
)
EXIT_NEEDS_APPROVAL = 3

# Single-sourced from backend/exec/engine.py ALLOWED_TD_COMMANDS
# (the truth): the hand mirror drifted — it accepted a shape the
# engine rejected, so an approved proposal could never run (#477).
# ALLOWED_TD is the engine's list itself, never a copy (same
# pattern as ask_investigator.py #521).
# engine.py is stdlib-only, so the stdlib-only import contract holds.
# Imported lazily in _td_res(): CLI validation paths (bad prompt/repo)
# must run with stdlib+siblings only, no repo root on sys.path.
_TD_RES_CACHE = None


def _td_res():
    """Compiled allowlist shapes, loaded on first proposal validation."""
    global _TD_RES_CACHE
    if _TD_RES_CACHE is None:
        from backend.exec.engine import ALLOWED_TD_COMMANDS as ALLOWED_TD

        _TD_RES_CACHE = [_td_shape_re(s) for s in ALLOWED_TD]
    return _TD_RES_CACHE


# The buttons confirm pattern: Approve tap -> approve, Decline tap ->
# reject. Batched mode (deepagents default): one interrupt per model
# turn, value is a HITLRequest, resumed with {"decisions": [...]}
# matched to action_requests by position.
INTERRUPT_ON = {
    "propose_action": {
        "allowed_decisions": ["approve", "reject"],
        "description": "Review the proposed write action before it runs.",
    }
}

# deepagents resolves harness profiles by the BUILT instance's
# (provider, identifier): ChatOllama -> ollama:<model>, ChatOpenAI
# (our Nebius path) -> openai:<model>. Translate our spec provider
# names; anything else passes through for custom model classes.
PROFILE_PROVIDERS = {"ollama": "ollama", "nebius": "openai"}


def gate_chat_harness(spec):
    """Exclude task + deny virtual-fs writes for one chat model.

    Venv-only. Registers an exact provider:model harness profile
    excluding the task tool (spawns an ungated general-purpose
    subagent: unbounded spend with no approval gate) and returns the
    permissions list denying virtual-fs writes (scratch the model
    could mistake for repo state). Reads stay allowed; our 5 real
    tools are untouched. (#306)
    """
    from deepagents import HarnessProfileConfig, register_harness_profile
    from deepagents.middleware.filesystem import FilesystemPermission

    provider = PROFILE_PROVIDERS.get(spec["provider"], spec["provider"])
    register_harness_profile(
        f"{provider}:{spec['model']}", HarnessProfileConfig(excluded_tools={"task"})
    )
    return [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]


SYSTEM = (
    "You are helping Ari from his phone. Be terse, no filler. "
    "Answer the question directly; when it is about the repo, "
    "investigate read-only first "
    "(list_dir, grep_repo, read_repo_file, search_docs; "
    "pr_read for GitHub PR questions). "
    "You cannot run, change, or deploy anything yourself — the only "
    "repo write path is the propose_action tool: call it once with the "
    "exact change and STOP; a human approves or rejects it. "
    "When the user asks about or directs the shared browser (the "
    "Buttons browser view / cdp-stream): browser_snapshot sees the "
    "exact live page and browser_notes recalls what worked there "
    "before — read notes first. browser_act navigates, clicks, and "
    "types on that shared page; say what you are about to do, then "
    "act, then report what the page shows. These drive ONLY the "
    "shared cdp-stream session, never anything else. While the human "
    "drives, act calls refuse — narrate instead of retrying. Login "
    "walls, payments, and sends: hand that step to the human in "
    "chat and stop driving until they hand back — never automate "
    "past them. When a handoff resolves, offer the lesson back as "
    "a browser_notes save once the human confirms it. "
    "Always invoke tools through the function-calling interface. "
    "Never write a tool call out as JSON text. "
    "If a grep pattern has no matches, retry with a shorter distinctive "
    "fragment. "
    "Keep replies readable on a phone: short paragraphs or a short "
    "list, no wide tables."
)

# Trivial-turn system prompt: no tools, no repo context, no history.
TRIVIAL_SYSTEM = (
    "You are helping Ari from his phone. Be terse, no "
    "filler. Answer briefly in one or two sentences."
)


def _short_err(exc, limit=160):
    """Cap exception detail for Sorry lines (the prefix is kept verbatim,
    so eval gates still classify the run as invalid, not failed)."""
    text = str(exc)
    return text if len(text) <= limit else text[:limit] + "…"


DECISION_TYPES = ("approve", "edit", "reject", "respond")


def valid_thread_id(tid):
    """Thread ids double as chat session ids (same charset as before)."""
    return isinstance(tid, str) and bool(THREAD_RE.match(tid))


def _td_shape_re(shape):
    parts = []
    for tok in shape.split():
        if tok == "N":
            parts.append(r"[1-9][0-9]*")
        else:
            parts.append(re.escape(tok))
    return re.compile(r"\s+".join(parts) + r"\Z")


BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
DISPATCH_TASK_LIMIT = 4000


def _valid_branch(branch):
    """git-plausible branch name: charset, length, no dot-dot."""
    return (
        isinstance(branch, str)
        and 1 <= len(branch) <= 120
        and bool(BRANCH_RE.match(branch))
        and ".." not in branch
        and branch[0] not in "/-."
    )


def _valid_pr_number(value):
    """Positive-int PR number (bools are not PRs)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


REVIEW_EVENTS = ("comment", "request-changes")


def validate_proposal(payload):
    """Validate a propose_action payload. Returns (ok, text).

    ok=True: text is the formatted PROPOSE block (the same shape the
    server already parses for Ask proposals). ok=False: text is a
    REJECTED reason. Never raises on odd inputs; never executes.
    """
    if not isinstance(payload, dict):
        return False, "REJECTED: proposal must be an object"
    kind = payload.get("kind")
    if kind == "issue":
        title = payload.get("title")
        body = payload.get("body")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            return False, "REJECTED: issue needs a title (1-120 chars)"
        if not isinstance(body, str) or not body.strip() or len(body) > 4000:
            return False, "REJECTED: issue needs a body (1-4000 chars)"
        return True, (
            f"PROPOSE issue\nTITLE: {title.strip()}\nBODY:\n{body.strip()}\nEND PROPOSE"
        )
    if kind == "td":
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 300:
            return False, "REJECTED: td needs a command (1-300 chars)"
        command = " ".join(command.split())
        for rx in _td_res():
            if rx.match(command):
                return True, (f"PROPOSE td\nCOMMAND: {command}\nEND PROPOSE")
        return False, (
            "REJECTED: td command is not an allowlisted shape "
            "(verbatim, changing only N to digits)"
        )
    if kind == "dispatch":
        repo = payload.get("repo")
        branch = payload.get("branch")
        task = payload.get("task")
        if not s5tools.valid_repo(repo):
            return False, "REJECTED: dispatch needs a repo (owner/name)"
        branch = branch.strip() if isinstance(branch, str) else branch
        if not _valid_branch(branch):
            return False, (
                "REJECTED: dispatch needs a branch "
                "(1-120 chars, [A-Za-z0-9._/-], no ..)"
            )
        if (
            not isinstance(task, str)
            or not task.strip()
            or len(task) > DISPATCH_TASK_LIMIT
        ):
            return False, ("REJECTED: dispatch needs a task (1-4000 chars)")
        return True, (
            f"PROPOSE dispatch\nREPO: {repo}\nBRANCH: {branch}\n"
            f"TASK:\n{task.strip()}\nEND PROPOSE"
        )
    if kind == "coder":
        repo = payload.get("repo")
        branch = payload.get("branch")
        task = payload.get("task")
        if not s5tools.valid_repo(repo):
            return False, "REJECTED: coder needs a repo (owner/name)"
        branch = branch.strip() if isinstance(branch, str) else branch
        if not _valid_branch(branch):
            return False, (
                "REJECTED: coder needs a branch (1-120 chars, [A-Za-z0-9._/-], no ..)"
            )
        if (
            not isinstance(task, str)
            or not task.strip()
            or len(task) > DISPATCH_TASK_LIMIT
        ):
            return False, ("REJECTED: coder needs a task (1-4000 chars)")
        return True, (
            f"PROPOSE coder\nREPO: {repo}\nBRANCH: {branch}\n"
            f"TASK:\n{task.strip()}\nEND PROPOSE"
        )
    if kind == "comment":
        repo = payload.get("repo")
        pr = payload.get("pr_number")
        body = payload.get("body")
        if not s5tools.valid_repo(repo):
            return False, "REJECTED: comment needs a repo (owner/name)"
        if not _valid_pr_number(pr):
            return False, ("REJECTED: comment needs a PR number (positive int)")
        if not isinstance(body, str) or not body.strip() or len(body) > 4000:
            return False, "REJECTED: comment needs a body (1-4000 chars)"
        return True, (
            f"PROPOSE comment\nREPO: {repo}\nPR: {pr}\n"
            f"BODY:\n{body.strip()}\nEND PROPOSE"
        )
    if kind == "review":
        repo = payload.get("repo")
        pr = payload.get("pr_number")
        event = payload.get("review_event")
        body = payload.get("body")
        if not s5tools.valid_repo(repo):
            return False, "REJECTED: review needs a repo (owner/name)"
        if not _valid_pr_number(pr):
            return False, ("REJECTED: review needs a PR number (positive int)")
        if event not in REVIEW_EVENTS:
            return False, (
                "REJECTED: review event must be 'comment' or 'request-changes'"
            )
        if not isinstance(body, str) or not body.strip() or len(body) > 4000:
            return False, "REJECTED: review needs a body (1-4000 chars)"
        return True, (
            f"PROPOSE review\nREPO: {repo}\nPR: {pr}\nEVENT: {event}\n"
            f"BODY:\n{body.strip()}\nEND PROPOSE"
        )
    return False, (
        "REJECTED: kind must be 'issue', 'td', 'dispatch', "
        "'coder', 'comment', or 'review'"
    )


def decision_for_verdict(verdict, message=""):
    """Map a buttons tap to one HITL decision. Raises ValueError."""
    if verdict == "approve":
        return {"type": "approve"}
    if verdict == "reject":
        note = (
            message
            if isinstance(message, str) and message.strip()
            else "Declined by reviewer."
        )
        return {"type": "reject", "message": note[:500]}
    raise ValueError(f"bad verdict {verdict!r}: want 'approve'/'reject'")


def approved_proposal_blocks(action_requests):
    """Validated PROPOSE/REJECTED texts for approved interrupt requests.

    Pure: the model decides what to SAY after approval, so the server
    cannot rely on the reply carrying anything machine-readable.
    This re-validates the approved args deterministically so _cmd_resume
    can print the executable blocks itself.
    """
    texts = []
    for req in action_requests or []:
        if not isinstance(req, dict):
            continue
        _ok, text = validate_proposal(req.get("args", {}))
        texts.append(text)
    return texts


def resume_payload(decisions):
    """Wrap per-request decisions for Command(resume=...)."""
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("decisions must be a non-empty list")
    for d in decisions:
        if not isinstance(d, dict) or d.get("type") not in DECISION_TYPES:
            raise ValueError(f"bad decision: {d!r}")
    return {"decisions": decisions}


def build_tools(checkout, *, impact_tools=False):
    """The custom tools. Venv-only (imports langchain + retrieval).

    Default is the 13 phone-chat tools (10 repo/web tools plus
    browser_snapshot, browser_act, browser_notes for the shared
    cdp-stream session); impact_tools=True adds the 4 visual-impact
    tools (run_ux_capture, diff_versions, ask_diff, lighthouse_read)
    for UI impact passes (#849). The default surface is pinned by
    tests/test_deep_chat.py::test_tool_surface.
    """
    from langchain_core.tools import tool  # noqa: E402

    import retrieval  # noqa: E402

    allowed = s5tools.allowed_commands_text()

    @tool
    def read_repo_file(path: str) -> str:
        """Read a file from the repo checkout (read-only).

        Args:
            path: Repo-relative path, e.g. buttons/server.py.
        """
        return s5tools.read_repo_file(checkout, path)

    @tool
    def grep_repo(pattern: str) -> str:
        """Search file contents in the repo checkout (read-only grep).

        If a long pattern has no matches, retry with a shorter
        distinctive fragment.

        Args:
            pattern: Literal search text.
        """
        return s5tools.grep_repo(checkout, pattern)

    @tool
    def list_dir(path: str = "") -> str:
        """List files in a repo checkout directory (read-only).

        Args:
            path: Repo-relative directory, '' for the checkout root.
        """
        return s5tools.list_dir(checkout, path or "")

    @tool
    def search_docs(query: str, repo: str = "") -> str:
        """Search S5 runbooks and docs for how-to and process answers.

        Args:
            query: Short question (2-10 words).
            repo: Optional owner/name (or bare checkout name) to
                search one repo; blank searches this checkout.
        """
        try:
            if repo:
                target = s5tools.checkout_for(retrieval.normalize_repo(repo))
            else:
                target = checkout
        except ValueError as exc:
            return f"doc search unavailable: {exc}"
        return retrieval.search_td(query, target, top_k=3)

    @tool
    def propose_action(
        kind: str,
        title: str = "",
        body: str = "",
        command: str = "",
        repo: str = "",
        branch: str = "",
        task: str = "",
        pr_number: int = 0,
        review_event: str = "",
    ) -> str:
        """Propose the ONE write action this turn needs (human-approved).

        Call at most once per turn, after read-only investigation, then
        STOP and wait for the human's approve/reject decision. Never
        executes anything: on approval this only returns the validated
        proposal text for the server to run.

        Args:
            kind: 'issue' (file a GitHub issue), 'td' (run one
                allowlisted td command), 'dispatch' (implement via
                a BoomTick worktree session), 'coder' (implement via
                a detached coder executor on a managed worktree),
                'comment' (post a PR comment), or 'review' (submit a
                PR review: comment or request-changes only, never
                approve).
            title: Issue title (kind='issue').
            body: Issue body markdown (kind='issue'), PR comment
                markdown (kind='comment'), or review body
                (kind='review').
            command: Exact td command (kind='td'; allowlisted shapes
                only, changing N to digits).
            repo: Target repo owner/name (kind='dispatch', 'coder',
                'comment', 'review').
            branch: Base branch to work from (kind='dispatch',
                'coder').
            task: Implementation brief (kind='dispatch', 'coder').
            pr_number: Target PR number, positive int
                (kind='comment', 'review').
            review_event: 'comment' or 'request-changes'
                (kind='review').
        """
        ok, text = validate_proposal(
            {
                "kind": kind,
                "title": title,
                "body": body,
                "command": command,
                "repo": repo,
                "branch": branch,
                "task": task,
                "pr_number": pr_number,
                "review_event": review_event,
            }
        )
        return text

    @tool
    def run_repo_command(command: str) -> str:
        """Run an allowlisted verification command in the repo checkout.

        Use AFTER reasoning to verify a claim — run the failing test,
        check git state — not to explore; read and grep first. Never
        executes anything else: anything outside the allowlist comes
        back as REJECTED text. Read-only in effect (tests may write
        caches); source edits still go through propose_action.

        Args:
            command: Full command line; must start with one of:
                {allowed}.
        """
        return s5tools.run_repo_command(checkout, command)

    run_repo_command.description = run_repo_command.description.format(allowed=allowed)

    @tool
    def fetch_url(url: str) -> str:
        """Fetch a URL's text: upstream docs, API references, changelogs.

        Use when you have a URL in hand (never guess deep links;
        use search_web to discover one). Returns decoded text capped
        at 256KB with a truncation marker; private/loopback/metadata
        addresses, non-http(s) schemes, and failures come back as
        FETCH_ERROR text. No search, no JS rendering, no auth, no POST.

        Args:
            url: Full http(s) URL to GET.
        """
        return s5tools.fetch_url(url)

    @tool
    def search_web(query: str, limit: int = 5) -> str:
        """Search the web for URLs about a topic (keyless discovery).

        Use when you need upstream docs, changelogs, or references
        you don't already have a URL for. Returns ranked
        title/URL/snippet hits; then call fetch_url on a hit to read
        it. Failures come back as SEARCH_ERROR text, no matches as
        "no results".

        Args:
            query: Search keywords, e.g. "ollama changelog".
            limit: Max hits, 1-10 (default 5).
        """
        return s5tools.search_web(query, limit=limit)

    @tool
    def search_turns(query: str, kind: str = "", limit: int = 5) -> str:
        """Search past agent turns for prior outcomes (read-only recall).

        Use when the user asks about earlier work: how something was
        fixed before, what triage concluded, whether this failure was
        seen already — or what the browser did before ("what did we
        do on linkedin"). Keyword match over each turn's script,
        model, thread, prompt, final answer, and tool names, newest
        first, capped lines.

        Args:
            query: Keywords from the earlier outcome, e.g. "triage verdict".
            kind: Optional row-kind filter, e.g. "deep-agent-run".
            limit: Max hits, 1-5 (default 5).
        """
        return runs.search_turns(query, kind=kind, limit=limit)

    @tool
    def pr_read(number: str, repo: str = "") -> str:
        """Read a GitHub PR: title, author, state, reviews, checks.

        Use for open-PR questions — private repos are invisible to
        keyless search_web and td PR reads need a project_config.json
        the checkout may not carry. Read-only; never comments or edits.

        Args:
            number: PR number as digits, e.g. "9826".
            repo: Optional owner/name (or bare checkout name); blank
                reads the turn's repo.
        """
        return s5tools.read_pr(number, repo or s5tools.repo_for_checkout(checkout))

    @tool
    def browser_snapshot() -> str:
        """See the shared cdp-stream browser page (read-only).

        Use when the user asks about the browser view or before
        driving it: returns the live page URL, title, and up to 100
        click targets with coordinates. Attaches to the live shared
        session; browser-busy means no viewer session exists. No
        pixels — the human watches the stream, this text is enough.

        Takes no arguments.
        """
        return cobrowse_handoff.browser_snapshot_impl()

    @tool
    def browser_act(
        op: str,
        url: str = "",
        x: int = 0,
        y: int = 0,
        text: str = "",
        key: str = "",
        code: str = "",
        dx: int = 0,
        dy: int = 0,
    ) -> str:
        """Drive the shared cdp-stream page (announce-then-act).

        Say what you are about to do in chat, then call once per
        step, then report what the page shows. Refuses while the
        human drives (PAUSED) — narrate instead of retrying. Never
        automates past login walls, payments, or sends: hand those
        to the human in chat.

        Args:
            op: 'navigate' (http(s) url required), 'click' (x, y
                from browser_snapshot targets), 'type' (text into
                the focused field), 'key' (key + code, e.g. Enter),
                'wheel' (scroll at x, y by dx, dy).
            url: Full http(s) URL for navigate.
            x: Target center-x for click/wheel.
            y: Target center-y for click/wheel.
            text: Text to type into the focused field.
            key: Key name for key, e.g. 'Enter'.
            code: Physical code, e.g. 'Enter' (defaults to key).
            dx: Horizontal scroll amount for wheel.
            dy: Vertical scroll amount for wheel.
        """
        return cobrowse_handoff.browser_drive_impl(
            op, url=url, x=x, y=y, dx=dx, dy=dy, text=text, key=key, code=code
        )

    @tool
    def browser_notes(site: str = "", note: str = "") -> str:
        """Per-site browser memory: read before driving, save lessons.

        Read first (blank site lists everything): what worked here
        before — login quirks, selectors that land, pages that fight
        automation. Save after the human confirms a lesson (handoff
        outcomes are the main source): one or two sentences.

        Args:
            site: Page host or purpose, e.g. 'linkedin'; blank to
                list all notes.
            note: Lesson text to save; blank to read.
        """
        return cobrowse_handoff.browser_notes_impl(site, note)

    base = [
        read_repo_file,
        grep_repo,
        list_dir,
        search_docs,
        propose_action,
        run_repo_command,
        fetch_url,
        search_web,
        search_turns,
        pr_read,
        browser_snapshot,
        browser_act,
        browser_notes,
    ]
    if not impact_tools:
        return base

    @tool
    def run_ux_capture(
        route: str = "",
        all_routes: bool = False,
        desktop: bool = False,
        mobile: bool = False,
        screenshots_only: bool = False,
    ) -> str:
        """Capture the checkout's UI via td ux (Playwright).

        Runs in the repo checkout (which must carry scripts/ux-*.ts;
        baseUrl comes from its ux-audit.config.json) and writes
        artifacts/ux-audit. Slow (minutes over many viewports):
        capture base, snapshot, deploy, capture head, then diff.

        Args:
            route: Single route to capture, e.g. "/hrm".
            all_routes: Capture every discovered route.
            desktop: Desktop viewports only.
            mobile: Mobile viewports only.
            screenshots_only: Screenshots without the audit checks.
        """
        try:
            result = impact.run_ux_capture(
                checkout,
                route=route or "",
                all_routes=all_routes,
                desktop=desktop,
                mobile=mobile,
                screenshots_only=screenshots_only,
            )
        except ValueError as exc:
            return f"REJECTED: {exc}"
        if not result.get("ok"):
            return f"CAPTURE_ERROR: {result.get('error', 'unknown')}"
        rows = result.get("results", [])
        done = sum(
            1 for r in rows if isinstance(r, dict) and r.get("status") == "success"
        )
        return (
            f"captured {done}/{len(rows)} route(s) to "
            f"{impact.UX_DIR} (snapshot before the next run)"
        )

    @tool
    def diff_versions(before: str, after: str) -> str:
        """Diff two ux captures into severity-ranked findings.

        Pure JSON + screenshot comparison, no model: compares
        DOM-side audit JSON (overflow, axe violations, images, tap
        targets, above-the-fold) and screenshot pairs (changed
        regions) plus lighthouse drops.

        Args:
            before: Base capture dir (checkout-relative or tmp).
            after: Head capture dir (checkout-relative or tmp).
        """
        base_dir = impact.resolve_capture_dir(checkout, before)
        head_dir = impact.resolve_capture_dir(checkout, after)
        if base_dir is None or head_dir is None:
            return (
                "REJECTED: capture dirs must be existing dirs "
                "inside the checkout or tmp"
            )
        try:
            return impact.format_diff_text(impact.diff_versions(base_dir, head_dir))
        except ValueError as exc:
            return f"REJECTED: {exc}"

    @tool
    def ask_diff(question: str, before: str, after: str) -> str:
        """Ask a local model about a capture diff (zero Gemini).

        Sends the ranked diff findings plus downscaled crops of
        the changed screenshot regions to the chat model (Ollama
        first, Nebius fallback); full-res screenshots never ride
        the turn.

        Args:
            question: What to judge, e.g. "what visually changed".
            before: Base capture dir (checkout-relative or tmp).
            after: Head capture dir (checkout-relative or tmp).
        """
        base_dir = impact.resolve_capture_dir(checkout, before)
        head_dir = impact.resolve_capture_dir(checkout, after)
        if base_dir is None or head_dir is None:
            return (
                "REJECTED: capture dirs must be existing dirs "
                "inside the checkout or tmp"
            )
        try:
            base_snap = impact.collect_capture(base_dir)
            head_snap = impact.collect_capture(head_dir)
            result = impact.diff_versions(base_snap, head_snap)
            evidence = impact.format_evidence_for_model(question, result)
            crops = impact.changed_crops(base_snap, head_snap)
        except ValueError as exc:
            return f"REJECTED: {exc}"
        return impact.ask_diff(question, evidence, images=crops)

    @tool
    def lighthouse_read(capture: str = "") -> str:
        """Summarize one capture's lighthouse scores.

        Args:
            capture: Capture dir (checkout-relative or tmp);
                '' reads the checkout's current artifacts/ux-audit.
        """
        target = impact.resolve_capture_dir(checkout, capture or impact.UX_DIR)
        if target is None:
            return "REJECTED: capture dir must exist inside the checkout or tmp"
        return impact.lighthouse_read(target)["text"]

    return base + [run_ux_capture, diff_versions, ask_diff, lighthouse_read]


def build_model(prefer=None):
    """Chat model via model_router. Returns (model, spec). Venv-only.

    prefer is the per-turn override ("ollama"/"nebius"); None keeps
    today's ladder (env contract applies)."""
    spec = model_router.resolve_llm(prefer=prefer)
    kwargs = model_router.chat_kwargs(spec)
    # No-think is the production setting: on this CPU-only box a qwen3
    # thinking trace takes 15+ min per prompt. CHAT_THINK=1 re-enables.
    # Bounded context for the same reason: the default Ollama window
    # lets long turns blow out CPU latency. OLLAMA_NUM_CTX overrides.
    think = os.environ.get("CHAT_THINK", "") == "1"
    if spec["provider"] == "ollama":
        from langchain_ollama import ChatOllama  # noqa: E402

        kwargs["num_ctx"] = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
        if not think:
            kwargs["model_kwargs"] = {"think": False}
        return ChatOllama(**kwargs), spec
    from langchain_openai import ChatOpenAI  # noqa: E402

    return ChatOpenAI(api_key=os.environ["NEBIUS_API_KEY"], **kwargs), spec


def _invoke_with_failover(model, spec, messages):
    """model.invoke with one local retry on retryable cloud errors.

    Returns (answer, failover): failover is None when the primary
    leg served, else {"from", "to", "error"} recording which legs
    ran. Local-primary calls never enter the helper (single hop, no
    ping-pong); local-unreachable surfaces the ORIGINAL cloud error.
    """
    failover = {}

    def _note(exc):
        failover["error"] = _short_err(exc)
        print(
            f"model: {spec['provider']} {_short_err(exc)} — retrying locally",
            file=sys.stderr,
        )

    def _make_local():
        local_model, local_spec = build_model(prefer="ollama")
        failover["from"] = f"{spec['provider']}:{spec['model']}"
        failover["to"] = f"{local_spec['provider']}:{local_spec['model']}"
        return lambda: local_model.invoke(messages)

    answer = model_router.with_local_fallback(
        lambda: model.invoke(messages),
        _make_local,
        _note,
        primary_is_local=(spec.get("provider") == "ollama"),
    )
    return answer, (failover or None)


def open_checkpointer(path=THREAD_DB):
    """Persistent checkpointer; falls back to per-process memory.

    Returns (saver, persistent, holder). The caller MUST keep holder
    referenced for the checkpointer's whole lifetime — it owns the DB
    connection, and dropping it closes the database.
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
    except ImportError:
        from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

        print("sqlite saver unavailable; memory is per-process only", file=sys.stderr)
        return InMemorySaver(), False, None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cm = SqliteSaver.from_conn_string(path)
    return cm.__enter__(), True, cm


def tool_budget_middleware():
    """ToolBudgetMiddleware instance. Venv-only (langchain import).

    Counts ToolMessages in the thread: within budget the request
    passes through; past CHAT_TOOL_BUDGET the model sees only
    propose_action plus the spent nudge, and stray non-propose calls
    are rejected at the tool boundary instead of executing (#307).
    Sync hooks only: chat turns use sync invoke/stream.
    """
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.messages import SystemMessage, ToolMessage

    def _spent(state):
        try:
            messages = state.get("messages", [])
        except AttributeError:
            return 0
        return sum(isinstance(m, ToolMessage) for m in messages)

    def _name(tool):
        return (
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        )

    class ToolBudget(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            if _spent(request.state) < CHAT_TOOL_BUDGET:
                return handler(request)
            narrowed = [t for t in request.tools if _name(t) == "propose_action"]
            return handler(
                request.override(
                    tools=narrowed,
                    messages=[
                        *request.messages,
                        SystemMessage(content=BUDGET_SPENT_NUDGE),
                    ],
                )
            )

        def wrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "propose_action":
                return handler(request)
            if _spent(request.state) < CHAT_TOOL_BUDGET:
                return handler(request)
            call = request.tool_call
            return ToolMessage(
                content="Error: tool budget spent, call propose_action "
                "or answer from gathered evidence.",
                tool_call_id=call["id"] or "",
                name=call["name"],
                status="error",
            )

    return ToolBudget()


def build_agent(
    repo, checkpointer, model=None, recorder=None, intent_hint=None, spec=None
):
    """create_deep_agent chat slice. Venv-only. Model injectable."""
    from deepagents import create_deep_agent  # noqa: E402

    if model is None:
        model, spec = build_model()
    stack = [tool_budget_middleware()]
    if recorder is not None:
        stack.extend(observe.trace_middleware(recorder))
    kwargs = {"middleware": stack}
    permissions = gate_chat_harness(spec) if spec is not None else None
    system = f"Repo: {repo}. " + SYSTEM
    if intent_hint:
        system += " " + intent_hint
    return create_deep_agent(
        model=model,
        tools=build_tools(s5tools.checkout_for(repo), impact_tools=True),
        system_prompt=system,
        interrupt_on=dict(INTERRUPT_ON),
        permissions=permissions,
        checkpointer=checkpointer,
        **kwargs,
    )


def _pending_interrupts(agent, config):
    found = []
    snap = agent.get_state(config)
    for task in snap.tasks:
        for iv in getattr(task, "interrupts", None) or []:
            found.append({"id": iv.id, "value": iv.value})
    return found


def _message_role(message):
    """human/system/other by class name (no langchain import)."""
    name = message.__class__.__name__
    if name == "HumanMessage":
        return "human"
    if name == "SystemMessage":
        return "system"
    return "other"


def plan_history_trim(messages, keep=MAX_HISTORY_MESSAGES):
    """Ids to drop from checkpoint history. Pure, no langchain import.

    Keeps a leading SystemMessage plus the newest `keep` messages,
    snapped to start at a HumanMessage boundary so a ToolMessage is
    never orphaned from its AI tool call. Returns [] when within
    budget or when no safe boundary exists (trim skipped, not forced).
    """
    messages = list(messages or [])
    rest = (
        messages[1:]
        if messages and _message_role(messages[0]) == "system"
        else messages
    )
    if len(rest) <= keep:
        return []
    window = rest[-keep:]
    starts = [i for i, m in enumerate(window) if _message_role(m) == "human"]
    if not starts:
        return []
    keep_ids = {id(m) for m in window[starts[0] :]}
    return [m.id for m in rest if id(m) not in keep_ids and getattr(m, "id", None)]


def trim_thread_history(agent, config, keep=MAX_HISTORY_MESSAGES):
    """Drop aged-out checkpoint messages. Best-effort; never raises."""
    try:
        snap = agent.get_state(config)
        messages = (snap.values or {}).get("messages", []) or []
        drop = plan_history_trim(messages, keep=keep)
        if not drop:
            return 0
        from langchain_core.messages import RemoveMessage  # noqa: E402

        agent.update_state(config, {"messages": [RemoveMessage(id=i) for i in drop]})
        return len(drop)
    except Exception:
        return 0


def _salvage_input(agent, config, first_input, tool_messages):
    """Message list for the post-cutoff final-answer pass. Pure shape."""
    from langchain_core.messages import HumanMessage  # noqa: E402

    if isinstance(first_input, dict) and "messages" in first_input:
        base = list(first_input["messages"])
        evidence = list(tool_messages[-SALVAGE_TOOL_TAIL:])
    else:
        try:
            thread = (agent.get_state(config).values or {}).get("messages", []) or []
        except Exception:
            thread = []
        base, evidence = [], list(thread[-SALVAGE_TOOL_TAIL:])
    return (
        base
        + evidence
        + [
            HumanMessage(
                content="Answer now using only the conversation and tool "
                "results above. Do not make any more tool calls."
            )
        ]
    )


def _stream(agent, first_input, config, model=None, spec=None, failover=None):
    """Stream one agent invocation.

    Returns (reply, tool_msgs, msgs, salvaged). On a tool-loop
    recursion cutoff with a model available, one final no-tools
    pass answers from gathered evidence instead of dying (same
    tail-cap salvage pattern); without a model the error propagates.
    With a spec, the salvage pass gets one local retry on retryable
    cloud errors, recorded into the failover dict when given.
    """
    from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402
    from langgraph.errors import GraphRecursionError  # noqa: E402

    text_parts = []
    tool_messages = []
    last_ai = ""
    try:
        stream = agent.stream(
            first_input,
            config={
                **config,
                "recursion_limit": RECURSION_LIMIT,
                "callbacks": tracing.tracing_callbacks(),
            },
            stream_mode=["messages", "updates"],
        )
        for mode, payload in stream:
            if mode == "messages":
                chunk = payload[0]
                if (
                    isinstance(chunk, AIMessageChunk)
                    and isinstance(chunk.content, str)
                    and chunk.content
                ):
                    print(chunk.content, end="", flush=True)
                    text_parts.append(chunk.content)
            elif mode == "updates":
                for update in payload.values():
                    # Updates are usually {messages: [...]} dicts, but
                    # some nodes (e.g. around interrupts) emit tuples
                    # instead.
                    msgs = (
                        update.get("messages", []) if isinstance(update, dict) else []
                    )
                    for m in msgs:
                        if m.__class__.__name__ == "ToolMessage":
                            tool_messages.append(m)
                        elif (
                            isinstance(m, AIMessage)
                            and isinstance(m.content, str)
                            and m.content
                            and not getattr(m, "tool_calls", None)
                        ):
                            last_ai = m.content
    except GraphRecursionError:
        if model is None:
            raise
        try:
            salvage_msgs = _salvage_input(agent, config, first_input, tool_messages)
            if spec is None:
                answer = model.invoke(salvage_msgs)
            else:
                answer, info = _invoke_with_failover(model, spec, salvage_msgs)
                if info is not None and failover is not None:
                    failover.update(info)
            salvaged = (answer.content or "").strip()
        except Exception:
            salvaged = ""
        if not salvaged:
            raise
        print(salvaged, flush=True)
        return salvaged, tool_messages, [AIMessage(content=salvaged)], True
    print("", flush=True)
    # Streaming models feed text_parts; non-streaming ones only appear
    # whole in updates. Prefer the last text-only AI message.
    reply = (last_ai or "".join(text_parts)).strip()
    return reply, tool_messages, [AIMessage(content=reply)], False


def _emit_interrupt(thread_id, interrupts):
    try:
        blob = json.dumps({"thread_id": thread_id, "interrupts": interrupts})
    except (TypeError, ValueError) as exc:
        print(f"interrupt payload not JSON: {exc}", file=sys.stderr)
        return 1
    print(f"INTERRUPT: {blob}", flush=True)
    return EXIT_NEEDS_APPROVAL


def _cmd_validate(raw):
    try:
        payload = json.loads(cli.b64decode(raw))
    except Exception:
        print(json.dumps({"ok": False, "text": "REJECTED: bad input"}))
        return 2
    ok, text = validate_proposal(payload)
    print(json.dumps({"ok": ok, "text": text}))
    return 0


def _cmd_trivial_turn(repo, thread_id, prompt, started, model, spec):
    """Fast path for greetings: one direct model call, no tools, no
    history. Still ledgered (usage when reported) with a minimal
    run_start/model_request/model_response/run_end trace so the run
    expands in Past runs like full turns."""
    from langchain_core.messages import (  # noqa: E402
        AIMessage,
        HumanMessage,
        SystemMessage,
    )

    recorder = observe.TraceRecorder(observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id, f"{spec['provider']}:{spec['model']}")
    messages = [SystemMessage(content=TRIVIAL_SYSTEM), HumanMessage(content=prompt)]
    try:
        recorder.model_request(len(messages))
        answer, failover = _invoke_with_failover(model, spec, messages)
        reply = (answer.content or "").strip()
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "deep_chat.py",
            f"{spec['provider']}:{spec['model']}",
            thread_id,
            messages,
            started,
            extra={
                "repo": repo,
                "mode": "turn",
                "intent": "trivial",
                "prompt": prompt,
                "trace": recorder.path,
                "decisions": decisions.build_decisions([], [], reply, error=str(exc)),
                "error": str(exc)[:200],
            },
        )
        return cli.sorry(reply)
    print(reply, flush=True)
    raw_usage = getattr(answer, "usage_metadata", None)
    usage = None
    if isinstance(raw_usage, dict):
        usage = {
            k: raw_usage.get(k, 0)
            for k in ("input_tokens", "output_tokens", "total_tokens")
        }
    recorder.model_response(raw_usage if isinstance(raw_usage, dict) else {})
    if failover:
        recorder.event("model_failover", **failover)
    recorder.run_end(True)
    recorder.close()
    served = failover["to"] if failover else f"{spec['provider']}:{spec['model']}"
    ledger.record(
        "deep_chat.py",
        served,
        thread_id,
        messages + [AIMessage(content=reply)],
        started,
        extra={
            "repo": repo,
            "mode": "turn",
            "intent": "trivial",
            "prompt": prompt,
            "trace": recorder.path,
            "decisions": decisions.build_decisions(
                [], [], reply, reason="trivial greeting, fast path"
            ),
            **({"failover": failover} if failover else {}),
        },
        usage=usage,
        calls=[],
    )
    return 0


def _cmd_turn(repo, thread_id, raw_prompt, prefer=None):
    prompt = cli.b64decode(raw_prompt)
    if prompt is None:
        return cli.usage_error("bad base64 prompt")
    if not prompt.strip() or len(prompt) > MAX_PROMPT:
        return cli.usage_error("bad prompt")
    started = time.time()
    try:
        model, spec = build_model(prefer=prefer)
    except (RuntimeError, ValueError) as exc:
        return cli.sorry(f"Sorry, no model is available right now: {_short_err(exc)}")
    except ImportError as exc:
        # Stdout, like every other Sorry line: exit 1 promises the
        # reply on stdout so the SSE chunk reader shows it on the phone.
        return cli.sorry(f"Sorry, the chat backend is not installed: {_short_err(exc)}")
    print(f"deep_chat via {spec['provider']}:{spec['model']}", file=sys.stderr)
    turn_intent = intent.classify(prompt)
    if turn_intent == "trivial":
        return _cmd_trivial_turn(repo, thread_id, prompt, started, model, spec)
    from langchain_core.messages import HumanMessage  # noqa: E402

    recorder = observe.TraceRecorder(observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id, f"{spec['provider']}:{spec['model']}")
    saver, _, _holder = open_checkpointer()
    # Intent-routed skills now live in the unified registry
    # (skills/*.md with intent_* trigger nodes); the one-line intent
    # hint still prefixes the injection.
    skill_base = intent.hint(turn_intent) or ""
    intent_hint = skills_registry.SkillsRegistry().inject_skills_for_node(
        f"intent_{turn_intent}", skill_base
    )
    agent = build_agent(
        repo,
        saver,
        model=model,
        recorder=recorder,
        intent_hint=intent_hint or None,
        spec=spec,
    )
    config = {"configurable": {"thread_id": thread_id}}
    trimmed = trim_thread_history(agent, config)
    if trimmed:
        print(f"trimmed {trimmed} aged-out history message(s)", file=sys.stderr)
    messages = [HumanMessage(content=prompt)]
    failover = {}
    try:
        reply, tool_msgs, _, salvaged = _stream(
            agent,
            {"messages": messages},
            config,
            model=model,
            spec=spec,
            failover=failover,
        )
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        recorder.run_end(False)
        recorder.close()
        _usage, _calls = recorder.totals()
        ledger.record(
            "deep_chat.py",
            f"{spec['provider']}:{spec['model']}",
            thread_id,
            messages,
            started,
            extra={
                "repo": repo,
                "mode": "turn",
                "trace": recorder.path,
                "intent": turn_intent,
                "prompt": prompt,
                "decisions": decisions.build_decisions(
                    _calls, [], reply, error=str(exc)
                ),
                "error": str(exc)[:200],
            },
            usage=_usage,
            calls=_calls,
        )
        return cli.sorry(reply)
    from langchain_core.messages import AIMessage  # noqa: E402

    recorder.run_end(True)
    recorder.close()
    print(f"trace: {recorder.path}", file=sys.stderr)
    _usage, _calls = recorder.totals()
    tool_results = [decisions.tool_text(getattr(m, "content", "")) for m in tool_msgs]
    ledger.record(
        "deep_chat.py",
        f"{spec['provider']}:{spec['model']}",
        thread_id,
        messages + tool_msgs + [AIMessage(content=reply)],
        started,
        extra={
            "repo": repo,
            "mode": "turn",
            "trace": recorder.path,
            "intent": turn_intent,
            "prompt": prompt,
            "decisions": decisions.build_decisions(
                _calls, tool_results, reply, reason=f"{turn_intent} turn, full path"
            ),
            **({"salvaged": True} if salvaged else {}),
            **({"failover": dict(failover)} if failover else {}),
        },
        usage=_usage,
        calls=_calls,
    )
    pending = _pending_interrupts(agent, config)
    if pending:
        return _emit_interrupt(thread_id, pending)
    return 0


def _cmd_resume(repo, thread_id, verdict, raw_message):
    try:
        decision = decision_for_verdict(verdict)
    except ValueError as exc:
        return cli.usage_error(str(exc))
    message = ""
    if raw_message is not None:
        message = cli.b64decode(raw_message)
        if message is None:
            return cli.usage_error("bad base64 message")
        try:
            decision = decision_for_verdict(verdict, message)
        except ValueError as exc:
            return cli.usage_error(str(exc))
    started = time.time()
    try:
        model, spec = build_model()
    except (RuntimeError, ValueError) as exc:
        return cli.sorry(f"Sorry, no model is available right now: {_short_err(exc)}")
    except ImportError as exc:
        # Stdout, like every other Sorry line: exit 1 promises the
        # reply on stdout so the SSE chunk reader shows it on the phone.
        return cli.sorry(f"Sorry, the chat backend is not installed: {_short_err(exc)}")
    print(f"deep_chat resume via {spec['provider']}:{spec['model']}", file=sys.stderr)
    from langgraph.types import Command  # noqa: E402
    from langchain_core.messages import AIMessage  # noqa: E402

    recorder = observe.TraceRecorder(observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id, f"{spec['provider']}:{spec['model']}")
    saver, _, _holder = open_checkpointer()
    agent = build_agent(repo, saver, model=model, recorder=recorder, spec=spec)
    config = {"configurable": {"thread_id": thread_id}}
    pending = _pending_interrupts(agent, config)
    if not pending:
        print(f"no pending approval for thread {thread_id!r}", file=sys.stderr)
        recorder.run_end(False)
        recorder.close()
        return 1
    # Batched mode: one decision per action_request, by position.
    first = pending[0].get("value") or {}
    count = len(first.get("action_requests", []) or []) or 1
    failover = {}
    try:
        reply, tool_msgs, _, salvaged = _stream(
            agent,
            Command(resume=resume_payload([decision] * count)),
            config,
            model=model,
            spec=spec,
            failover=failover,
        )
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        recorder.run_end(False)
        recorder.close()
        _usage, _calls = recorder.totals()
        ledger.record(
            "deep_chat.py",
            f"{spec['provider']}:{spec['model']}",
            thread_id,
            [AIMessage(content=reply)],
            started,
            extra={
                "repo": repo,
                "mode": "resume",
                "verdict": verdict,
                "trace": recorder.path,
                "decisions": decisions.build_decisions(
                    _calls,
                    [],
                    reply,
                    verdict=verdict,
                    message=message,
                    error=str(exc),
                    proposals=approved_proposal_blocks(
                        first.get("action_requests", [])
                    ),
                ),
                "error": str(exc)[:200],
            },
            usage=_usage,
            calls=_calls,
        )
        return cli.sorry(reply)
    recorder.run_end(True)
    recorder.close()
    print(f"trace: {recorder.path}", file=sys.stderr)
    _usage, _calls = recorder.totals()
    ledger.record(
        "deep_chat.py",
        f"{spec['provider']}:{spec['model']}",
        thread_id,
        tool_msgs + [AIMessage(content=reply)],
        started,
        extra={
            "repo": repo,
            "mode": "resume",
            "verdict": verdict,
            "trace": recorder.path,
            "decisions": decisions.build_decisions(
                _calls,
                [decisions.tool_text(getattr(m, "content", "")) for m in tool_msgs],
                reply,
                verdict=verdict,
                message=message,
                proposals=approved_proposal_blocks(first.get("action_requests", [])),
                reason=f"resumed after {verdict}, full path",
            ),
            **({"salvaged": True} if salvaged else {}),
            **({"failover": dict(failover)} if failover else {}),
        },
        usage=_usage,
        calls=_calls,
    )
    if verdict == "approve":
        for text in approved_proposal_blocks(first.get("action_requests", [])):
            print(text, flush=True)
    pending = _pending_interrupts(agent, config)
    if pending:
        return _emit_interrupt(thread_id, pending)
    return 0


def extract_prefer(argv):
    """Per-turn backend override from --prefer=ollama|nebius.

    Scanned (not positional) so it survives every argv shape; the
    b64 prompt slot can never match ('-' is outside the base64
    alphabet). Returns the provider or None. Raises ValueError on a
    value the router would reject, so misuse fails at the CLI, not
    as a confusing Sorry."""
    for arg in argv[1:]:
        if isinstance(arg, str) and arg.startswith("--prefer="):
            value = arg.split("=", 1)[1].strip().lower()
            if value not in ("ollama", "nebius"):
                raise ValueError(f"bad --prefer={value!r}: want 'ollama' or 'nebius'")
            return value
    return None


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    try:
        prefer = extract_prefer(argv)
    except ValueError as exc:
        return cli.usage_error(str(exc))
    # Strip before positional parsing so the flag never shifts the
    # resume/message slots.
    argv = [a for a in argv if not (isinstance(a, str) and a.startswith("--prefer="))]
    if len(argv) == 3 and argv[1] == "--validate-proposal":
        return _cmd_validate(argv[2])
    if len(argv) == 3 and argv[2] == "--supervise":
        if not s5tools.valid_repo(argv[1]):
            return cli.usage_error(f"bad repo: {argv[1]!r}")
        thread = f"supervise-{argv[1].split('/')[1]}"
        raw = base64.b64encode(SUPERVISOR_BRIEF.encode()).decode()
        return _cmd_turn(argv[1], thread, raw)
    if len(argv) < 4 or not s5tools.valid_repo(argv[1]):
        return cli.usage_error(
            "usage: deep_chat.py owner/repo <thread-id> <b64-prompt>"
            " [--prefer=ollama|nebius]",
            "   or: deep_chat.py owner/repo <thread-id> --resume "
            "approve|reject [b64-message]",
            "   or: deep_chat.py owner/repo --supervise",
            "   or: deep_chat.py --validate-proposal <b64-json>",
        )
    if not valid_thread_id(argv[2]):
        return cli.usage_error("bad thread-id")
    repo, thread_id = argv[1], argv[2]
    try:
        s5tools.checkout_for(repo)
    except ValueError:
        return cli.usage_error(f"bad repo: {repo!r}")
    if argv[3] == "--resume":
        if len(argv) < 5:
            return cli.usage_error("resume needs a verdict")
        return _cmd_resume(repo, thread_id, argv[4], argv[5] if len(argv) > 5 else None)
    return _cmd_turn(repo, thread_id, argv[3], prefer=prefer)


if __name__ == "__main__":
    raise SystemExit(main())

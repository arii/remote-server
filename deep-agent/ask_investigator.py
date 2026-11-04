"""Ask investigator: LangChain agent for /buttons/api/ask.

Usage: ask_investigator.py owner/repo <base64-instruction>
Prints progress lines, then a FINAL: line with the reply (same shape the
server already parses for proposals). Read-only tools plus one
allowlisted run_repo_command verify tool plus one SSRF-guarded
fetch_url tool, all scoped to the repo's checkout. Local model
first (qwen3:8b via Ollama).

Every run is recorded to the ledger with tool results.
"""

import json
import os
import subprocess
import sys
import time

import cli

cli.bootstrap()

from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_ollama import ChatOllama  # noqa: E402

import ledger  # noqa: E402
import model_router  # noqa: E402
import observe  # noqa: E402
import retrieval  # noqa: E402
import runs  # noqa: E402
import s5tools  # noqa: E402


def _model_config():
    """(model_id, ollama_url); blank env falls back to defaults.

    Blank-tolerant read shared with model_router (cf. #620, where
    set-but-empty env beat working defaults and produced provider
    400s): ASK_MODEL/OLLAMA_URL resolve here, never as raw .get().
    """
    return (
        model_router._env("ASK_MODEL", model_router.DEFAULT_OLLAMA_MODEL),
        model_router._env("OLLAMA_URL", model_router.DEFAULT_OLLAMA_URL),
    )


MODEL_ID, OLLAMA_URL = _model_config()

# Single-sourced from backend/exec/engine.py ALLOWED_TD_COMMANDS
# (the truth): the hand mirror drifted behind the #395 shapes, so
# ALLOWED_TD is the engine's list itself, never a copy (#521).
# engine.py is stdlib-only, safe before the langchain imports below.
if cli.repo_root() not in sys.path:
    sys.path.insert(0, cli.repo_root())
from backend.exec.engine import ALLOWED_TD_COMMANDS as ALLOWED_TD  # noqa: E402

_repo = ""
_checkout = ""
_retriever = None


def _retriever_for():
    global _retriever
    retrieval.require_ready()
    if _retriever is None:
        from langchain_chroma import Chroma
        from langchain_ollama import OllamaEmbeddings

        _retriever = Chroma(
            persist_directory=os.path.join(
                cli.repo_root(), "deep-agent", "chroma_db"),
            embedding_function=OllamaEmbeddings(
                model="nomic-embed-text", base_url=OLLAMA_URL
            ),
            collection_name="s5-docs",
        ).as_retriever(search_kwargs={"k": 3})
    return _retriever


@tool
def search_docs(query: str) -> str:
    """Search S5 runbooks and docs for how-to and process answers.

    Args:
        query: Short question (2-10 words).
    """
    if not query or not query.strip() or len(query) > 500:
        return "REJECTED: bad query"
    try:
        hits = _retriever_for().invoke(query)
    except retrieval.IndexNotReadyError as exc:
        return str(exc)
    except Exception as exc:
        return f"doc search unavailable: {exc}"
    parts = [
        f"[{h.metadata.get('repo')}/{h.metadata.get('source')}] "
        f"{h.page_content[:700]}"
        for h in hits
    ]
    return "\n\n".join(parts)[:1600] or "no doc hits"


@tool
def read_repo_file(path: str) -> str:
    """Read a file from the repo checkout (read-only).

    Args:
        path: Repo-relative path, e.g. buttons/server.py.
    """
    # Same sandbox + messages as the inline version this replaces;
    # one implementation lives in s5tools now (like grep_repo).
    return s5tools.read_repo_file(_checkout, path)


@tool
def grep_repo(pattern: str) -> str:
    """Search file contents in the repo checkout (read-only grep).

    Always search the shortest distinctive fragment: for an HTTP
    route use the path only (e.g. '/buttons/api/ask'), never the
    method plus path ('POST ...' matches docs, never handler code).
    Hits in docs or eval files are mentions, not the implementation:
    answer from the file whose code handles the match.

    Args:
        pattern: Short literal search text.
    """
    # Delegate to the shared helper so log/index/ledger noise
    # (.boomtick/, chroma_db/, .claude/, .muse/, ledger.jsonl) never
    # floods the -m 20 window and buries real hits like server.py.
    return s5tools.grep_repo(_checkout, pattern)


def _json_or_none(text):
    try:
        return json.loads(text or "")
    except json.JSONDecodeError:
        return None


def _td_error(number, snap):
    """Format a td JSON error with the PR number so agents relay it."""
    msg = (snap.get("message") or "").strip()[:200] or "unknown error"
    code = snap.get("code", "")
    suffix = f" (code {code})" if code != "" else ""
    return f"PR #{number}: td error: {msg}{suffix}"


@tool
def pr_state(number: str) -> str:
    """Summarize a GitHub PR: state, reviews, checks (read-only).

    Args:
        number: PR number as digits, e.g. "153".
    """
    # String type: the tool wrapper coerces True to 1 for int args,
    # which would smuggle a bool past validation into the command.
    if not isinstance(number, str) or not number.isdigit() \
            or int(number) <= 0:
        return "REJECTED: bad PR number"
    try:
        proc = subprocess.run(
            ["td", "--json", "gh", "pr-view", "--pr", str(number),
             "--repo", _repo],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"td failed: {exc}"
    if proc.returncode != 0:
        # td exits nonzero for API errors but still prints the JSON
        # error on stdout (stderr stays empty): parse it first.
        snap = _json_or_none(proc.stdout)
        if isinstance(snap, dict) and snap.get("status") == "error":
            return _td_error(number, snap)
        return f"td error: {(proc.stderr or '').strip()[:200]}"
    try:
        snap = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return f"td error: bad json: {(proc.stdout or '').strip()[:200]}"
    if not isinstance(snap, dict):
        return f"td error: bad json: {(proc.stdout or '').strip()[:200]}"
    if snap.get("status") == "error":
        return _td_error(number, snap)
    summary = snap.get("checksSummary") or {}
    return _short_line(
        f"{snap.get('state')} draft={snap.get('draft')} "
        f"review={snap.get('reviewDecision')} "
        f"mergeable={snap.get('mergeable')} "
        f"checks={summary.get('passed')}/{summary.get('total')} "
        f"failed={summary.get('failed')} "
        f"pending={summary.get('pending')} "
        f"title={snap.get('title')}", limit=400)


@tool
def run_repo_command(command: str) -> str:
    """Run an allowlisted verification command in the repo checkout.

    Use AFTER reasoning to verify a claim — run the failing test,
    check git state — not to explore; read and grep first. Never
    executes anything else: anything outside the allowlist comes
    back as REJECTED text.

    Args:
        command: Full command line (allowlisted tests/lint/git
            shapes only).
    """
    return s5tools.run_repo_command(_checkout, command)


@tool
def fetch_url(url: str) -> str:
    """Fetch a URL's text: upstream docs, API references, changelogs.

    Use when you have a URL in hand (never guess deep links).
    Returns decoded text capped at 256KB with a truncation marker;
    private/loopback/metadata addresses, non-http(s) schemes, and
    failures come back as FETCH_ERROR text. No search, no JS
    rendering, no auth, no POST.

    Args:
        url: Full http(s) URL to GET.
    """
    return s5tools.fetch_url(url)


@tool
def search_turns(query: str, kind: str = "",
                 limit: int = 5) -> str:
    """Search past agent turns for prior outcomes (read-only recall).

    Use when the user asks about earlier work: how something was
    fixed before, what triage concluded, whether this failure was
    seen already. Keyword match over each turn's script, model,
    thread, prompt, and final answer, newest first, capped lines.

    Args:
        query: Keywords from the earlier outcome, e.g. "triage verdict".
        kind: Optional row-kind filter, e.g. "deep-agent-run".
        limit: Max hits, 1-5 (default 5).
    """
    return runs.search_turns(query, kind=kind, limit=limit)


SYSTEM = (
    "You are helping Ari from his phone. Be terse, no filler. "
    "Investigate read-only with your tools; never propose running "
    "anything except via the PROPOSE protocol below.\n"
    "If the instruction asks for a write action, investigate first, "
    "then propose the exact change and STOP. Allowed proposal kinds: "
    "creating a GitHub issue, running one allowlisted td command, or "
    "dispatching implementation to a worktree session.\n"
    "Emit a proposal ONLY in exactly this form, on its own lines:\n"
    "PROPOSE issue\nTITLE: <one line>\nBODY:\n<markdown>\nEND PROPOSE\n"
    "or\nPROPOSE td\nCOMMAND: <exact td command>\nEND PROPOSE\n"
    "or\nPROPOSE dispatch\nREPO: <owner/name>\nBRANCH: <base branch>\n"
    "TASK:\n<implementation brief>\nEND PROPOSE\n"
    "A PROPOSE td COMMAND must be one of the allowed shapes "
    "verbatim, changing only N to digits — never invent td "
    "subcommands or flags.\n"
    f"Allowed td commands (exact shapes):\n" + "\n".join(ALLOWED_TD) + "\n"
    "If no write is needed, just answer — no PROPOSE block. "
    "Always invoke tools through the function-calling interface. "
    "Never write a tool call out as JSON text. "
    "Keep replies readable on a phone: short paragraphs or a short "
    "list, no wide tables."
)


def _short_line(content, limit=160):
    """First line of a tool result, capped for phone-sized tails.

    The REPORT block streams live to the phone; a visible ellipsis
    marks truncation so a cut line never reads as complete.
    """
    line = content.strip().splitlines()[0]
    return line if len(line) <= limit else line[:limit] + "…"


class _ProgressRecorder(observe.TraceRecorder):
    """TraceRecorder that narrates each step to stdout.

    Local LLM steps cost 60-120s each; without life signs a 5-10
    minute run looks hung and every caller timeout fires blind.
    Step lines are additive: parsers match REPORT/FINAL:/
    tool_calls_made: only, so banner -> steps -> REPORT/FINAL stays
    parseable, and a timeout-killed run (#475) leaves its steps on
    record. Stdout, not stderr: run_pi_pipeline streams stdout to
    the pi-progress tail.
    """

    def __init__(self, path):
        super().__init__(path)
        self.steps = 0
        self._mark = time.time()

    def _say(self, kind, name, elapsed):
        self.steps += 1
        try:
            print(f"ask: step {self.steps} {kind} {name} "
                  f"{elapsed:.1f}s", flush=True)
        except (OSError, ValueError):
            pass  # observability must not break runs
        self._mark = time.time()

    def model_response(self, usage):
        super().model_response(usage)
        self._say("model", MODEL_ID, time.time() - self._mark)

    def tool_call(self, name, args, duration_s, ok, result=None,
                  error=None):
        super().tool_call(name, args, duration_s, ok, result=result,
                          error=error)
        self._say("tool", name, duration_s)


def main() -> int:
    if len(sys.argv) < 3 or not s5tools.valid_repo(sys.argv[1]):
        return cli.usage_error(
            "usage: ask_investigator.py owner/repo <b64-instruction>")
    global _repo, _checkout
    _repo = sys.argv[1]
    _checkout = s5tools.checkout_for(_repo)
    instruction = cli.b64decode(sys.argv[2])
    if instruction is None:
        return cli.usage_error("bad base64 instruction")
    if not instruction.strip() or len(instruction) > 2000:
        return cli.usage_error("bad instruction")
    started = time.time()
    # No-think is the production setting: on this CPU-only box a
    # qwen3 thinking trace takes 15+ min per prompt, unshippable for
    # a phone assistant. Set ASK_THINK=1 to re-enable for eval.
    think = os.environ.get("ASK_THINK", "") == "1"
    model = ChatOllama(
        model=MODEL_ID,
        base_url=OLLAMA_URL,
        temperature=0,
        model_kwargs={} if think else {"think": False},
    )
    thread = "ask-" + _repo.replace("/", "-")
    recorder = _ProgressRecorder(
        observe.default_trace_path("ask_investigator", thread))
    recorder.run_start("ask_investigator.py", thread,
                       f"ollama:{MODEL_ID}")
    agent = create_agent(
        model=model,
        tools=[search_docs, read_repo_file, grep_repo, pr_state,
               run_repo_command, fetch_url, search_turns],
        system_prompt=f"Repo: {_repo}. " + SYSTEM,
        middleware=observe.trace_middleware(recorder),
    )
    print(f"Investigating {_repo} (local model)…", flush=True)
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": instruction}]},
            # 20: trace middleware nodes consume budget (~4 steps per
            # tool call, measured); preserves room for ~4 tool calls.
            config={"recursion_limit": 20},
        )
    except Exception as exc:
        recorder.run_end(False)
        recorder.close()
        _usage, _calls = recorder.totals()
        ledger.record(
            "ask_investigator.py",
            f"ollama:{MODEL_ID}",
            thread,
            [HumanMessage(content=instruction)],
            started,
            extra={"trace": recorder.path, "error": str(exc)[:200]},
            usage=_usage,
            calls=_calls,
        )
        raise
    recorder.run_end(True)
    recorder.close()
    print(f"trace: {recorder.path}", file=sys.stderr)
    calls = sum(
        len(getattr(m, "tool_calls", []) or [])
        for m in result["messages"]
    )
    ledger.record(
        "ask_investigator.py",
        f"ollama:{MODEL_ID}",
        thread,
        result["messages"],
        started,
        extra={"trace": recorder.path},
    )
    print(f"tool_calls_made: {calls}")
    elapsed = time.time() - started
    word = "step" if recorder.steps == 1 else "steps"
    print(f"done in {elapsed:.1f}s, {recorder.steps} {word}")
    outcomes = [
        _short_line(str(m.content))
        for m in result["messages"]
        if isinstance(m, ToolMessage)
        and str(m.content).strip()
    ]
    print("REPORT (authoritative, from tool results):")
    if outcomes:
        for line in outcomes:
            print(f"  {line}")
    else:
        print("  (no tools called)")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

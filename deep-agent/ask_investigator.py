"""Ask investigator: LangChain agent for /buttons/api/ask.

Usage: ask_investigator.py owner/repo <base64-instruction>
Prints progress lines, then a FINAL: line with the reply (same shape the
server already parses for proposals). Read-only tools, all scoped to the
repo's checkout. Local model first (qwen3:8b via Ollama).

Every run is recorded to the ledger with tool results.
"""

import base64
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_ollama import ChatOllama  # noqa: E402

import ledger  # noqa: E402
import observe  # noqa: E402
import s5tools  # noqa: E402

MODEL_ID = os.environ.get("ASK_MODEL", "qwen3:8b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")

# Mirror of ALLOWED_TD_COMMANDS in buttons/server.py (source of truth).
ALLOWED_TD = [
    "td --json agent fix-ci --pr N --execute",
    "td gh read-pr-reviews --pr N",
    "td gh read-pr-comments --pr N",
    "td gh resolve-conflicts --pr N",
    "td context-warm",
    "td --json preflight",
    "td --json agent list-sessions",
    "td --json jules list-sessions",
    "td --json agent run-feedback-check",
]

_repo = ""
_checkout = ""
_retriever = None


def _retriever_for():
    global _retriever
    if _retriever is None:
        from langchain_chroma import Chroma
        from langchain_ollama import OllamaEmbeddings

        _retriever = Chroma(
            persist_directory="/srv/remote-server/deep-agent/chroma_db",
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


@tool
def pr_state(number: str) -> str:
    """Summarize a GitHub PR: state, reviews, checks (read-only).

    Args:
        number: PR number as digits, e.g. "153".
    """
    # String type: the tool wrapper coerces True to 1 for int args,
    # which would smuggle a bool past validation into gh.
    if not isinstance(number, str) or not number.isdigit() \
            or int(number) <= 0:
        return "REJECTED: bad PR number"
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", _repo,
             "--json", "state,isDraft,reviewDecision,mergeable,title",
             "--jq", "{state, draft: .isDraft, review: .reviewDecision, "
                     "mergeable, title}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"gh failed: {exc}"
    if proc.returncode != 0:
        return f"gh error: {(proc.stderr or '').strip()[:200]}"
    return (proc.stdout or "").strip()[:800]


SYSTEM = (
    "You are helping Ari from his phone. Be terse, no filler. "
    "Investigate read-only with your tools; never propose running "
    "anything except via the PROPOSE protocol below.\n"
    "If the instruction asks for a write action, investigate first, "
    "then propose the exact change and STOP. Allowed proposal kinds: "
    "creating a GitHub issue, or running one allowlisted td command.\n"
    "Emit a proposal ONLY in exactly this form, on its own lines:\n"
    "PROPOSE issue\nTITLE: <one line>\nBODY:\n<markdown>\nEND PROPOSE\n"
    "or\nPROPOSE td\nCOMMAND: <exact td command>\nEND PROPOSE\n"
    "A PROPOSE td COMMAND must be one of the allowed shapes "
    "verbatim, changing only N to digits — never invent td "
    "subcommands or flags.\n"
    f"Allowed td commands (exact shapes):\n" + "\n".join(ALLOWED_TD) + "\n"
    "If no write is needed, just answer — no PROPOSE block. "
    "Always invoke tools through the function-calling interface. "
    "Never write a tool call out as JSON text."
)


def main() -> int:
    if len(sys.argv) < 3 or not s5tools.valid_repo(sys.argv[1]):
        print("usage: ask_investigator.py owner/repo <b64-instruction>",
              file=sys.stderr)
        return 2
    global _repo, _checkout
    _repo = sys.argv[1]
    _checkout = s5tools.checkout_for(_repo)
    try:
        instruction = base64.b64decode(sys.argv[2]).decode()
    except Exception:
        print("bad base64 instruction", file=sys.stderr)
        return 2
    if not instruction.strip() or len(instruction) > 2000:
        print("bad instruction", file=sys.stderr)
        return 2
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
    recorder = observe.TraceRecorder(
        observe.default_trace_path("ask_investigator", thread))
    recorder.run_start("ask_investigator.py", thread,
                       f"ollama:{MODEL_ID}")
    agent = create_agent(
        model=model,
        tools=[search_docs, read_repo_file, grep_repo, pr_state],
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
    except Exception:
        recorder.run_end(False)
        recorder.close()
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
    outcomes = [
        str(m.content).strip().splitlines()[0][:200]
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

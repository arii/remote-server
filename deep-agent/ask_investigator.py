"""Ask investigator: LangChain agent replacing `pi -p` for /buttons/api/ask.

Usage: ask_investigator.py owner/repo <base64-instruction>
Prints progress lines, then a FINAL: line with the reply (same shape the
server already parses for proposals). Read-only tools, all scoped to the
repo's checkout. Local model first (qwen3:8b via Ollama).

Every run is recorded to the ledger with tool results.
"""

import base64
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from langchain.agents import create_agent  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_ollama import ChatOllama  # noqa: E402

import ledger  # noqa: E402

MODEL_ID = os.environ.get("ASK_MODEL", "qwen3:8b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

CHECKOUTS = {"arii/remote-server": "/srv/remote-server"}

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


def _checkout_for(repo):
    if repo in CHECKOUTS:
        return CHECKOUTS[repo]
    short = repo.split("/", 1)[1]
    return f"/srv/repos/{short}"


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
    full = os.path.realpath(os.path.join(_checkout, path or ""))
    if full != _checkout and not full.startswith(_checkout + os.sep):
        return "REJECTED: path escapes the checkout"
    if not os.path.isfile(full):
        return "not found: no such file"
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read(3000)
    except OSError as exc:
        return f"read failed: {exc}"


@tool
def grep_repo(pattern: str) -> str:
    """Search file contents in the repo checkout (read-only ripgrep).

    Args:
        pattern: Literal search text.
    """
    if not pattern or len(pattern) > 120:
        return "REJECTED: bad pattern"
    try:
        proc = subprocess.run(
            ["rg", "--no-heading", "-n", "-m", "20", "--", pattern, "."],
            cwd=_checkout,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"grep failed: {exc}"
    out = (proc.stdout or "").strip()
    return out[:2000] or "no matches"


@tool
def pr_state(number: int) -> str:
    """Summarize a GitHub PR: state, reviews, checks (read-only).

    Args:
        number: PR number (digits only).
    """
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
    ):
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
    f"Allowed td commands (exact shapes):\n" + "\n".join(ALLOWED_TD) + "\n"
    "If no write is needed, just answer — no PROPOSE block. "
    "Always invoke tools through the function-calling interface. "
    "Never write a tool call out as JSON text."
)


def main() -> int:
    if len(sys.argv) < 3 or not REPO_RE.match(sys.argv[1]):
        print("usage: ask_investigator.py owner/repo <b64-instruction>",
              file=sys.stderr)
        return 2
    global _repo, _checkout
    _repo = sys.argv[1]
    _checkout = _checkout_for(_repo)
    try:
        instruction = base64.b64decode(sys.argv[2]).decode()
    except Exception:
        print("bad base64 instruction", file=sys.stderr)
        return 2
    if not instruction.strip() or len(instruction) > 2000:
        print("bad instruction", file=sys.stderr)
        return 2
    started = time.time()
    model = ChatOllama(
        model=MODEL_ID,
        base_url=OLLAMA_URL,
        temperature=0,
    )
    agent = create_agent(
        model=model,
        tools=[search_docs, read_repo_file, grep_repo, pr_state],
        system_prompt=f"Repo: {_repo}. " + SYSTEM,
    )
    print(f"Investigating {_repo} (local model)…", flush=True)
    result = agent.invoke(
        {"messages": [{"role": "user", "content": instruction}]},
        config={"recursion_limit": 12},
    )
    calls = sum(
        len(getattr(m, "tool_calls", []) or [])
        for m in result["messages"]
    )
    ledger.record(
        "ask_investigator.py",
        f"ollama:{MODEL_ID}",
        "ask-" + _repo.replace("/", "-"),
        result["messages"],
        started,
    )
    print(f"tool_calls_made: {calls}")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Per-repo triage fan-out: orchestrator + one scout subagent per repo.

Usage: triage_fanout.py [--prefer=ollama|nebius|auto] [owner/repo ...]
Defaults to arii/tech-dancer + arii/boomtick. Each scout is stateless:
complete instructions go in its single task call. auto (the default)
uses Nebius when keyed, else the local model.
"""

import os
import sys
import time

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent

import cli
import ledger
import model_router
import triage_common

FANOUT_MODEL_PIN = "nvidia/Nemotron-3_5-Lightning"
# Blank-tolerant: a set-but-empty NEBIUS_MODEL falls back to the pin
# instead of sending model='' to the provider (#620).
MODEL_ID = model_router._env("NEBIUS_MODEL", FANOUT_MODEL_PIN)
DEFAULT_REPOS = ["arii/tech-dancer", "arii/boomtick"]


@tool
def triage_counts(repo: str) -> str:
    """Get deterministic triage counts for one repository.

    Args:
        repo: Repository in owner/name format.
    """
    return triage_common.fetch_triage_counts(repo)


def extract_prefer(argv):
    """Backend override from --prefer=ollama|nebius|auto (default auto).

    Mirrors deep_chat.extract_prefer, plus the auto ladder: Nebius when
    keyed, else the default ladder (local Ollama). Returns the provider
    or "auto". Raises ValueError on a value the router would reject,
    so misuse fails at the CLI.
    """
    for arg in argv[1:]:
        if isinstance(arg, str) and arg.startswith("--prefer="):
            value = arg.split("=", 1)[1].strip().lower()
            if value not in ("ollama", "nebius", "auto"):
                raise ValueError(
                    f"bad --prefer={value!r}: want 'ollama', 'nebius', "
                    "or 'auto'")
            return value
    return "auto"


def main() -> int:
    started = time.time()
    load_dotenv("/srv/remote-server/.env")
    try:
        prefer = extract_prefer(sys.argv)
    except ValueError as exc:
        return cli.usage_error(str(exc))
    if prefer == "auto":
        # Ladder: Nebius when keyed (today's behavior), else local.
        prefer = "nebius" if os.environ.get("NEBIUS_API_KEY") else None
    keyless_auto = prefer is None
    if prefer == "nebius" and not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set", file=sys.stderr)
        return 1
    # Strip before positional parsing so the flag never shifts the repos.
    repos = [a for a in sys.argv[1:]
             if not (isinstance(a, str) and a.startswith("--prefer="))] \
        or DEFAULT_REPOS
    bad = [r for r in repos if not triage_common.valid_repo(r)]
    if bad:
        return cli.usage_error(f"bad repo(s): {bad}")
    try:
        spec = model_router.resolve_llm(prefer=prefer)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    provider = spec.get("provider")
    if provider == "ollama":
        # Local path (#622): resolved spec as-is, no Nebius pin/endpoint.
        if keyless_auto:
            print("fanout: no Nebius key — triage on local model (weaker)",
                  file=sys.stderr)
        kwargs = model_router.chat_kwargs(spec)
        # No-think default mirrors chat/coder: a qwen3 thinking trace
        # takes 15+ min per prompt on this CPU-only box.
        if os.environ.get("FANOUT_THINK", "") != "1":
            kwargs["model_kwargs"] = {"think": False}
        from langchain_ollama import ChatOllama  # noqa: E402
        model = ChatOllama(**kwargs)
        model_id = spec["model"]
    else:
        # Re-resolve after load_dotenv: the import-time MODEL_ID predates
        # the dotenv load, and a blank NEBIUS_MODEL must fall back to the
        # pin.
        model_id = model_router._env("NEBIUS_MODEL", MODEL_ID)
        if not model_id:
            print("fanout: no model resolved "
                  "(NEBIUS_MODEL is blank and no default)",
                  file=sys.stderr)
            return 1
        # Cap checked above; keep this entrypoint's pinned model/endpoint.
        spec = dict(spec, model=model_id,
                    base_url="https://api.studio.nebius.com/v1")
        kwargs = model_router.chat_kwargs(spec)
        model = ChatOpenAI(
            api_key=os.environ["NEBIUS_API_KEY"],
            max_tokens=2000,
            **kwargs,
        )
    repo_list = ", ".join(repos)
    agent = create_deep_agent(
        model=model,
        system_prompt=(
            "You coordinate per-repo triage scouts. Delegate with the "
            f"task tool: one call each for {repo_list}, each "
            "instructing the scout to return the raw triage_counts "
            "output plus one line naming the top action. Subagents are "
            "stateless: put complete instructions in each call. Then "
            "merge the reports into at most 2 lines per repo."
        ),
        subagents=[
            {
                "name": "repo_scout",
                "description": "Gather triage counts for exactly one repo",
                "system_prompt": "Call triage_counts once for the repo "
                "named in your instructions. Return its raw output, then "
                "one line TOP ACTION: <the single next action>.",
                "tools": [triage_counts],
            }
        ],
        checkpointer=MemorySaver(),
    )
    # Unique per run: the constant "fanout-run" collided across runs
    # and repos in the ledger; each process is one checkpoint sequence.
    thread = "fanout-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        result = agent.invoke(
            {
                "messages": [
                    {"role": "user", "content": f"Triage {repo_list}."}
                ]
            },
            config={"configurable": {"thread_id": thread}},
        )
    except Exception as exc:
        # Provider 4xx/5xx (e.g. unknown model) surface as one line,
        # never a LangChain traceback (#620).
        code = getattr(exc, "status_code", None)
        detail = str(exc).strip().split("\n", 1)[0] or type(exc).__name__
        if code is not None:
            label = "Nebius" if provider == "nebius" else "Ollama"
            print(f"fanout: {label} error {code}: {detail}",
                  file=sys.stderr)
        else:
            print(f"fanout: error: {detail}", file=sys.stderr)
        return 1
    tasks = triage_common.count_tool_calls(result["messages"], "task")
    ledger.record(
        "triage_fanout.py",
        f"{provider}:{model_id}",
        thread,
        result["messages"],
        started,
        extra={"task_delegations": tasks, "repos": repos},
    )
    print(f"task_delegations: {tasks}")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

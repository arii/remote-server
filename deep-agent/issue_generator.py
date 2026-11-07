"""IssueGeneratorGraph: closed-loop spec agent (deterministic + LLM draft).

Pipeline: brief -> td init -> draft (LLM) -> investigate (LLM + tools)
-> Q&A (human-in-the-loop interrupt) -> td validate-issue gate ->
persist to decisions.py + ledger.py. Invalid specs loop back to the
draft node with exact td errors; exhausted loops fail closed.

Only stdlib imports at module top so unit tests run anywhere;
langgraph/langchain/ollama imports are lazy (deep-agent venv).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

import skills_registry

REQUIRED_SECTIONS = (
    "Problem Statement",
    "Goal",
    "Scope",
    "Definition of Done",
)

MAX_LOOPS_DEFAULT = 5

# Sanctioned Ollama endpoint (issue #1085): OLLAMA_URL override, else
# the compose-network host. Never raw localhost — unreachable inside
# containers, which is exactly how the spec agent died silently.
COMPOSE_OLLAMA_URL = "http://ollama:11434"

_REPO_FILE_RE = re.compile(r"(?<![\w/])([\w./-]+\.\w+)(?::\d+)?")


def _ollama_base_url():
    """Resolve the sanctioned Ollama endpoint (issue #1085)."""
    try:
        import model_router
        default = model_router.DEFAULT_OLLAMA_URL
    except Exception:
        default = COMPOSE_OLLAMA_URL
    url = os.environ.get("OLLAMA_URL", default).strip().rstrip("/") or default
    return url


def evidence_cites_repo_file(evidence, repo_root):
    """Repo-relative paths in evidence that exist on disk (issue #1053).

    The on-brief guard: a run whose investigation cites no real repo
    file is off-brief and must fail loud, never file an ungrounded
    spec with exit 0.
    """
    hits = []
    for match in _REPO_FILE_RE.finditer(evidence or ""):
        rel = match.group(1).lstrip("./")
        if not rel or rel.startswith(".") or ".." in rel.split("/"):
            continue
        if os.path.isfile(os.path.join(repo_root, rel)) and rel not in hits:
            hits.append(rel)
    return hits


def assemble_spec(brief, context="", answers=None):
    """Render the canonical spec markdown from brief + context + Q&A."""
    answers = answers or {}
    lines = [f"# {brief.strip()}", ""]
    lines += ["## Problem Statement", (context or "").strip() or brief.strip(), ""]
    lines += ["## Goal", brief.strip(), ""]
    lines += ["## Scope", "Touched files: TBD during investigation.", ""]
    lines += ["## Definition of Done", "Acceptance criteria below hold.", ""]
    lines += ["## Scope & File Boundaries", "### Allowed Files",
              "- TBD", "### Forbidden Files", "- TBD", ""]
    lines += ["## Implementation Plan", "1. TBD", ""]
    lines += ["## Acceptance Criteria", "- [ ] TBD", ""]
    if answers:
        lines += ["## Q&A Context", ""]
        for key in sorted(answers):
            lines.append(f"- **{key}**: {answers[key]}")
        lines.append("")
    lines += ["## Verification Commands", "```bash", "# TBD", "```", ""]
    return "\n".join(lines)


def extract_questions(text):
    """Numbered/bulleted questions the draft asks the human."""
    questions = []
    for line in (text or "").splitlines():
        match = re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.+\?)\s*$", line)
        if match:
            question = match.group(1).strip()
            if question not in questions:
                questions.append(question)
    return questions


def _run_td(args, timeout=180):
    """Run td, return (returncode, stdout). FileNotFoundError if missing."""
    proc = subprocess.run(
        ["td"] + args, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip()


class IssueGeneratorGraph:
    """Closed-loop spec generator. Inject fakes for deterministic tests.

    model_fn: () -> chat model (default model_router.build_chat_model
      with think=False, backend from S5_ISSUE_PROVIDER or Ollama).
    investigate_fn(messages, tools) -> reply text (default
      langchain create_agent run).
    td_runner: (args) -> (returncode, stdout) (default _run_td).
    """

    def __init__(self, repo_root=None, model_fn=None, investigate_fn=None,
                 td_runner=None, max_loops=MAX_LOOPS_DEFAULT,
                 thread_id=None):
        self.repo_root = os.path.abspath(repo_root or os.getcwd())
        self.model_fn = model_fn
        self.investigate_fn = investigate_fn
        self.td_runner = td_runner or _run_td
        self.max_loops = max_loops
        self.thread_id = thread_id or f"issuegen-{int(time.time())}"
        self.last_spec = None

    # -- deterministic pieces (pure, unit-tested) --------------------

    def td_init(self):
        """Run td init; return (ok, payload-or-error)."""
        try:
            code, out = self.td_runner(["init", "--json"])
        except FileNotFoundError:
            return False, "td CLI not found on PATH"
        except Exception as exc:
            return False, f"td init failed: {exc}"
        try:
            payload = json.loads(out) if out else {}
        except ValueError:
            return False, f"td init returned non-JSON: {out[:200]}"
        if code != 0 or payload.get("status") not in ("ready", "success"):
            return False, (payload.get("message")
                           or out[:300] or f"exit {code}")
        return True, payload

    def validate_spec(self, spec_text):
        """Validate via td validate-issue; return (valid, data, errors)."""
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8")
        try:
            handle.write(spec_text)
            handle.close()
            try:
                code, out = self.td_runner(
                    ["validate-issue", "--file", handle.name,
                     "--repo-root", self.repo_root, "--json"])
            except FileNotFoundError:
                return False, {}, ["td CLI not found on PATH"]
            try:
                data = json.loads(out) if out else {}
            except ValueError:
                return False, {}, [f"td returned non-JSON: {out[:200]}"]
            errors = list(data.get("errors") or [])
            if code != 0 and not errors:
                errors = [(data.get("message") or out[:300]
                           or f"exit {code}")]
            return bool(data.get("valid")), data, errors
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    # -- LLM pieces (injected in tests) -------------------------------

    def _model(self):
        if self.model_fn is not None:
            return self.model_fn()
        import model_router
        prefer = os.environ.get("S5_ISSUE_PROVIDER", "").strip() or None
        spec = model_router.resolve_llm(prefer=prefer)
        # Retain the resolved backend: ledger rows record the model
        # that actually ran, not the S5_ISSUE_MODEL default (issue
        # #1121). Injected model_fn (tests) leaves this unset.
        self.last_spec = dict(spec)
        model, _ = model_router.build_chat_model(spec=spec, think=False)
        return model

    def check_backend(self, timeout=5):
        """Probe the resolved model backend (issue #1085).

        Returns (ok, message). Injected model_fn skips the probe.
        The backend comes from model_router.resolve_llm() honoring
        S5_ISSUE_PROVIDER: an Ollama spec probes the socket as before,
        a Nebius spec only needs NEBIUS_API_KEY (no socket to probe).
        A miss names the sanctioned options instead of dying later
        with a bare ConnectError. Cloud fallback is deliberately NOT
        automatic: never spend metered money without being asked.
        """
        if self.model_fn is not None:
            return True, "injected model (probe skipped)"
        import model_router
        prefer = os.environ.get("S5_ISSUE_PROVIDER", "").strip() or None
        try:
            spec = model_router.resolve_llm(prefer=prefer)
        except (RuntimeError, ValueError) as exc:
            return False, (
                f"STUCK: no usable model backend ({exc}). Set OLLAMA_URL "
                "to a host serving Ollama (compose network: "
                "http://ollama:11434), or set S5_ISSUE_PROVIDER=nebius "
                "with NEBIUS_API_KEY for the metered fallback.")
        if spec.get("provider") == "nebius":
            if not os.environ.get("NEBIUS_API_KEY", "").strip():
                return False, (
                    "STUCK: Nebius backend resolved but NEBIUS_API_KEY "
                    "is unset; set it (and S5_ISSUE_MODEL if the default "
                    "model is wrong) or unset S5_ISSUE_PROVIDER.")
            return True, (
                f"Nebius backend selected ({spec.get('model', '?')})")
        url = _ollama_base_url()
        host_port = url.split("://", 1)[-1].rstrip("/")
        host, _, port_s = host_port.partition(":")
        try:
            port = int(port_s or 11434)
        except ValueError:
            return False, (
                f"STUCK: bad port in OLLAMA_URL {url!r}; set "
                "OLLAMA_URL to a host:port serving Ollama "
                "(compose network: http://ollama:11434).")
        import socket
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, f"Ollama reachable at {url}"
        except OSError as exc:
            return False, (
                f"STUCK: model backend unreachable at {url} ({exc}). "
                "Set OLLAMA_URL to a host serving Ollama "
                "(compose network: http://ollama:11434), or run where "
                "Ollama serves. Cloud fallback (NEBIUS_*) is available "
                "but never automatic.")

    def _investigator_tools(self):
        import ask_investigator
        return [ask_investigator.search_docs,
                ask_investigator.read_repo_file,
                ask_investigator.grep_repo]

    def draft_text(self, brief, context, errors, answers):
        """One draft pass (LLM). Tests inject model_fn."""
        model = self._model()
        prompt = (
            "Write a design spec for this task. Follow the repo spec "
            "format exactly (Problem Statement, Goal, Scope, "
            "Definition of Done, Scope & File Boundaries with Allowed "
            "Files, Implementation Plan, Acceptance Criteria checklist, "
            "Verification Commands fenced block).\n"
            "First step, before any other action: read one repo file "
            "relevant to the task and quote it — a spec that cites no "
            "repo file is rejected outright. Ask any blocking "
            "questions as a trailing bulleted list of questions.\n\n"
            f"Task: {brief}\n\nContext:\n{context}\n")
        if errors:
            prompt += "\nPrevious validation errors to fix:\n" + "\n".join(
                f"- {e}" for e in errors) + "\n"
        if answers:
            prompt += "\nHuman answers:\n" + "\n".join(
                f"- {k}: {v}" for k, v in sorted(answers.items())) + "\n"
        prompt = skills_registry.SkillsRegistry().inject_skills_for_node(
            "draft_spec_node", prompt)
        reply = model.invoke(prompt)
        return getattr(reply, "content", reply) if not isinstance(
            reply, str) else reply

    def investigate_text(self, brief, draft):
        """One investigate pass (LLM + tools). Tests inject this."""
        if self.investigate_fn is not None:
            return self.investigate_fn(brief, draft)
        from langchain.agents import create_agent
        agent = create_agent(self._model(), self._investigator_tools())
        result = agent.invoke(
            {"messages": [{"role": "user",
                           "content": f"Gather evidence for: {brief}\n\n"
                                      f"Draft so far:\n{draft}\n\n"
                                      "Cite every factual claim with a "
                                      "repo file path (path:line). If you "
                                      "cannot read any repo file, say so "
                                      "explicitly instead of inventing "
                                      "content."}]})
        messages = result.get("messages", []) if isinstance(
            result, dict) else []
        texts = [getattr(m, "content", "") or "" for m in messages]
        return "\n\n".join(t for t in texts if t) or draft

    # -- graph ---------------------------------------------------------

    def build_graph(self):
        """Compile the closed-loop graph (needs langgraph)."""
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt
        from typing_extensions import TypedDict

        if getattr(self, "_checkpointer", None) is None:
            # One saver per instance: resume must hit the same
            # checkpoint store as the paused run.
            self._checkpointer = InMemorySaver()

        outer = self

        class GenState(TypedDict, total=False):
            brief: str
            context: str
            draft: str
            evidence: str
            off_brief: bool
            questions: list
            answers: dict
            errors: list
            attempts: int
            valid: bool
            validation: dict
            persisted: bool

        def draft_spec_node(state):
            if state.get("draft") and not state.get("errors"):
                return {}
            ok, _ = outer.td_init()
            if not ok:
                return {"errors": ["td init failed"],
                        "attempts": state.get("attempts", 0) + 1}
            draft = outer.draft_text(
                state.get("brief", ""), state.get("context", ""),
                state.get("errors", []), state.get("answers", {}))
            return {"draft": draft,
                    "attempts": state.get("attempts", 0) + 1}

        def investigate_node(state):
            if state.get("evidence") and not state.get("errors"):
                return {}
            evidence = outer.investigate_text(
                state.get("brief", ""), state.get("draft", ""))
            if not evidence_cites_repo_file(evidence, outer.repo_root):
                return {"evidence": evidence,
                        "errors": ["investigation cited no repo file "
                                   "that exists on disk — agent "
                                   "off-brief, failing loud instead of "
                                   "filing an ungrounded spec"],
                        "off_brief": True}
            return {"evidence": evidence}

        def qa_loop_node(state):
            answers = dict(state.get("answers", {}))
            questions = list(state.get("questions", []) or
                             extract_questions(state.get("draft", "")))
            missing = [q for q in questions if q not in answers]
            if not missing:
                return {"questions": questions, "answers": answers}
            reply = interrupt({"questions": missing})
            if isinstance(reply, dict):
                answers.update(reply)
            elif isinstance(reply, str):
                try:
                    answers.update(json.loads(reply))
                except ValueError:
                    pass
            still_missing = [q for q in questions if q not in answers]
            if still_missing:
                return {"questions": questions, "answers": answers}
            return {"questions": questions, "answers": answers}

        def validate_spec_node(state):
            if state.get("off_brief"):
                return {"valid": False,
                        "validation": state.get("validation", {}),
                        "errors": state.get("errors", [])}
            spec_text = state.get("draft", "")
            if state.get("evidence"):
                spec_text += "\n\n## Investigation Evidence\n" + state[
                    "evidence"]
            if state.get("answers"):
                spec_text += "\n\n## Q&A Context\n" + "\n".join(
                    f"- **{k}**: {v}"
                    for k, v in sorted(state["answers"].items()))
            valid, data, errors = outer.validate_spec(spec_text)
            update = {"valid": valid, "validation": data,
                      "errors": errors if not valid else []}
            if not valid:
                return update
            return update

        def should_redraft(state):
            if state.get("valid"):
                return "persist"
            if state.get("off_brief"):
                return END
            if state.get("attempts", 0) >= outer.max_loops:
                return END
            return "draft"

        def persist_decision_node(state):
            import decisions
            import ledger
            calls = [{"name": "td init", "args": ""},
                     {"name": "td validate-issue",
                      "args": f"attempts={state.get('attempts', 0)}"}]
            results = [f"valid={state.get('valid')}"]
            reply = (state.get("draft", "")[:500]
                     if state.get("valid") else "")
            lines = decisions.build_decisions(
                calls, results, reply,
                reason="issue spec closed loop")
            spec = getattr(outer, "last_spec", None) or {}
            ledger.record(
                "issue_generator.py",
                spec.get("model") or os.environ.get(
                    "S5_ISSUE_MODEL", "qwen3:8b"),
                outer.thread_id, [],
                time.time(),
                extra={"valid": state.get("valid"),
                       "attempts": state.get("attempts", 0),
                       "decisions": lines,
                       "validation": state.get("validation", {})},
                provider=spec.get("provider"))
            return {"persisted": True}

        builder = StateGraph(GenState)
        builder.add_node("draft", draft_spec_node)
        builder.add_node("investigate", investigate_node)
        builder.add_node("qa_loop", qa_loop_node)
        builder.add_node("validate", validate_spec_node)
        builder.add_node("persist", persist_decision_node)
        builder.add_edge(START, "draft")
        builder.add_edge("draft", "investigate")
        builder.add_edge("investigate", "qa_loop")
        builder.add_edge("qa_loop", "validate")
        builder.add_conditional_edges("validate", should_redraft,
                                      {"draft": "draft",
                                       "persist": "persist",
                                       END: END})
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=self._checkpointer)

    def run(self, brief, context="", answers=None, resume=None):
        """Invoke to completion (or interrupt); resume via Command."""
        from langgraph.types import Command
        ok, message = self.check_backend()
        if not ok:
            raise RuntimeError(message)
        graph = self.build_graph()
        config = {"configurable": {"thread_id": self.thread_id}}
        if resume is None:
            return graph.invoke(
                {"brief": brief, "context": context,
                 "answers": dict(answers or {}), "errors": [],
                 "attempts": 0, "valid": False},
                config)
        return graph.invoke(Command(resume=resume), config)


def main(argv=None):
    """CLI: --spec-file <path|-> [--qna-file <path>] [--max-loops N]."""
    import cli as cli_helpers
    args = list(argv if argv is not None else sys.argv[1:])
    spec_file = None
    qna_file = None
    max_loops = MAX_LOOPS_DEFAULT
    repo_root = None
    i = 0
    while i < len(args):
        if args[i] == "--spec-file" and i + 1 < len(args):
            spec_file, i = args[i + 1], i + 2
        elif args[i] == "--qna-file" and i + 1 < len(args):
            qna_file, i = args[i + 1], i + 2
        elif args[i] == "--max-loops" and i + 1 < len(args):
            try:
                max_loops = int(args[i + 1])
            except ValueError:
                return cli_helpers.usage_error(
                    "spec-file mode: --max-loops N:",
                    "usage: issue_generator.py --spec-file <path|->"
                    " [--qna-file <path>] [--max-loops N]")
            i += 2
        elif args[i] == "--repo-root" and i + 1 < len(args):
            repo_root, i = args[i + 1], i + 2
        else:
            return cli_helpers.usage_error(
                f"spec-file mode: unknown arg: {args[i]}",
                "usage: issue_generator.py --spec-file <path|->"
                " [--qna-file <path>] [--max-loops N]")
    if not spec_file:
        return cli_helpers.usage_error(
            "spec-file mode: --spec-file is required:",
            "usage: issue_generator.py --spec-file <path|->"
            " [--qna-file <path>] [--max-loops N]")
    if spec_file == "-":
        brief = sys.stdin.read()
    else:
        try:
            with open(spec_file, encoding="utf-8") as handle:
                brief = handle.read()
        except OSError as exc:
            return cli_helpers.sorry(f"Sorry, cannot read spec: {exc}")
    if not brief.strip():
        return cli_helpers.sorry("Sorry, empty brief, nothing to do.")
    gen = IssueGeneratorGraph(repo_root=repo_root, max_loops=max_loops)
    try:
        from langgraph.types import Command
        state = gen.run(brief)
        rounds = 0
        while "__interrupt__" in state and rounds < max_loops + 1:
            payload = state["__interrupt__"][0].value
            questions = (payload.get("questions")
                         if isinstance(payload, dict) else [])
            if not qna_file:
                print(json.dumps({"questions": questions}))
                return 2
            try:
                with open(qna_file, encoding="utf-8") as handle:
                    resume = json.load(handle)
            except (OSError, ValueError) as exc:
                return cli_helpers.sorry(
                    f"Sorry, cannot read Q&A file: {exc}")
            state = gen.run(brief, resume=resume)
            rounds += 1
    except ImportError:
        return cli_helpers.sorry(
            "Sorry, the agent runtime is unavailable here "
            "(deep-agent venv with langgraph is required).")
    except RuntimeError as exc:
        return cli_helpers.sorry(f"Sorry, {exc}")
    if state.get("valid"):
        print(json.dumps({"status": "ready", "valid": True,
                          "attempts": state.get("attempts", 0)}))
        return 0
    for error in state.get("errors", []):
        print(f"Invalid spec: {error}")
    try:
        import ledger as _ledger
        _spec = getattr(gen, "last_spec", None) or {}
        _ledger.record(
            "issue_generator.py",
            _spec.get("model") or os.environ.get(
                "S5_ISSUE_MODEL", "qwen3:8b"),
            gen.thread_id, [], time.time(),
            extra={"valid": False,
                   "attempts": state.get("attempts", 0),
                   "errors": state.get("errors", [])},
            provider=_spec.get("provider"))
    except ImportError:
        pass
    if "__interrupt__" in state:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

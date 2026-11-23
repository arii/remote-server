"""IssueGeneratorGraph: closed-loop spec agent (deterministic + LLM draft).

Pipeline: brief -> td init -> draft (LLM) -> investigate (LLM + tools)
-> Q&A (human-in-the-loop interrupt) -> td validate-issue gate ->
persist to decisions.py + ledger.py. Invalid specs loop back to the
draft node with exact td errors; exhausted loops fail closed.

Spec generation is strictly bounded (issue #1404): the draft opens
with a deterministically gathered context pack, investigation runs
under a hard model-turn and tool-call budget with tool bindings
stripped on the forced final turn, and tool outputs are head/tail
truncated so exploration cannot bloat the prompt.

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
from pathlib import Path
from threading import Lock

import skills_registry
import literal_checks
from coder import DEFAULT_TOOL_OUTPUT_LIMIT, _truncate_tool_output, plan_path_hints
from backend.shared.s5tools import active_checkout_path, verification_guidance

REQUIRED_SECTIONS = (
    "Problem Statement",
    "Goal",
    "Scope",
    "Definition of Done",
)

MAX_LOOPS_DEFAULT = 5

# Bounded spec generation (issue #1404).
SPEC_MAX_TURNS_DEFAULT = 4
SPEC_MAX_TOOL_CALLS_DEFAULT = 3
PRESEED_CONTEXT_LIMIT = 6000
PRESEED_SNIPPET_LIMIT = 1500
SPEC_TOOL_OUTPUT_LIMIT = DEFAULT_TOOL_OUTPUT_LIMIT
_URL_RE = re.compile(
    r"https?://[^\s`<>()[\]]+|(?<![\w:/])//[^\s`<>()[\]]+", re.IGNORECASE
)


def clean_allowed_file_urls(text):
    lines = []
    allowed = False
    for line in text.splitlines(keepends=True):
        heading = re.match(r"^\s*#{1,6}\s+(.+)", line)
        if heading:
            allowed = heading[1].lower().startswith(("allowed files", "allowed paths"))
        elif allowed and _URL_RE.search(line):
            line = _URL_RE.sub("", line)
            line = re.sub(r"\[[^\]]*\]\(\s*\)", "", line)
            line = re.sub(r"`{2,}", "", line)
            if not line.strip(" \t\r\n-*+`[]()"):
                continue
        lines.append(line)
    return "".join(lines)


def allowed_file_entries(text):
    paths = {}
    allowed = False
    for line in clean_allowed_file_urls(text).splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.+)", line)
        if heading:
            allowed = heading[1].lower().startswith(("allowed files", "allowed paths"))
        elif allowed:
            match = re.match(r"^\s*[-*+]\s+`?([^`\s]+)`?(.*)", line)
            if match:
                path = match[1]
                while path.startswith("./"):
                    path = path[2:]
                if "/" in path or "." in path:
                    create = bool(re.search(r"\(CREATE\)", match[2], re.I))
                    paths[path] = paths.get(path, False) or create
    return paths


def explicit_allowed_paths(text):
    return list(allowed_file_entries(text))


class InvestigationTruncated(RuntimeError):
    pass


def investigation_tool_guard(results=None, max_calls=None, budget=None):
    """Repeat-call guard + hard tool budget + output truncation (#1404).

    max_calls bounds executed tool calls. Once the budget is spent,
    further calls short-circuit with an error ToolMessage and
    budget['exhausted'] flips, so force_final_turn strips the tool
    bindings on the next model turn and forces the final answer.
    """
    from langchain.agents.middleware import wrap_tool_call
    from langchain_core.messages import ToolMessage

    seen = set()
    repeats = 0
    calls = 0
    lock = Lock()

    @wrap_tool_call
    def stop_repeated_call(request, handler):
        nonlocal repeats, calls
        call = request.tool_call
        key = (call["name"], json.dumps(call["args"], sort_keys=True))
        with lock:
            if key in seen:
                # A repeat loop means the model is stuck, not merely
                # over budget: raise even once the budget is spent.
                repeats += 1
                if repeats >= 3:
                    raise InvestigationTruncated(
                        "Repeated investigation tool call limit reached (3 repeats): "
                        f"{key[0]} {key[1]}"
                    )
                return ToolMessage(
                    content="Already called with these args; result was shown earlier "
                    "— use it or call a different tool.",
                    tool_call_id=call["id"],
                    status="error",
                )
            if max_calls is not None and calls >= max_calls:
                # Only executed calls spend the budget; beyond it,
                # distinct new calls short-circuit with an error.
                if budget is not None:
                    budget["exhausted"] = True
                return ToolMessage(
                    content=(
                        f"Spec investigation tool budget exhausted "
                        f"({max_calls} calls) — answer from the "
                        "evidence already gathered."
                    ),
                    tool_call_id=call["id"],
                    status="error",
                )
            calls += 1
            if max_calls is not None and calls >= max_calls and budget is not None:
                budget["exhausted"] = True
            seen.add(key)
        result = handler(request)
        content = getattr(result, "content", result)
        # Output truncation ships with the spec budget wiring (#1404);
        # un-budgeted guards (auditor.py) keep their exact old behavior.
        if max_calls is not None and isinstance(content, str) and content:
            truncated = _truncate_tool_output(content, SPEC_TOOL_OUTPUT_LIMIT)
            if truncated != content and isinstance(result, ToolMessage):
                result = ToolMessage(
                    content=truncated,
                    tool_call_id=call["id"],
                    name=call["name"],
                    status=getattr(result, "status", "success"),
                )
                content = truncated
        if (
            results is not None
            and str(content).strip()
            and getattr(result, "status", "") != "error"
        ):
            with lock:
                results.append(f"{key[0]} {key[1]}: {str(content)[:500]}")
        return result

    return stop_repeated_call


def force_final_turn(budget):
    """wrap_model_call: strip tool bindings once the budget is spent.

    The model turn after the last allowed tool call must emit the
    final structured answer instead of starting another exploration
    loop (issue #1404).
    """
    from langchain.agents.middleware import wrap_model_call

    @wrap_model_call
    def strip_spent_tools(request, handler):
        if budget.get("exhausted"):
            from langchain_core.messages import HumanMessage

            request = request.override(
                tools=[],
                tool_choice="none",
                messages=[
                    *request.messages,
                    HumanMessage(
                        content="The investigation tool budget is exhausted. "
                        "Write the final evidence summary now, citing each factual "
                        "claim with a repo file path:line from the pre-seeded "
                        "context or tool results already gathered. No tools are "
                        "available: do not request tools or emit <tool_call> markup. "
                        "If no repository evidence was gathered, say so explicitly "
                        "instead of inventing citations."
                    ),
                ],
            )
        return handler(request)

    return strip_spent_tools


def completed_spec(state, allowed_path=None):
    parts = [state.get("draft", "")]
    if state.get("evidence"):
        parts.append("## Investigation Evidence\n" + state["evidence"])
    if state.get("answers"):
        parts.append(
            "## Q&A Context\n"
            + "\n".join(f"- **{k}**: {v}" for k, v in sorted(state["answers"].items()))
        )
    text = clean_allowed_file_urls(
        "\n\n".join(part for part in parts if part).rstrip() + "\n"
    )
    if allowed_path and allowed_path not in explicit_allowed_paths(text):
        entry = f"- `{allowed_path}` (new)\n"
        text, count = re.subn(
            r"(?im)^(\s*#{1,6}\s+Allowed (?:Files|Paths)[^\n]*\n)",
            lambda match: match[0] + entry,
            text,
            count=1,
        )
        if not count:
            text += "\n## Scope\n### Allowed Files\n" + entry
    return text


def write_spec(path, text):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# Sanctioned Ollama endpoint (issue #1085): OLLAMA_URL override, else
# the compose-network host. Never raw localhost — unreachable inside
# containers, which is exactly how the spec agent died silently.
COMPOSE_OLLAMA_URL = "http://ollama:11434"

_REPO_FILE_RE = re.compile(r"(?<![\w/])([\w./-]+\.\w+)(?::\d+)?")
_TOOL_CALL_MARKUP_RE = re.compile(
    r"<tool_call\b[^>]*>.*?</tool_call>", re.DOTALL | re.IGNORECASE
)


def _env_positive_int(name, default):
    """Positive-int env override, or the default when unset/blank."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _ollama_base_url():
    """Resolve the sanctioned Ollama endpoint (issue #1085)."""
    try:
        from backend.shared import model_router

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
        ["td"] + args, capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout or "").strip()


class IssueGeneratorGraph:
    """Closed-loop spec generator. Inject fakes for deterministic tests.

    model_fn: () -> chat model (default model_router.build_chat_model
      with think=False, backend from S5_ISSUE_PROVIDER or Ollama).
    investigate_fn(messages, tools) -> reply text (default
      langchain create_agent run).
    td_runner: (args) -> (returncode, stdout) (default _run_td).
    """

    def __init__(
        self,
        repo_root=None,
        model_fn=None,
        investigate_fn=None,
        td_runner=None,
        max_loops=MAX_LOOPS_DEFAULT,
        thread_id=None,
        event_fn=None,
        max_spec_turns=None,
        max_spec_tool_calls=None,
        preseed_fn=None,
        spec_path=None,
    ):
        self.repo_root = os.path.abspath(repo_root or os.getcwd())
        self.spec_path = spec_path
        self.model_fn = model_fn
        self.investigate_fn = investigate_fn
        self.td_runner = td_runner or _run_td
        self.max_loops = max_loops
        self.thread_id = thread_id or f"issuegen-{int(time.time())}"
        self.last_spec = None
        self.event_fn = event_fn
        self.investigation_truncated = False
        # Bounded spec generation (issue #1404).
        self.max_spec_turns = self._bound_value(
            "S5_SPEC_MAX_TURNS", max_spec_turns, SPEC_MAX_TURNS_DEFAULT
        )
        self.max_spec_tool_calls = self._bound_value(
            "S5_SPEC_MAX_TOOL_CALLS", max_spec_tool_calls, SPEC_MAX_TOOL_CALLS_DEFAULT
        )
        self._preseed_fn = preseed_fn
        self._preseed_cache = {}

    @staticmethod
    def _bound_value(env_name, explicit, default):
        """Explicit arg > env override > default; always a positive int."""
        if explicit is not None:
            if (
                not isinstance(explicit, int)
                or isinstance(explicit, bool)
                or explicit <= 0
            ):
                raise ValueError(f"{env_name} must be a positive integer")
            return explicit
        return _env_positive_int(env_name, default)

    def _spec_recursion_limit(self):
        """Graph-step cap: explicit S5_SPEC_RECURSION_LIMIT wins, else
        derive from the turn budget (2 steps per model turn + 1) (#1404)."""
        return _env_positive_int("S5_SPEC_RECURSION_LIMIT", 2 * self.max_spec_turns + 1)

    def preseed_context_pack(self, brief):
        """Deterministic context pack for the first spec turn (#1404).

        Repo tree slice, brief-named file snippets, retrieval.search_td
        hits, and recent commits for the named files, all gathered in
        Python before the first LLM turn so scoped briefs resolve
        without exploration turns. Bounded to PRESEED_CONTEXT_LIMIT.
        Never raises: each source degrades to an explanatory line.
        """
        if self._preseed_fn is not None:
            return self._preseed_fn(brief)
        if brief in self._preseed_cache:
            return self._preseed_cache[brief]
        parts = []
        candidates = {}
        for rel in evidence_cites_repo_file(brief, self.repo_root):
            try:
                candidates[rel] = active_checkout_path(self.repo_root, rel)
            except ValueError:
                continue
            if len(candidates) == 3:
                break
        try:
            top = sorted(os.listdir(self.repo_root))
            top = [e for e in top if e not in {".git", ".worktrees"}][:40]
            parts.append("Repo top level: " + ", ".join(top))
        except OSError as exc:
            parts.append(f"Repo tree unavailable: {exc}")
        for rel, path in candidates.items():
            try:
                with open(
                    path,
                    encoding="utf-8",
                    errors="replace",
                ) as handle:
                    snippet = handle.read(2 * PRESEED_SNIPPET_LIMIT)
                parts.append(
                    f"--- {rel} (excerpt) ---\n"
                    + _truncate_tool_output(snippet, PRESEED_SNIPPET_LIMIT)
                )
            except OSError as exc:
                parts.append(f"--- {rel} unreadable: {exc}")
        try:
            import retrieval

            parts.append(
                "Related context (search):\n"
                + retrieval.search_td(brief, self.repo_root, top_k=4)
            )
        except Exception as exc:
            parts.append(f"Related context search unavailable: {exc}")
        if candidates:
            try:
                proc = subprocess.run(
                    ["git", "-C", self.repo_root, "log", "--oneline", "-5", "--"]
                    + list(candidates),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                log = (proc.stdout or "").strip()
                if proc.returncode == 0 and log:
                    parts.append("Recent related commits:\n" + log)
            except (OSError, subprocess.SubprocessError):
                pass
        pack = _truncate_tool_output(
            "\n\n".join(part for part in parts if part).strip(),
            PRESEED_CONTEXT_LIMIT,
        )
        self._preseed_cache[brief] = pack
        return pack

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
            return False, (payload.get("message") or out[:300] or f"exit {code}")
        return True, payload

    def has_implementation_path(self, entries):
        existing = []
        for path, create in entries.items():
            normalized = os.path.normpath(path)
            artifact = (
                normalized == self.spec_path
                if self.spec_path is not None
                else re.fullmatch(r"docs/specs/issue-\d+\.md", normalized)
            )
            if artifact or path.startswith("/") or ".." in path.split("/"):
                continue
            try:
                target = Path(active_checkout_path(self.repo_root, path))
            except ValueError:
                continue
            if target.is_file():
                existing.append(path)
            elif create and not os.path.lexists(target):
                return True
        if not existing:
            return False
        result = subprocess.run(
            [
                "git",
                "-C",
                self.repo_root,
                "--literal-pathspecs",
                "ls-files",
                "-z",
                "--",
                *existing,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and not set(existing).isdisjoint(
            result.stdout.split("\0")
        )

    def validate_spec(self, spec_text):
        """Validate citation-free scope text, preserving commands and stored links."""
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        )
        try:
            in_fence = False
            for line in clean_allowed_file_urls(spec_text).splitlines(keepends=True):
                if line.strip().startswith("```"):
                    in_fence = not in_fence
                handle.write(
                    line
                    if in_fence
                    else re.sub(
                        r"(?<![\w./])(?:\./)+(?=[\w])", "", _URL_RE.sub("", line)
                    )
                )
            handle.close()
            try:
                code, out = self.td_runner(
                    [
                        "validate-issue",
                        "--file",
                        handle.name,
                        "--repo-root",
                        self.repo_root,
                        "--json",
                    ]
                )
            except FileNotFoundError:
                return False, {}, ["td CLI not found on PATH"]
            try:
                data = json.loads(out) if out else {}
            except ValueError:
                return False, {}, [f"td returned non-JSON: {out[:200]}"]
            errors = list(data.get("errors") or [])
            if code != 0 and not errors:
                errors = [(data.get("message") or out[:300] or f"exit {code}")]
            entries = allowed_file_entries(spec_text)
            paths = list(entries)
            scope_error = re.compile(
                r"^allowed path not in checkout: (.+?) \(mark with"
            )
            original_errors = list(errors)
            errors = [
                error
                for error in errors
                if not (match := scope_error.match(error)) or match[1] in paths
            ]
            for path in paths:
                if path.startswith("/") or ".." in path.split("/"):
                    errors.append(f"allowed path must be repo-relative: {path}")
            # Literal contradiction check (issue #1375): a spec whose
            # instructions retain a string its acceptance criteria
            # forbid must redraft before coding, not code the conflict.
            errors.extend(literal_checks.spec_contradictions(spec_text))
            if not errors and not self.has_implementation_path(entries):
                errors.append(
                    "Allowed Files must contain at least one implementation path, "
                    "not the generated spec itself. Use one Markdown bullet per "
                    "repo-relative file (not a table); choose an existing tracked "
                    "file or mark an intended new file (CREATE)."
                )
            valid = bool(data.get("valid") or original_errors) and not errors
            data.update(allowed_files=paths, valid=valid, errors=errors)
            return valid, data, errors
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

    # -- LLM pieces (injected in tests) -------------------------------

    def _usage_callback(self, phase):
        spec = dict(getattr(self, "last_spec", None) or {})
        if not spec:
            return None
        from llm_usage import usage_callback

        return usage_callback(
            spec, self.thread_id, script="issue_generator.py", stage="spec", phase=phase
        )

    def _model(self):
        if self.model_fn is not None:
            return self.model_fn()
        from backend.shared import model_router

        prefer = os.environ.get("S5_ISSUE_PROVIDER", "").strip() or None
        spec = model_router.resolve_llm(prefer=prefer)
        # Retain the resolved backend: ledger rows record the model
        # that actually ran, not the S5_ISSUE_MODEL default (issue
        # #1121). Injected model_fn (tests) leaves this unset.
        model, resolved = model_router.build_chat_model(
            spec=spec, think=False, stage="spec"
        )
        self.last_spec = dict(resolved)
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
        from backend.shared import model_router

        prefer = os.environ.get("S5_ISSUE_PROVIDER", "").strip() or None
        try:
            spec = model_router.resolve_llm(prefer=prefer)
        except (RuntimeError, ValueError) as exc:
            return False, (
                f"STUCK: no usable model backend ({exc}). Set OLLAMA_URL "
                "to a host serving Ollama (compose network: "
                "http://ollama:11434), or set S5_ISSUE_PROVIDER=nebius "
                "with NEBIUS_API_KEY for the metered fallback."
            )
        if spec.get("provider") in ("nebius", "openrouter", "twt"):
            try:
                model_router.api_key(spec)
            except RuntimeError as exc:
                return False, f"STUCK: {exc}"
            return True, (
                f"{spec['provider'].title()} backend selected ({spec.get('model', '?')})"
            )
        url = _ollama_base_url()
        host_port = url.split("://", 1)[-1].rstrip("/")
        host, _, port_s = host_port.partition(":")
        try:
            port = int(port_s or 11434)
        except ValueError:
            return False, (
                f"STUCK: bad port in OLLAMA_URL {url!r}; set "
                "OLLAMA_URL to a host:port serving Ollama "
                "(compose network: http://ollama:11434)."
            )
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
                "but never automatic."
            )

    def _investigator_tools(self):
        import ask_investigator

        return [
            ask_investigator.search_docs,
            ask_investigator.read_repo_file,
            ask_investigator.grep_repo,
        ]

    def draft_text(self, brief, context, errors, answers):
        """One draft pass (LLM). Tests inject model_fn."""
        model = self._model()
        tracked_paths = plan_path_hints(self.repo_root, brief)
        prompt = (
            "Write a design spec for this task. Follow the repo spec "
            "format exactly (Problem Statement, Goal, Scope, "
            "Definition of Done, Scope & File Boundaries with Allowed "
            "Files, Implementation Plan, Acceptance Criteria checklist, "
            "Verification Commands fenced block).\n"
            "Ground the spec in existing repo-relative paths shown in the "
            "tracked paths or repository evidence below. Quote only code "
            "actually shown in the repository evidence; do not invent "
            "paths, symbols, or excerpts and present them as existing. "
            "In Allowed Files, use one Markdown bullet per implementation "
            "path, not a table, and do not list the generated spec itself. "
            "For a proposed new file, label it (CREATE) on the same bullet "
            "as its path and describe it as new, not as "
            "evidence of current behavior. If the evidence does not show "
            "the implementation, say so and ask any blocking questions "
            "in a trailing bulleted list.\n\n"
            f"{verification_guidance()}\n\n"
            f"Task: {brief}\n\n"
            "Relevant tracked paths in the active checkout:\n"
            f"{tracked_paths or '(no matching tracked files)'}\n\n"
            "Pre-seeded repository context (gathered deterministically):\n"
            f"{self.preseed_context_pack(brief)}\n\n"
            f"Context:\n{context}\n"
        )
        if errors:
            prompt += (
                "\nPrevious validation errors to fix:\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\n"
            )
        if answers:
            prompt += (
                "\nHuman answers:\n"
                + "\n".join(f"- {k}: {v}" for k, v in sorted(answers.items()))
                + "\n"
            )
        prompt = skills_registry.SkillsRegistry().inject_skills_for_node(
            "draft_spec_node", prompt
        )
        callback = self._usage_callback("draft")
        reply = (
            model.invoke(prompt, config={"callbacks": [callback]})
            if callback
            else model.invoke(prompt)
        )
        text = getattr(reply, "content", reply) if not isinstance(reply, str) else reply
        return clean_allowed_file_urls(text)

    def investigate_text(self, brief, draft):
        """One investigate pass (LLM + tools). Tests inject this."""
        self.investigation_truncated = False
        if self.investigate_fn is not None:
            return self.investigate_fn(brief, draft)
        import ask_investigator
        from langchain.agents import create_agent
        from langchain.agents.middleware import wrap_model_call
        from langgraph.errors import GraphRecursionError

        limit = self._spec_recursion_limit()
        observed = {"messages": []}
        results = []
        budget = {"exhausted": False}

        @wrap_model_call
        def capture_messages(request, handler):
            response = handler(request)
            observed["messages"].extend(response.result)
            return response

        agent = create_agent(
            self._model(),
            self._investigator_tools(),
            middleware=[
                investigation_tool_guard(
                    results, max_calls=self.max_spec_tool_calls, budget=budget
                ),
                force_final_turn(budget),
                capture_messages,
            ],
        )
        previous = ask_investigator._checkout
        ask_investigator._checkout = self.repo_root
        config = {"recursion_limit": limit}
        callback = self._usage_callback("investigation")
        if callback:
            config["callbacks"] = [callback]
        try:
            result = agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": f"Gather evidence for: {brief}\n\n"
                            "Pre-seeded repository context (already "
                            "gathered; cite it, do not re-read these "
                            f"files):\n{self.preseed_context_pack(brief)}\n\n"
                            f"Draft so far:\n{draft}\n\n"
                            "Cite every factual claim with a "
                            "repo file path (path:line). If you "
                            "cannot read any repo file, say so "
                            "explicitly instead of inventing "
                            "content.",
                        }
                    ]
                },
                config=config,
            )
        except (InvestigationTruncated, GraphRecursionError) as exc:
            self.investigation_truncated = True
            if self.event_fn is not None:
                self.event_fn(f"{type(exc).__name__}: {exc}")
            result = observed
        finally:
            ask_investigator._checkout = previous
        messages = result.get("messages", []) if isinstance(result, dict) else []
        for message in reversed(messages):
            if (
                getattr(message, "type", "") == "ai"
                and not getattr(message, "tool_calls", [])
                and getattr(message, "content", "")
            ):
                return message.content
        if self.investigation_truncated:
            return "\n".join(results)[:6000]
        return ""

    # -- graph ---------------------------------------------------------

    def build_graph(self):
        """Compile the closed-loop graph (needs langgraph)."""
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph

        # Dead Command import; south lane owns this file
        # (fix-issue-gen-backend): fold the removal in there.
        from langgraph.types import Command, interrupt  # noqa: F401
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
                return {
                    "errors": ["td init failed"],
                    "attempts": state.get("attempts", 0) + 1,
                }
            draft = outer.draft_text(
                state.get("brief", ""),
                state.get("context", ""),
                state.get("errors", []),
                state.get("answers", {}),
            )
            return {"draft": draft, "attempts": state.get("attempts", 0) + 1}

        def investigate_node(state):
            if state.get("evidence") and not state.get("errors"):
                return {}
            evidence = outer.investigate_text(
                state.get("brief", ""), state.get("draft", "")
            )
            if _TOOL_CALL_MARKUP_RE.search(evidence or "") and not (
                _TOOL_CALL_MARKUP_RE.sub("", evidence).strip()
            ):
                return {
                    "evidence": evidence,
                    "errors": [
                        "investigation returned only tool-call markup instead "
                        "of a cited evidence summary"
                    ],
                    "off_brief": True,
                }
            if not evidence_cites_repo_file(evidence, outer.repo_root):
                return {
                    "evidence": evidence,
                    "errors": [
                        "investigation cited no repo file "
                        "that exists on disk — agent "
                        "off-brief, failing loud instead of "
                        "filing an ungrounded spec"
                    ],
                    "off_brief": True,
                }
            update = {"evidence": evidence}
            if outer.investigation_truncated:
                update["draft"] = outer.draft_text(
                    state.get("brief", ""),
                    state.get("context", "")
                    + "\n\nInvestigation evidence:\n"
                    + evidence,
                    state.get("errors", []),
                    state.get("answers", {}),
                )
                update["attempts"] = state.get("attempts", 0) + 1
            return update

        def qa_loop_node(state):
            answers = dict(state.get("answers", {}))
            questions = list(
                state.get("questions", []) or extract_questions(state.get("draft", ""))
            )
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
                return {
                    "valid": False,
                    "validation": state.get("validation", {}),
                    "errors": state.get("errors", []),
                }
            spec_text = state.get("draft", "")
            if state.get("evidence"):
                spec_text += "\n\n## Investigation Evidence\n" + state["evidence"]
            if state.get("answers"):
                spec_text += "\n\n## Q&A Context\n" + "\n".join(
                    f"- **{k}**: {v}" for k, v in sorted(state["answers"].items())
                )
            valid, data, errors = outer.validate_spec(spec_text)
            update = {
                "valid": valid,
                "validation": data,
                "errors": errors if not valid else [],
            }
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
            from backend.shared import ledger

            calls = [
                {"name": "td init", "args": ""},
                {
                    "name": "td validate-issue",
                    "args": f"attempts={state.get('attempts', 0)}",
                },
            ]
            results = [f"valid={state.get('valid')}"]
            reply = state.get("draft", "")[:500] if state.get("valid") else ""
            lines = decisions.build_decisions(
                calls, results, reply, reason="issue spec closed loop"
            )
            spec = getattr(outer, "last_spec", None) or {}
            ledger.record(
                "issue_generator.py",
                spec.get("model") or os.environ.get("S5_ISSUE_MODEL", "qwen3:8b"),
                outer.thread_id,
                [],
                time.time(),
                extra={
                    "stage": "spec",
                    "phase": "summary",
                    "llm_call": False,
                    "valid": state.get("valid"),
                    "attempts": state.get("attempts", 0),
                    "decisions": lines,
                    "validation": state.get("validation", {}),
                },
                provider=spec.get("provider"),
            )
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
        builder.add_conditional_edges(
            "validate",
            should_redraft,
            {"draft": "draft", "persist": "persist", END: END},
        )
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
                {
                    "brief": brief,
                    "context": context,
                    "answers": dict(answers or {}),
                    "errors": [],
                    "attempts": 0,
                    "valid": False,
                },
                config,
            )
        return graph.invoke(Command(resume=resume), config)


def main(argv=None):
    """CLI: --spec-file <path|-> [--out PATH] [--qna-file PATH] [--max-loops N]."""
    from backend.shared import cli as cli_helpers

    args = list(argv if argv is not None else sys.argv[1:])
    spec_file = None
    qna_file = None
    max_loops = MAX_LOOPS_DEFAULT
    repo_root = None
    out = None
    i = 0
    while i < len(args):
        if args[i] == "--spec-file" and i + 1 < len(args):
            spec_file, i = args[i + 1], i + 2
        elif args[i] == "--out" and i + 1 < len(args):
            out, i = args[i + 1], i + 2
        elif args[i] == "--qna-file" and i + 1 < len(args):
            qna_file, i = args[i + 1], i + 2
        elif args[i] == "--max-loops" and i + 1 < len(args):
            try:
                max_loops = int(args[i + 1])
            except ValueError:
                return cli_helpers.usage_error(
                    "spec-file mode: --max-loops N:",
                    "usage: issue_generator.py --spec-file <path|->"
                    " [--qna-file <path>] [--max-loops N]",
                )
            i += 2
        elif args[i] == "--repo-root" and i + 1 < len(args):
            repo_root, i = args[i + 1], i + 2
        else:
            return cli_helpers.usage_error(
                f"spec-file mode: unknown arg: {args[i]}",
                "usage: issue_generator.py --spec-file <path|->"
                " [--qna-file <path>] [--max-loops N]",
            )
    if not spec_file:
        return cli_helpers.usage_error(
            "spec-file mode: --spec-file is required:",
            "usage: issue_generator.py --spec-file <path|->"
            " [--qna-file <path>] [--max-loops N]",
        )
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
        state = gen.run(brief)
        rounds = 0
        while "__interrupt__" in state and rounds < max_loops + 1:
            payload = state["__interrupt__"][0].value
            questions = payload.get("questions") if isinstance(payload, dict) else []
            if not qna_file:
                print(json.dumps({"questions": questions}))
                return 2
            try:
                with open(qna_file, encoding="utf-8") as handle:
                    resume = json.load(handle)
            except (OSError, ValueError) as exc:
                return cli_helpers.sorry(f"Sorry, cannot read Q&A file: {exc}")
            state = gen.run(brief, resume=resume)
            rounds += 1
    except ImportError:
        return cli_helpers.sorry(
            "Sorry, the agent runtime is unavailable here "
            "(deep-agent venv with langgraph is required)."
        )
    except RuntimeError as exc:
        return cli_helpers.sorry(f"Sorry, {exc}")
    if state.get("valid"):
        if out:
            try:
                write_spec(out, completed_spec(state))
            except OSError as exc:
                return cli_helpers.sorry(f"Sorry, cannot write spec: {exc}")
        print(
            json.dumps(
                {"status": "ready", "valid": True, "attempts": state.get("attempts", 0)}
            )
        )
        return 0
    for error in state.get("errors", []):
        print(f"Invalid spec: {error}")
    try:
        from backend.shared import ledger as _ledger

        _spec = getattr(gen, "last_spec", None) or {}
        _ledger.record(
            "issue_generator.py",
            _spec.get("model") or os.environ.get("S5_ISSUE_MODEL", "qwen3:8b"),
            gen.thread_id,
            [],
            time.time(),
            extra={
                "valid": False,
                "attempts": state.get("attempts", 0),
                "errors": state.get("errors", []),
            },
            provider=_spec.get("provider"),
        )
    except ImportError:
        pass
    if "__interrupt__" in state:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

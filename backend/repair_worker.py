#!/usr/bin/env python3
"""LangGraph factory line state machine for local autonomous code repair.

Coordinates deterministic code repair using local models (qwen2.5-coder:7b via Ollama).
Performs task specification parsing, isolated git worktree setup, full-file LLM rewrites,
AST validation, test verification, failure feedback loop with compiler/test stderr,
and automated PR creation.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, TypedDict

try:
    from langgraph.graph import END, StateGraph
except ImportError:
    # Allow running if langgraph is imported differently
    StateGraph = None  # type: ignore
    END = "__end__"  # type: ignore

try:
    from backend.ast_validator import validate_source_syntax
    from backend.core.repos import get_checkout_path
    from backend.parse_task_spec import (
        SCAFFOLD_HINT,
        TaskSpecError,
        parse_task_spec,
    )
    from backend.shared import ledger
    from backend.verify_allowlist import (
        VERIFY_TIMEOUT,
        VERIFY_VERB_HELP,
        validate_verify_command,
    )
except ImportError:
    try:
        from ast_validator import validate_source_syntax
        from core.repos import get_checkout_path
        from parse_task_spec import (
            SCAFFOLD_HINT,
            TaskSpecError,
            parse_task_spec,
        )
        from shared import ledger  # type: ignore
        from verify_allowlist import (
            VERIFY_TIMEOUT,
            VERIFY_VERB_HELP,
            validate_verify_command,
        )
    except ImportError:
        # Fallback when running from different relative paths
        from ast_validator import validate_source_syntax  # type: ignore
        from parse_task_spec import (  # type: ignore
            SCAFFOLD_HINT,
            TaskSpecError,
            parse_task_spec,
        )
        from verify_allowlist import (  # type: ignore
            VERIFY_TIMEOUT,
            VERIFY_VERB_HELP,
            validate_verify_command,
        )

        def get_checkout_path(repo: str):  # type: ignore
            return None

        ledger = None  # type: ignore


# Parity with backend/shared/model_router.py DEFAULT_OLLAMA_URL: the stack's
# Ollama host. Replicated instead of imported so repair_worker stays
# stdlib-only and self-contained (model_router pulls pricing, runs, ledger
# and the spend-cap machinery). Never bake the
# /api/generate path into the default; endpoints are built by the helpers below.
DEFAULT_OLLAMA_HOST = "http://ollama:11434"
OLLAMA_GENERATE_PATH = "/api/generate"
OLLAMA_TAGS_PATH = "/api/tags"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_OLLAMA_TIMEOUT = 1200
MAX_RETRIES = 3

DEFAULT_REPO = "remote-server"

REPO_BASENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_last_run_tokens = {"input_tokens": 0, "output_tokens": 0}


class UnscaffoldedIssueError(RuntimeError):
    """The issue body lacks the repair scaffold (Target Files /
    Verification Command sections). Raised by the parse node so
    run_repair can fail with an ACTIONABLE message instead of a
    bare parse error, and the backend can surface it titled."""


# Machine-greppable stderr line run_repair prints for an
# unscaffolded issue. The backend worker thread scans its merged
# log for this marker and records the titled failure_reason the
# dispatch queue row shows (issue #908).
UNSCAFFOLDED_MARKER = "repair-unscaffolded:"


def normalize_repo(repo: Optional[str]) -> str:
    """Full owner/repo gh path for a --repo value.

    Accepts a basename ("boomtick") or a full path
    ("arii/boomtick"); basenames resolve under arii/, the same
    convention as backend's repo_gh. Blank or None falls back to
    the default repo. Raises ValueError when invalid.
    """
    if repo is None:
        raw = ""
    elif isinstance(repo, str):
        raw = repo.strip()
    else:
        raise ValueError(f"invalid repo name: {repo!r}")
    if not raw:
        raw = DEFAULT_REPO
    name = raw.removeprefix("arii/").strip("/")
    if not name or "/" in name or not REPO_BASENAME_RE.match(name):
        raise ValueError(f"invalid repo name: {repo!r}")
    return f"arii/{name}"


class AgentState(TypedDict):
    issue_number: int
    issue_body: str
    repo: str
    target_files: List[str]
    verification_cmd: str
    file_contents: Dict[str, str]
    original_file_contents: Dict[str, str]
    current_errors: str
    retry_count: int
    ast_valid: bool
    tests_passed: bool
    worktree_dir: str
    branch_name: str
    mode: str
    input_tokens: int
    output_tokens: int
    pr_url: str


def _env(name: str, default: str = "") -> str:
    """Blank-tolerant env lookup (mirrors backend/shared/model_router._env)."""
    val = os.environ.get(name, default)
    if not isinstance(val, str) or not val.strip():
        return default
    return val


def ollama_base_url(raw: Optional[str] = None) -> str:
    """Stack Ollama host: OLLAMA_URL env (or raw override), path-tolerant.

    Accepts a bare host (with or without trailing slash) or a full
    /api/generate URL; always returns the bare host. Blank input falls
    back to the default.
    """
    base = raw if raw is not None else _env("OLLAMA_URL", DEFAULT_OLLAMA_HOST)
    if not isinstance(base, str) or not base.strip():
        base = DEFAULT_OLLAMA_HOST
    base = base.strip().rstrip("/")
    if base.endswith(OLLAMA_GENERATE_PATH):
        base = base[: -len(OLLAMA_GENERATE_PATH)]
    return base


def ollama_generate_url(raw: Optional[str] = None) -> str:
    """Full Ollama generate endpoint for the resolved host."""
    return ollama_base_url(raw) + OLLAMA_GENERATE_PATH


def ollama_tags_url(raw: Optional[str] = None) -> str:
    """Full Ollama tags endpoint for the resolved host."""
    return ollama_base_url(raw) + OLLAMA_TAGS_PATH


def ollama_model(raw: Optional[str] = None) -> str:
    """Coder model: OLLAMA_MODEL env (or raw override), blank-tolerant."""
    if isinstance(raw, str) and raw.strip():
        return raw
    return _env("OLLAMA_MODEL", DEFAULT_MODEL)


def ollama_timeout(raw: Optional[int] = None) -> int:
    """Timeout in seconds for Ollama generate requests (default 1200)."""
    if isinstance(raw, int) and raw > 0:
        return raw
    try:
        return int(_env("OLLAMA_TIMEOUT", str(DEFAULT_OLLAMA_TIMEOUT)))
    except ValueError:
        return DEFAULT_OLLAMA_TIMEOUT


def _query_ollama_raw(
    prompt: str, model: Optional[str] = None, url: Optional[str] = None
) -> Dict[str, Any]:
    """Send prompt to Ollama generate endpoint and return raw response dict.

    Model and endpoint resolve at call time from OLLAMA_MODEL / OLLAMA_URL
    (blank-tolerant), so container env always wins over import order.
    """
    payload = {
        "model": ollama_model(model),
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
        },
    }
    req = urllib.request.Request(
        ollama_generate_url(url),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=ollama_timeout()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _query_ollama(
    prompt: str, model: Optional[str] = None, url: Optional[str] = None
) -> str:
    """Send prompt to Ollama generate endpoint and return response text."""
    res_data = _query_ollama_raw(prompt, model=model, url=url)
    return res_data.get("response", "")


def _model_present(names: List[str], model: str) -> bool:
    """True when the /api/tags catalog serves the coder model.

    Exact match, or (for an untagged model) any tag of the same base —
    Ollama resolves untagged pulls to :latest.
    """
    for name in names:
        if name == model:
            return True
        if ":" not in model and isinstance(name, str) and name.split(":")[0] == model:
            return True
    return False


def preflight_ollama(timeout: int = 5) -> Optional[str]:
    """Check local Ollama is reachable and serves the coder model.

    One GET to /api/tags covers both checks. Returns None when healthy,
    else a one-line failure naming the endpoint (unreachable) or the
    model (missing from the catalog). Never raises, never touches the network
    beyond the single tags probe.
    """
    base = ollama_base_url()
    model = ollama_model()
    tags_url = ollama_tags_url(base)
    try:
        req = urllib.request.Request(tags_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        return f"repair: local Ollama unreachable at {tags_url} ({exc})"
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    models = data.get("models", []) if isinstance(data, dict) else []
    names = [m.get("name", "") for m in models if isinstance(m, dict)]
    if not _model_present(names, model):
        return (
            f"repair: local Ollama at {base} is missing model {model!r} "
            f"(hint: ollama pull {model})"
        )
    return None


def _extract_code_from_llm_response(response: str) -> str:
    """Extract code from LLM response, stripping markdown fences if present."""
    response = response.strip()
    # Match ```python ... ``` or ```bash ... ``` or just ``` ... ```
    fence_pattern = r"^```(?:[a-zA-Z0-9_\-\.\+]+)?\r?\n(.*?)```$"
    m = re.search(fence_pattern, response, re.DOTALL)
    if m:
        return m.group(1).strip()
    if response.startswith("```") and response.endswith("```"):
        lines = response.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return response


# Node implementations


def _worktree_path(worktree_dir, rel_path):
    """Joined target path, or None when it escapes the worktree.

    realpath both sides (s5tools pattern): catches dotdot, absolute
    entries, and symlink escapes. Falsy/non-str inputs reject.
    """
    if not isinstance(worktree_dir, str) or not worktree_dir:
        return None
    if not isinstance(rel_path, str) or not rel_path:
        return None
    root = os.path.realpath(worktree_dir)
    full = os.path.realpath(os.path.join(worktree_dir, rel_path))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def parse_issue_node(state: AgentState) -> Dict[str, Any]:
    """Fetch and parse task specification from GitHub issue."""
    issue_number = state.get("issue_number")
    issue_body = state.get("issue_body", "")
    repo = normalize_repo(state.get("repo"))

    if not issue_body and issue_number:
        cmd = [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "body",
            "--repo",
            repo,
        ]
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if proc.returncode == 0:
            try:
                parsed_json = json.loads(proc.stdout)
                issue_body = parsed_json.get("body", "")
            except Exception as exc:
                raise RuntimeError(
                    f"issue #{issue_number} body is not valid JSON "
                    f"(fetch corruption, not a spec error): {exc}"
                ) from exc
        else:
            raise RuntimeError(
                f"Failed to fetch issue #{issue_number}: {proc.stderr.strip()}"
            )

    try:
        spec = parse_task_spec(issue_body)
    except TaskSpecError as exc:
        # Unscaffolded issue (#908): a normal GitHub issue (audit,
        # feature, UX finding) carries no repair scaffold. Fail with
        # an actionable message naming the missing section and the
        # template — never a bare parse error.
        raise UnscaffoldedIssueError(
            f"issue #{issue_number} is not scaffolded for repair: "
            f"{exc}. To repair it, {SCAFFOLD_HINT}."
        ) from exc
    verification_cmd = spec.get("verification_command", "")
    # Security gate (issue #483): issue text is untrusted — reject
    # non-allowlisted commands here, before the worktree stage, so
    # they never reach an executor.
    if not validate_verify_command(verification_cmd):
        raise RuntimeError(
            "verification command not allowlisted: "
            f"{verification_cmd[:200]!r} "
            f"(allowed: {VERIFY_VERB_HELP})"
        )
    return {
        "issue_body": issue_body,
        "target_files": spec.get("target_files", []),
        "verification_cmd": verification_cmd,
    }


def setup_worktree_node(state: AgentState) -> Dict[str, Any]:
    """Set up an isolated git worktree and read target file contents."""
    issue_number = state.get("issue_number", 0)
    mode = state.get("mode") or "fresh"
    existing_branch = state.get("branch_name")
    worktree_dir = state.get("worktree_dir") or tempfile.mkdtemp(
        prefix=f"repair-issue-{issue_number}-"
    )

    repo_name = state.get("repo")
    checkout_dir = None
    if repo_name:
        try:
            resolved = get_checkout_path(repo_name)
            checkout_dir = str(resolved) if resolved is not None else None
        except Exception:
            checkout_dir = None

    branch_name = ""
    if mode == "resume" and existing_branch:
        check = subprocess.run(
            ["git", "rev-parse", "--verify", existing_branch],
            cwd=checkout_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check.returncode == 0:
            cmd = ["git", "worktree", "add", worktree_dir, existing_branch]
            proc = subprocess.run(
                cmd,
                cwd=checkout_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode == 0:
                branch_name = existing_branch
            else:
                logging.warning(
                    f"Failed to resume worktree for branch {existing_branch}: {proc.stderr.strip()}; falling back to fresh branch"
                )
        else:
            logging.warning(
                f"Resume branch {existing_branch} not found; falling back to fresh branch"
            )

    if not branch_name:
        branch_name = (
            existing_branch
            if mode != "resume" and existing_branch
            else f"repair/issue-{issue_number}-{int(time.time())}"
        )
        cmd = ["git", "worktree", "add", "-b", branch_name, worktree_dir, "HEAD"]
        proc = subprocess.run(
            cmd,
            cwd=checkout_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree: {proc.stderr.strip()}")

    # Read target files from worktree
    file_contents: Dict[str, str] = {}
    for rel_path in state.get("target_files", []):
        full_path = _worktree_path(worktree_dir, rel_path)
        if full_path is None:
            raise RuntimeError(f"target escapes the worktree: {rel_path[:200]!r}")
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                file_contents[rel_path] = f.read()
        else:
            file_contents[rel_path] = ""

    # Preserve dependency directories across git worktrees (e.g. node_modules, .venv)
    if checkout_dir:
        for dep_dir in ("node_modules", ".venv"):
            src_dep = os.path.join(checkout_dir, dep_dir)
            dst_dep = os.path.join(worktree_dir, dep_dir)
            if os.path.exists(src_dep) and not os.path.exists(dst_dep):
                try:
                    os.symlink(src_dep, dst_dep)
                except OSError:
                    pass

    print(
        f"[repair] Worktree ready at {worktree_dir} on branch {branch_name}",
        flush=True,
    )

    return {
        "worktree_dir": worktree_dir,
        "branch_name": branch_name,
        "file_contents": file_contents,
        "original_file_contents": dict(file_contents),
    }


def _should_edit_on_retry(file_path: str, current_errors: str) -> bool:
    """Return whether file_path is eligible for modification during error retries."""
    ext = os.path.splitext(file_path)[1].lower()
    base = os.path.basename(file_path).lower()
    if (
        ext in (".md", ".markdown", ".txt", ".rst")
        or base.endswith(".example")
        or base.endswith(".sample")
    ):
        return False
    return True


def edit_file_node(state: AgentState) -> Dict[str, Any]:
    """Query Ollama to generate full-file rewrites for target files."""
    file_contents = dict(state.get("file_contents", {}))
    original_file_contents = dict(state.get("original_file_contents", {}))
    issue_body = state.get("issue_body", "")
    current_errors = state.get("current_errors", "")
    retry_count = state.get("retry_count", 0)
    input_tokens = state.get("input_tokens", 0)
    output_tokens = state.get("output_tokens", 0)

    files_to_edit = list(file_contents.keys())
    if current_errors and retry_count > 0:
        candidates = [
            f for f in files_to_edit if _should_edit_on_retry(f, current_errors)
        ]
        mentioned = [
            f
            for f in candidates
            if f in current_errors or os.path.basename(f) in current_errors
        ]
        if mentioned:
            files_to_edit = mentioned
        elif candidates:
            files_to_edit = candidates

    for file_path in files_to_edit:
        current_code = file_contents[file_path]
        baseline_code = original_file_contents.get(file_path, current_code)
        prompt = (
            "You are an expert autonomous code repair engineer. Your task is to modify the following file "
            "to fulfill the specification and fix any compiler/test errors.\n\n"
            f"Specification:\n{issue_body}\n\n"
        )
        if current_errors and retry_count > 0:
            # Bound context to most recent diagnostic output (max 50 lines / 3000 chars) to prevent context bloat
            err_lines = current_errors.strip().splitlines()
            truncated_errors = (
                "\n".join(err_lines[-50:])
                if len(err_lines) > 50
                else current_errors.strip()
            )
            if len(truncated_errors) > 3000:
                truncated_errors = truncated_errors[-3000:]
            prompt += (
                "Previous attempt failed with the following error/compiler output:\n"
                f"{truncated_errors}\n\n"
                f"Target File: {file_path}\n"
                f"Original Baseline Content:\n{baseline_code}\n\n"
                f"Previous Attempt Content:\n{current_code}\n\n"
                "Please fix these errors in your revised implementation based on the original baseline.\n\n"
            )
        else:
            prompt += (
                f"Target File: {file_path}\nOriginal Content:\n{baseline_code}\n\n"
            )
        prompt += (
            "Instructions:\n"
            "1. Output the COMPLETE file rewrite. Do not output partial diffs, snippets, or placeholders.\n"
            "2. Output valid syntax only.\n"
            "3. Enclose your full-file code rewrite in markdown code fences (```).\n"
        )

        print(
            f"[repair] Querying Ollama ({ollama_model()}) for {file_path} (timeout {ollama_timeout()}s)...",
            flush=True,
        )
        try:
            res_data = _query_ollama_raw(prompt)
            raw_response = res_data.get("response", "")
            in_tok = int(res_data.get("prompt_eval_count") or 0)
            out_tok = int(res_data.get("eval_count") or 0)
            input_tokens += in_tok
            output_tokens += out_tok
            _last_run_tokens["input_tokens"] = input_tokens
            _last_run_tokens["output_tokens"] = output_tokens
            print(
                f"[repair] Ollama generated {out_tok} tokens ({in_tok} prompt tokens) for {file_path}",
                flush=True,
            )
        except (urllib.error.URLError, OSError) as exc:
            print(f"[repair] Ollama query failed for {file_path}: {exc}", flush=True)
            endpoint = ollama_generate_url()
            return {
                "file_contents": file_contents,
                "current_errors": (
                    f"Ollama query failed for {file_path} at {endpoint}: {exc} "
                    f"(hint: is Ollama up at {ollama_base_url()} with model "
                    f"{ollama_model()!r} pulled?)"
                ),
                "retry_count": state.get("retry_count", 0) + 1,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        rewritten_code = _extract_code_from_llm_response(raw_response)
        file_contents[file_path] = rewritten_code

    return {
        "file_contents": file_contents,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def ast_check_node(state: AgentState) -> Dict[str, Any]:
    """Run fast AST pre-validation on all target files."""
    file_contents = state.get("file_contents", {})
    worktree_dir = state.get("worktree_dir", "")
    retry_count = state.get("retry_count", 0)

    ast_errors: List[str] = []
    for file_path, code in file_contents.items():
        res = validate_source_syntax(file_path, code)
        if res.get("degraded"):
            logging.warning(
                "syntax gate degraded for %s: %s", file_path, res["degraded"]
            )
        if not res.get("valid", True):
            err_line = res.get("line")
            err_msg = res.get("error", "Syntax error")
            ast_errors.append(f"{file_path} (line {err_line}): {err_msg}")

    if ast_errors:
        print(
            f"[repair] AST pre-validation failed ({len(ast_errors)} error(s)):\n"
            + "\n".join(ast_errors),
            flush=True,
        )
        return {
            "ast_valid": False,
            "current_errors": "AST Pre-validation Failed:\n" + "\n".join(ast_errors),
            "retry_count": retry_count + 1,
        }

    print("[repair] AST pre-validation passed for all target files", flush=True)

    # AST is valid: write the validated code to the worktree files
    for file_path, code in file_contents.items():
        full_path = _worktree_path(worktree_dir, file_path)
        if full_path is None:
            raise RuntimeError(f"target escapes the worktree: {file_path[:200]!r}")
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(code)

    return {
        "ast_valid": True,
        "current_errors": "",
    }


def verify_code_node(state: AgentState) -> Dict[str, Any]:
    """Execute the target verification command inside the worktree directory."""
    verification_cmd = state.get("verification_cmd", "")
    worktree_dir = state.get("worktree_dir", "")
    retry_count = state.get("retry_count", 0)

    if not verification_cmd:
        return {"tests_passed": True, "current_errors": ""}

    # Defense in depth: the parse node already gated this, but state
    # reaching verify directly must never execute untrusted text.
    # Retry is exhausted — a rejected command never passes on retry.
    cmd_args = validate_verify_command(verification_cmd)
    if cmd_args is None:
        return {
            "tests_passed": False,
            "current_errors": (
                "verification command not allowlisted: "
                f"{verification_cmd[:200]!r} "
                f"(allowed: {VERIFY_VERB_HELP})"
            ),
            "retry_count": MAX_RETRIES,
        }
    print(
        f"[repair] Executing verification command: {verification_cmd}",
        flush=True,
    )
    run_env = os.environ.copy()
    venv_bin = os.path.join(worktree_dir, ".venv", "bin")
    node_bin = os.path.join(worktree_dir, "node_modules", ".bin")
    current_path = run_env.get("PATH", "")
    run_env["PATH"] = f"{venv_bin}:{node_bin}:{current_path}"

    python_paths = [worktree_dir]
    for sub in ("cli", "backend", "src", "deep-agent"):
        candidate = os.path.join(worktree_dir, sub)
        if os.path.isdir(candidate):
            python_paths.append(candidate)
    existing_ppath = run_env.get("PYTHONPATH", "")
    if existing_ppath:
        python_paths.append(existing_ppath)
    run_env["PYTHONPATH"] = ":".join(python_paths)

    try:
        proc = subprocess.run(
            cmd_args,
            cwd=worktree_dir,
            env=run_env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=VERIFY_TIMEOUT,
        )
    except FileNotFoundError as exc:
        print(
            f"[repair] Verification command executable not found: {exc}",
            flush=True,
        )
        return {
            "tests_passed": False,
            "current_errors": f"verification command executable not found: {exc}",
            "retry_count": retry_count + 1,
        }
    except subprocess.TimeoutExpired:
        print(
            f"[repair] Verification command timed out after {VERIFY_TIMEOUT}s",
            flush=True,
        )
        return {
            "tests_passed": False,
            "current_errors": (
                "verification command timed out after "
                f"{VERIFY_TIMEOUT}s: {verification_cmd[:200]}"
            ),
            "retry_count": retry_count + 1,
        }

    if proc.returncode == 0:
        print("[repair] Verification command passed", flush=True)
        return {
            "tests_passed": True,
            "current_errors": "",
        }
    else:
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        print(
            f"[repair] Verification command failed (exit code {proc.returncode}):\n{output.strip()}",
            flush=True,
        )
        return {
            "tests_passed": False,
            "current_errors": f"Verification command failed (exit code {proc.returncode}):\n{output.strip()}",
            "retry_count": retry_count + 1,
        }


def create_pr_node(state: AgentState) -> Dict[str, Any]:
    """Commit changes, push branch, and create a GitHub pull request."""
    worktree_dir = state.get("worktree_dir", "")
    branch_name = state.get("branch_name", "")
    issue_number = state.get("issue_number", 0)
    repo = normalize_repo(state.get("repo"))

    # Clean up dependency symlinks before staging so they are never committed
    for dep_dir in ("node_modules", ".venv"):
        dst_dep = os.path.join(worktree_dir, dep_dir)
        if os.path.islink(dst_dep):
            try:
                os.unlink(dst_dep)
            except OSError:
                pass

    commit_env = os.environ.copy()
    commit_env.setdefault("GIT_AUTHOR_NAME", "S5 Autonomous Repair")
    commit_env.setdefault("GIT_AUTHOR_EMAIL", "repair@s5.internal")
    commit_env.setdefault("GIT_COMMITTER_NAME", "S5 Autonomous Repair")
    commit_env.setdefault("GIT_COMMITTER_EMAIL", "repair@s5.internal")

    status_proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if status_proc.stdout.strip():
        # Git add target files specifically to avoid staging untracked files
        target_files = state.get("target_files", [])
        if target_files:
            subprocess.run(
                ["git", "add", "--"] + target_files, cwd=worktree_dir, check=True
            )
        else:
            subprocess.run(["git", "add", "-A"], cwd=worktree_dir, check=True)

        commit_msg = f"fix(repair): autonomous repair for issue #{issue_number}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=worktree_dir,
            env=commit_env,
            check=True,
        )

    # Git push
    subprocess.run(
        [
            "git",
            "-c",
            "credential.https://github.com.helper=!gh auth git-credential",
            "push",
            "origin",
            branch_name,
        ],
        cwd=worktree_dir,
        env=commit_env,
        check=True,
    )

    # Create PR via gh CLI
    pr_title = f"fix(repair): autonomous repair for issue #{issue_number}"
    pr_body = (
        f"Autonomous code repair resolved issue #{issue_number}.\n\n"
        f"Target Files: {', '.join(state.get('target_files', []))}\n"
        f"Verification Command: `{state.get('verification_cmd', '')}`\n"
    )
    print(
        f"[repair] Pushed branch {branch_name} and creating PR for issue #{issue_number}...",
        flush=True,
    )
    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        pr_title,
        "--body",
        pr_body,
        "--head",
        branch_name,
        "--repo",
        repo,
    ]
    proc = subprocess.run(
        cmd, cwd=worktree_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create PR: {proc.stderr.strip()}")

    pr_url = (proc.stdout or "").strip()
    print(f"[repair] Successfully created PR: {pr_url}", flush=True)
    return {"pr_url": pr_url}


def cleanup_node(state: AgentState) -> Dict[str, Any]:
    """Clean up worktree and temporary directories."""
    worktree_dir = state.get("worktree_dir")
    repo_name = state.get("repo")
    checkout_dir = None
    if repo_name:
        try:
            resolved = get_checkout_path(repo_name)
            checkout_dir = str(resolved) if resolved is not None else None
        except Exception:
            checkout_dir = None

    if worktree_dir and os.path.exists(worktree_dir):
        for dep_dir in ("node_modules", ".venv"):
            dst_dep = os.path.join(worktree_dir, dep_dir)
            if os.path.islink(dst_dep):
                try:
                    os.unlink(dst_dep)
                except OSError:
                    pass
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_dir],
            cwd=checkout_dir,
            check=False,
        )
        if os.path.exists(worktree_dir):
            shutil.rmtree(worktree_dir, ignore_errors=True)

    return {}


# Conditional routing functions


def route_after_setup(state: AgentState) -> str:
    """Route after setup: verify existing branch if resuming, else edit."""
    if state.get("mode") == "resume":
        return "ast_check_node"
    return "edit_file_node"


def route_after_ast_check(state: AgentState) -> str:
    """Route after AST check: retry edit_file if invalid, else verify_code."""
    if not state.get("ast_valid", False):
        if state.get("retry_count", 0) < MAX_RETRIES:
            return "edit_file_node"
        return "cleanup_node"
    return "verify_code_node"


def route_after_verify(state: AgentState) -> str:
    """Route after verification command: create PR if passed, else retry or cleanup."""
    if state.get("tests_passed", False):
        return "create_pr_node"
    if state.get("retry_count", 0) < MAX_RETRIES:
        return "edit_file_node"
    return "cleanup_node"


def build_repair_graph():
    """Construct and compile the LangGraph factory line state machine."""
    if StateGraph is None:
        logging.warning(
            "langgraph is not installed. Full LangGraph factory line repair worker requires langgraph."
        )
        return None

    workflow = StateGraph(AgentState)

    workflow.add_node("parse_issue_node", parse_issue_node)
    workflow.add_node("setup_worktree_node", setup_worktree_node)
    workflow.add_node("edit_file_node", edit_file_node)
    workflow.add_node("ast_check_node", ast_check_node)
    workflow.add_node("verify_code_node", verify_code_node)
    workflow.add_node("create_pr_node", create_pr_node)
    workflow.add_node("cleanup_node", cleanup_node)

    workflow.set_entry_point("parse_issue_node")

    workflow.add_edge("parse_issue_node", "setup_worktree_node")
    workflow.add_conditional_edges(
        "setup_worktree_node",
        route_after_setup,
        {
            "edit_file_node": "edit_file_node",
            "ast_check_node": "ast_check_node",
        },
    )
    workflow.add_edge("edit_file_node", "ast_check_node")

    workflow.add_conditional_edges(
        "ast_check_node",
        route_after_ast_check,
        {
            "edit_file_node": "edit_file_node",
            "verify_code_node": "verify_code_node",
            "cleanup_node": "cleanup_node",
        },
    )

    workflow.add_conditional_edges(
        "verify_code_node",
        route_after_verify,
        {
            "create_pr_node": "create_pr_node",
            "edit_file_node": "edit_file_node",
            "cleanup_node": "cleanup_node",
        },
    )

    workflow.add_edge("create_pr_node", "cleanup_node")
    workflow.add_edge("cleanup_node", END)

    return workflow.compile()


def run_repair(
    issue_number: int,
    issue_body: Optional[str] = None,
    repo: Optional[str] = None,
    branch: str = "",
    mode: str = "fresh",
) -> bool:
    """Run the autonomous repair pipeline for a given issue.

    ``repo`` is a basename ("boomtick") or full path
    ("arii/boomtick"); blank/None repairs the default repo.
    Raises ValueError on an invalid repo name.
    """
    full_repo = normalize_repo(repo)
    graph = build_repair_graph()
    if graph is None:
        print(
            "Repair worker stub: langgraph is not installed on this system.\n"
            "Full LangGraph factory line repair worker requires langgraph and ollama.",
            file=sys.stderr,
        )
        return False
    preflight_error = preflight_ollama()
    if preflight_error is not None:
        print(preflight_error, file=sys.stderr)
        return False
    initial_state: AgentState = {
        "issue_number": issue_number,
        "issue_body": issue_body or "",
        "repo": full_repo,
        "target_files": [],
        "verification_cmd": "",
        "file_contents": {},
        "original_file_contents": {},
        "current_errors": "",
        "retry_count": 0,
        "ast_valid": False,
        "tests_passed": False,
        "worktree_dir": "",
        "branch_name": branch,
        "mode": mode,
        "input_tokens": 0,
        "output_tokens": 0,
        "pr_url": "",
    }

    global _last_run_tokens
    _last_run_tokens = {"input_tokens": 0, "output_tokens": 0}

    started = time.time()
    final_state: Dict[str, Any] = {}
    pr_url = ""
    success = False
    try:
        final_state = graph.invoke(initial_state)
        success = bool(final_state.get("tests_passed", False))
        pr_url = str(final_state.get("pr_url", ""))
        return success
    except UnscaffoldedIssueError as exc:
        # Actionable + greppable: the log line keeps the human
        # message, the marker line lets the backend lift the
        # titled reason into the dispatch queue row (#908).
        logging.error(f"Repair pipeline encountered error: {exc}")
        print(f"{UNSCAFFOLDED_MARKER} {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        logging.error(f"Repair pipeline encountered error: {exc}")
        return False
    finally:
        if ledger is not None:
            try:
                in_tok = (
                    int(final_state.get("input_tokens", 0))
                    if isinstance(final_state, dict) and final_state.get("input_tokens")
                    else _last_run_tokens.get("input_tokens", 0)
                )
                out_tok = (
                    int(final_state.get("output_tokens", 0))
                    if isinstance(final_state, dict)
                    and final_state.get("output_tokens")
                    else _last_run_tokens.get("output_tokens", 0)
                )
                tot_tok = in_tok + out_tok
                pr_val = (
                    str(final_state.get("pr_url", ""))
                    if isinstance(final_state, dict)
                    else pr_url
                )
                clean_repo_slug = full_repo.replace("/", "-")
                thread_id = f"repair-{clean_repo_slug}-{issue_number}-{int(started)}"
                status_label = "APPROVED" if success else "FAILED"
                ledger.record(
                    script="repair_worker.py",
                    model=f"ollama:{ollama_model()}",
                    thread_id=thread_id,
                    messages=[
                        {
                            "role": "assistant",
                            "content": pr_val or status_label,
                        }
                    ],
                    started=started,
                    usage={
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "total_tokens": tot_tok,
                    },
                    extra={
                        "repo": full_repo,
                        "issue": issue_number,
                        "status": status_label,
                        "pr_url": pr_val,
                    },
                    provider="ollama",
                )
            except Exception as exc:
                logging.error(f"Failed to record repair run in ledger: {exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deterministic Autonomous Repair Worker"
    )
    parser.add_argument(
        "--issue", type=int, required=True, help="GitHub issue number to repair"
    )
    parser.add_argument(
        "--body", type=str, default="", help="Optional task specification markdown body"
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=DEFAULT_REPO,
        help="Repo basename (boomtick) or full path (arii/boomtick); "
        "scopes the gh issue/PR calls",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default="",
        help="Optional existing branch name to resume",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="fresh",
        choices=["fresh", "resume"],
        help="Retry mode: fresh or resume",
    )
    args = parser.parse_args(argv)

    try:
        repo = normalize_repo(args.repo)
    except ValueError as exc:
        parser.error(str(exc))
    success = run_repair(
        issue_number=args.issue,
        issue_body=args.body or None,
        repo=repo,
        branch=args.branch,
        mode=args.mode,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

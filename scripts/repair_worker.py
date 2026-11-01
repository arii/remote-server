#!/usr/bin/env python3
"""LangGraph factory line state machine for local autonomous code repair.

Deterministic repair workflow:
1. Parse task specifications (scripts/parse_task_spec.py)
2. Setup isolated git worktree and read target files
3. Prompt Ollama (qwen2.5-coder:7b) for full-file rewrite
4. Run fast AST pre-validation (scripts/ast_validator.py)
5. Run target verification command
6. Loop on failure (up to 3 retries) with stderr feedback
7. Commit verified changes, push, and open PR via gh pr create
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph

# Ensure scripts directory is on path whether run directly or as a module
try:
    from .ast_validator import validate_source_syntax
    from .parse_task_spec import parse_task_spec
except ImportError:
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from ast_validator import validate_source_syntax  # type: ignore
    from parse_task_spec import parse_task_spec  # type: ignore

logger = logging.getLogger("repair_worker")

DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("REPAIR_MODEL", "qwen2.5-coder:7b")
MAX_RETRIES = 3


class AgentState(TypedDict):
    issue_number: int
    issue_body: str
    target_files: list[str]
    verification_cmd: str
    file_contents: dict[str, str]
    current_errors: str
    retry_count: int
    ast_valid: bool
    tests_passed: bool
    worktree_dir: str
    branch_name: str


def sanitize_relative_path(path_str: str) -> str:
    """Sanitize path to ensure it cannot escape working tree directory."""
    clean = os.path.normpath(path_str.strip()).lstrip("/\\")
    if clean.startswith("..") or ".." in clean.split(os.sep):
        raise ValueError(f"Unsafe path traversal detected: {path_str}")
    return clean


def run_cmd(
    cmd: list[str] | str,
    cwd: str | None = None,
    check: bool = True,
    shell: bool = False,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Execute command and capture output."""
    logger.debug("Executing: %s (cwd=%s, shell=%s)", cmd, cwd, shell)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        shell=shell,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


def query_ollama(
    prompt: str,
    system_prompt: str = "",
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
) -> str:
    """Query local Ollama endpoint."""
    endpoint = f"{base_url.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("response", "")
    except Exception as e:
        logger.error("Failed to query Ollama at %s: %s", endpoint, e)
        raise


def extract_code_fence(text: str, file_path: str = "") -> str:
    """Extract code from markdown code fences if present; otherwise return raw text.

    If multiple code blocks exist (e.g. text preamble code block + rewritten code),
    select the largest block by content length.
    """
    pattern = r"```(?:[a-zA-Z0-9_\-+]*\n)?(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        # Choose the largest code block to avoid preamble or short note blocks
        largest = max(matches, key=len)
        return largest.strip()
    return text.strip()


def parse_issue_node(state: AgentState) -> dict[str, Any]:
    """Parse task specification from issue body."""
    logger.info("Node [parse_issue_node]: Parsing task spec for issue #%s", state.get("issue_number"))
    parsed = parse_task_spec(state.get("issue_body", ""))

    raw_targets = state.get("target_files") or parsed.get("target_files", [])
    sanitized_targets = [sanitize_relative_path(t) for t in raw_targets]
    verification_cmd = state.get("verification_cmd") or parsed.get("verification_cmd", "")

    return {
        "target_files": sanitized_targets,
        "verification_cmd": verification_cmd,
    }


def setup_worktree_node(state: AgentState) -> dict[str, Any]:
    """Setup isolated git worktree and read target file contents."""
    issue_number = state.get("issue_number", 0)
    branch_name = f"repair/issue-{issue_number}"
    worktree_dir = tempfile.mkdtemp(prefix=f"repair_worktree_{issue_number}_")

    logger.info("Node [setup_worktree_node]: Creating worktree at %s on branch %s", worktree_dir, branch_name)

    try:
        run_cmd(["git", "worktree", "add", "-b", branch_name, worktree_dir, "HEAD"])
    except subprocess.CalledProcessError:
        try:
            run_cmd(["git", "worktree", "add", worktree_dir, branch_name])
        except subprocess.CalledProcessError:
            run_cmd(["git", "branch", "-D", branch_name], check=False)
            run_cmd(["git", "worktree", "add", "-b", branch_name, worktree_dir, "HEAD"])

    file_contents: dict[str, str] = {}
    for rel_path in state.get("target_files", []):
        clean_rel = sanitize_relative_path(rel_path)
        full_path = os.path.join(worktree_dir, clean_rel)
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                file_contents[clean_rel] = f.read()
        else:
            file_contents[clean_rel] = ""

    return {
        "worktree_dir": worktree_dir,
        "branch_name": branch_name,
        "file_contents": file_contents,
        "retry_count": 0,
        "current_errors": "",
    }


def edit_file_node(state: AgentState) -> dict[str, Any]:
    """Prompt Ollama for a full-file rewrite and update files in worktree."""
    current_retry = state.get("retry_count", 0)
    current_errors = state.get("current_errors", "")
    target_files = state.get("target_files", [])
    worktree_dir = state.get("worktree_dir", "")
    file_contents = dict(state.get("file_contents", {}))

    logger.info(
        "Node [edit_file_node]: Editing target files (retry_count=%d, errors_present=%s)",
        current_retry,
        bool(current_errors),
    )

    system_prompt = (
        "You are an expert autonomous software repair engineer. "
        "You rewrite the target file completely with the fix applied. "
        "Return ONLY the complete rewritten source code for the requested file inside a single code block. "
        "Do NOT return git diffs, unified diffs, partial snippets, or explanations."
    )

    updated_contents: dict[str, str] = {}

    for file_path in target_files:
        clean_rel = sanitize_relative_path(file_path)
        original_content = file_contents.get(clean_rel, "")
        prompt = (
            f"Task Objective and Issue Description:\n"
            f"{state.get('issue_body', '')}\n\n"
            f"File to modify: {clean_rel}\n"
        )
        if original_content:
            prompt += f"Existing Content of {clean_rel}:\n```\n{original_content}\n```\n\n"
        else:
            prompt += f"The file {clean_rel} is currently empty or does not exist yet. Create its full content.\n\n"

        if current_errors:
            prompt += (
                f"PREVIOUS ATTEMPT FAILED WITH ERRORS:\n"
                f"{current_errors}\n\n"
                f"Please fix the code so it resolves these errors.\n\n"
            )

        prompt += (
            f"Rewrite the ENTIRE file `{clean_rel}` cleanly. "
            f"Output only the full replacement code enclosed in a ``` code block."
        )

        response = query_ollama(prompt, system_prompt=system_prompt)
        rewritten = extract_code_fence(response, clean_rel)
        updated_contents[clean_rel] = rewritten

        if worktree_dir:
            full_path = os.path.join(worktree_dir, clean_rel)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(rewritten)

    return {
        "file_contents": updated_contents,
        "retry_count": current_retry + 1,
    }


def ast_check_node(state: AgentState) -> dict[str, Any]:
    """Run fast AST pre-validation on all modified files."""
    logger.info("Node [ast_check_node]: Checking AST syntax for modified files")
    file_contents = state.get("file_contents", {})

    all_valid = True
    error_messages: list[str] = []

    for file_path, content in file_contents.items():
        is_valid, err = validate_source_syntax(file_path, content)
        if not is_valid:
            all_valid = False
            error_messages.append(f"AST Error in {file_path}:\n{err}")

    if all_valid:
        logger.info("Node [ast_check_node]: All files passed AST syntax validation")
        return {
            "ast_valid": True,
            "current_errors": "",
        }
    else:
        err_str = "\n".join(error_messages)
        logger.warning("Node [ast_check_node]: AST syntax failed:\n%s", err_str)
        return {
            "ast_valid": False,
            "current_errors": err_str,
        }


def verify_code_node(state: AgentState) -> dict[str, Any]:
    """Execute verification command safely without shell=True in worktree directory."""
    verification_cmd = state.get("verification_cmd", "").strip()
    worktree_dir = state.get("worktree_dir", "")

    logger.info("Node [verify_code_node]: Running verification '%s' in %s", verification_cmd, worktree_dir)

    if not verification_cmd:
        logger.info("Node [verify_code_node]: No verification command provided, passing by default")
        return {
            "tests_passed": True,
            "current_errors": "",
        }

    try:
        cmd_args = shlex.split(verification_cmd)
        proc = run_cmd(
            cmd_args,
            cwd=worktree_dir,
            shell=False,
            check=False,
            timeout=300,
        )
        if proc.returncode == 0:
            logger.info("Node [verify_code_node]: Verification command passed successfully")
            return {
                "tests_passed": True,
                "current_errors": "",
            }
        else:
            err_output = (proc.stderr or "") + "\n" + (proc.stdout or "")
            logger.warning("Node [verify_code_node]: Verification failed (exit code %d)", proc.returncode)
            return {
                "tests_passed": False,
                "current_errors": f"Verification command failed with code {proc.returncode}:\n{err_output.strip()}",
            }
    except Exception as e:
        logger.error("Node [verify_code_node]: Execution error: %s", e)
        return {
            "tests_passed": False,
            "current_errors": f"Verification execution exception: {e}",
        }


def create_pr_node(state: AgentState) -> dict[str, Any]:
    """Commit changes, push branch, and create pull request via gh pr create."""
    worktree_dir = state.get("worktree_dir", "")
    branch_name = state.get("branch_name", "")
    issue_number = state.get("issue_number", 0)

    logger.info("Node [create_pr_node]: Committing and creating PR for issue #%s", issue_number)

    run_cmd(["git", "add", "-A"], cwd=worktree_dir)
    commit_msg = f"fix(repair): autonomous repair for issue #{issue_number}"
    run_cmd(["git", "commit", "-m", commit_msg], cwd=worktree_dir)

    run_cmd(["git", "push", "-u", "origin", branch_name], cwd=worktree_dir)

    pr_title = f"fix(repair): autonomous repair for issue #{issue_number}"
    pr_body = (
        f"Autonomous repair generated for issue #{issue_number}.\n\n"
        f"Verification Command:\n`{state.get('verification_cmd', '')}`\n\n"
        f"All AST checks and verification tests passed."
    )
    run_cmd(
        ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--head", branch_name],
        cwd=worktree_dir,
    )

    return {}


def cleanup_node(state: AgentState) -> dict[str, Any]:
    """Clean up worktree directory and git worktree tracking."""
    worktree_dir = state.get("worktree_dir", "")
    logger.info("Node [cleanup_node]: Cleaning up worktree %s", worktree_dir)

    if worktree_dir and os.path.exists(worktree_dir):
        try:
            run_cmd(["git", "worktree", "remove", "--force", worktree_dir], check=False)
        except Exception:
            pass
        if os.path.exists(worktree_dir):
            shutil.rmtree(worktree_dir, ignore_errors=True)

    return {}


def route_ast_check(state: AgentState) -> Literal["verify_code_node", "edit_file_node", "cleanup_node"]:
    """Route after AST check: if valid -> verify_code_node; if invalid -> edit_file_node or cleanup."""
    if state.get("ast_valid", False):
        return "verify_code_node"
    if state.get("retry_count", 0) >= MAX_RETRIES:
        logger.warning("Max retries (%d) reached after AST failure, routing to cleanup", MAX_RETRIES)
        return "cleanup_node"
    logger.info("AST invalid, routing back to edit_file_node (attempt %d/%d)", state.get("retry_count", 0), MAX_RETRIES)
    return "edit_file_node"


def route_verification(state: AgentState) -> Literal["create_pr_node", "edit_file_node", "cleanup_node"]:
    """Route after code verification: if tests pass -> create_pr_node; if fail -> edit_file_node or cleanup."""
    if state.get("tests_passed", False):
        return "create_pr_node"
    if state.get("retry_count", 0) >= MAX_RETRIES:
        logger.warning("Max retries (%d) reached after test failure, routing to cleanup", MAX_RETRIES)
        return "cleanup_node"
    logger.info("Tests failed, routing back to edit_file_node (attempt %d/%d)", state.get("retry_count", 0), MAX_RETRIES)
    return "edit_file_node"


def build_repair_graph() -> Any:
    """Build and compile the LangGraph repair state machine."""
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
    workflow.add_edge("setup_worktree_node", "edit_file_node")
    workflow.add_edge("edit_file_node", "ast_check_node")

    workflow.add_conditional_edges(
        "ast_check_node",
        route_ast_check,
        {
            "verify_code_node": "verify_code_node",
            "edit_file_node": "edit_file_node",
            "cleanup_node": "cleanup_node",
        },
    )

    workflow.add_conditional_edges(
        "verify_code_node",
        route_verification,
        {
            "create_pr_node": "create_pr_node",
            "edit_file_node": "edit_file_node",
            "cleanup_node": "cleanup_node",
        },
    )

    workflow.add_edge("create_pr_node", "cleanup_node")
    workflow.add_edge("cleanup_node", END)

    return workflow.compile()


def run_repair_worker(
    issue_number: int,
    issue_body: str,
    target_files: list[str] | None = None,
    verification_cmd: str | None = None,
) -> dict[str, Any]:
    """Execute the repair state machine workflow."""
    initial_state: AgentState = {
        "issue_number": issue_number,
        "issue_body": issue_body,
        "target_files": target_files or [],
        "verification_cmd": verification_cmd or "",
        "file_contents": {},
        "current_errors": "",
        "retry_count": 0,
        "ast_valid": False,
        "tests_passed": False,
        "worktree_dir": "",
        "branch_name": "",
    }

    graph = build_repair_graph()
    final_state = graph.invoke(initial_state)
    return final_state


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Autonomous code repair state machine worker")
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub Issue Number")
    parser.add_argument("--issue-body", type=str, help="GitHub Issue Body content")
    parser.add_argument("--issue-file", type=str, help="Path to file containing GitHub Issue Body")
    parser.add_argument("--target-files", nargs="*", help="Optional override for target files")
    parser.add_argument("--verification-cmd", type=str, help="Optional override for verification command")
    args = parser.parse_args()

    body = args.issue_body or ""
    if args.issue_file and os.path.exists(args.issue_file):
        with open(args.issue_file, "r", encoding="utf-8") as f:
            body = f.read()

    result = run_repair_worker(
        issue_number=args.issue_number,
        issue_body=body,
        target_files=args.target_files,
        verification_cmd=args.verification_cmd,
    )
    print(json.dumps({k: v for k, v in result.items() if isinstance(v, (str, int, bool, list, dict))}, indent=2))


if __name__ == "__main__":
    main()

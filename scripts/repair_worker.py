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
import shlex
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
    from scripts.ast_validator import validate_source_syntax
    from scripts.parse_task_spec import parse_task_spec
except ImportError:
    try:
        from ast_validator import validate_source_syntax
        from parse_task_spec import parse_task_spec
    except ImportError:
        # Fallback when running from different relative paths
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from ast_validator import validate_source_syntax  # type: ignore
        from parse_task_spec import parse_task_spec  # type: ignore

DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
MAX_RETRIES = 3


class AgentState(TypedDict):
    issue_number: int
    issue_body: str
    target_files: List[str]
    verification_cmd: str
    file_contents: Dict[str, str]
    current_errors: str
    retry_count: int
    ast_valid: bool
    tests_passed: bool
    worktree_dir: str
    branch_name: str


def _query_ollama(prompt: str, model: str = DEFAULT_MODEL, url: str = DEFAULT_OLLAMA_URL) -> str:
    """Send prompt to Ollama generate endpoint and return response text."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        return res_data.get("response", "")


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


def parse_issue_node(state: AgentState) -> Dict[str, Any]:
    """Fetch and parse task specification from GitHub issue."""
    issue_number = state.get("issue_number")
    issue_body = state.get("issue_body", "")

    if not issue_body and issue_number:
        cmd = ["gh", "issue", "view", str(issue_number), "--json", "body"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0:
            try:
                parsed_json = json.loads(proc.stdout)
                issue_body = parsed_json.get("body", "")
            except Exception:
                issue_body = proc.stdout
        else:
            raise RuntimeError(f"Failed to fetch issue #{issue_number}: {proc.stderr.strip()}")

    spec = parse_task_spec(issue_body)
    return {
        "issue_body": issue_body,
        "target_files": spec.get("target_files", []),
        "verification_cmd": spec.get("verification_command", ""),
    }


def setup_worktree_node(state: AgentState) -> Dict[str, Any]:
    """Set up an isolated git worktree and read target file contents."""
    issue_number = state.get("issue_number", 0)
    branch_name = state.get("branch_name") or f"repair/issue-{issue_number}-{int(time.time())}"
    worktree_dir = state.get("worktree_dir") or tempfile.mkdtemp(prefix=f"repair-issue-{issue_number}-")

    # Create worktree
    cmd = ["git", "worktree", "add", "-b", branch_name, worktree_dir, "HEAD"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create git worktree: {proc.stderr.strip()}")

    # Read target files from worktree
    file_contents: Dict[str, str] = {}
    for rel_path in state.get("target_files", []):
        full_path = os.path.join(worktree_dir, rel_path)
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                file_contents[rel_path] = f.read()
        else:
            file_contents[rel_path] = ""

    return {
        "worktree_dir": worktree_dir,
        "branch_name": branch_name,
        "file_contents": file_contents,
    }


def edit_file_node(state: AgentState) -> Dict[str, Any]:
    """Query Ollama to generate full-file rewrites for target files."""
    file_contents = dict(state.get("file_contents", {}))
    issue_body = state.get("issue_body", "")
    current_errors = state.get("current_errors", "")

    for file_path, original_code in list(file_contents.items()):
        prompt = (
            "You are an expert autonomous code repair engineer. Your task is to modify the following file "
            "to fulfill the specification and fix any compiler/test errors.\n\n"
            f"Specification:\n{issue_body}\n\n"
        )
        if current_errors:
            # Bound context to most recent diagnostic output (max 50 lines / 3000 chars) to prevent context bloat
            err_lines = current_errors.strip().splitlines()
            truncated_errors = "\n".join(err_lines[-50:]) if len(err_lines) > 50 else current_errors.strip()
            if len(truncated_errors) > 3000:
                truncated_errors = truncated_errors[-3000:]
            prompt += (
                "Previous attempt failed with the following error/compiler output:\n"
                f"{truncated_errors}\n\n"
                "Please fix these errors in your revised implementation.\n\n"
            )
        prompt += (
            f"Target File: {file_path}\n"
            f"Original Content:\n{original_code}\n\n"
            "Instructions:\n"
            "1. Output the COMPLETE file rewrite. Do not output partial diffs, snippets, or placeholders.\n"
            "2. Output valid syntax only.\n"
            "3. Enclose your full-file code rewrite in markdown code fences (```).\n"
        )

        raw_response = _query_ollama(prompt)
        rewritten_code = _extract_code_from_llm_response(raw_response)
        file_contents[file_path] = rewritten_code

    return {
        "file_contents": file_contents,
    }


def ast_check_node(state: AgentState) -> Dict[str, Any]:
    """Run fast AST pre-validation on all target files."""
    file_contents = state.get("file_contents", {})
    worktree_dir = state.get("worktree_dir", "")
    retry_count = state.get("retry_count", 0)

    ast_errors: List[str] = []
    for file_path, code in file_contents.items():
        res = validate_source_syntax(file_path, code)
        if not res.get("valid", True):
            err_line = res.get("line")
            err_msg = res.get("error", "Syntax error")
            ast_errors.append(f"{file_path} (line {err_line}): {err_msg}")

    if ast_errors:
        return {
            "ast_valid": False,
            "current_errors": "AST Pre-validation Failed:\n" + "\n".join(ast_errors),
            "retry_count": retry_count + 1,
        }

    # AST is valid: write the validated code to the worktree files
    for file_path, code in file_contents.items():
        full_path = os.path.join(worktree_dir, file_path)
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

    cmd_args = shlex.split(verification_cmd)
    proc = subprocess.run(
        cmd_args,
        cwd=worktree_dir,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if proc.returncode == 0:
        return {
            "tests_passed": True,
            "current_errors": "",
        }
    else:
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
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

    # Git add and commit
    subprocess.run(["git", "add", "-A"], cwd=worktree_dir, check=True)
    commit_msg = f"fix(repair): autonomous repair for issue #{issue_number}"
    subprocess.run(["git", "commit", "-m", commit_msg], cwd=worktree_dir, check=True)

    # Git push
    subprocess.run(["git", "push", "origin", branch_name], cwd=worktree_dir, check=True)

    # Create PR via gh CLI
    pr_title = f"fix(repair): autonomous repair for issue #{issue_number}"
    pr_body = (
        f"Autonomous code repair resolved issue #{issue_number}.\n\n"
        f"Target Files: {', '.join(state.get('target_files', []))}\n"
        f"Verification Command: `{state.get('verification_cmd', '')}`\n"
    )
    cmd = ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--head", branch_name]
    proc = subprocess.run(cmd, cwd=worktree_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create PR: {proc.stderr.strip()}")

    return {}


def cleanup_node(state: AgentState) -> Dict[str, Any]:
    """Clean up worktree and temporary directories."""
    worktree_dir = state.get("worktree_dir")
    branch_name = state.get("branch_name")

    if worktree_dir and os.path.exists(worktree_dir):
        subprocess.run(["git", "worktree", "remove", "--force", worktree_dir], check=False)
        if os.path.exists(worktree_dir):
            shutil.rmtree(worktree_dir, ignore_errors=True)

    return {}


# Conditional routing functions


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


def run_repair(issue_number: int, issue_body: Optional[str] = None) -> bool:
    """Run the autonomous repair pipeline for a given issue."""
    initial_state: AgentState = {
        "issue_number": issue_number,
        "issue_body": issue_body or "",
        "target_files": [],
        "verification_cmd": "",
        "file_contents": {},
        "current_errors": "",
        "retry_count": 0,
        "ast_valid": False,
        "tests_passed": False,
        "worktree_dir": "",
        "branch_name": "",
    }

    graph = build_repair_graph()
    try:
        final_state = graph.invoke(initial_state)
        return final_state.get("tests_passed", False)
    except Exception as exc:
        logging.error(f"Repair pipeline encountered error: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Deterministic Autonomous Repair Worker")
    parser.add_argument("--issue", type=int, required=True, help="GitHub issue number to repair")
    parser.add_argument("--body", type=str, default="", help="Optional task specification markdown body")
    args = parser.parse_args()

    success = run_repair(issue_number=args.issue, issue_body=args.body or None)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Local Autonomous Repair Worker ("Local Jules").

Autonomously reproduces, triages, edits, and validates code fixes for
deterministic, highly specified code issues using local models (e.g.
qwen2.5-coder:7b via Ollama) and isolated git worktrees.
Zero external cloud dependencies.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    from scripts.ast_validator import validate_source_syntax
    from scripts.parse_task_spec import parse_task_spec
except ImportError:
    try:
        from ast_validator import validate_source_syntax
        from parse_task_spec import parse_task_spec
    except ImportError:
        # Fallback if invoked from another working directory
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ast_validator import validate_source_syntax
        from parse_task_spec import parse_task_spec


DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("REPAIR_MODEL", "qwen2.5-coder:7b")
DEFAULT_MAX_ITERATIONS = int(os.environ.get("REPAIR_MAX_ITERATIONS", "5"))


def query_ollama(
    prompt: str,
    model: str = DEFAULT_MODEL,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
    system_prompt: Optional[str] = None,
    timeout: int = 180,
) -> str:
    """Send generation query to local Ollama API endpoint."""
    url = f"{ollama_host.rstrip('/')}/api/generate"
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 4096,
        },
    }
    if system_prompt:
        payload["system"] = system_prompt

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return res_json.get("response", "")
    except urllib.error.URLError as err:
        raise RuntimeError(f"Failed to query Ollama at {url}: {err}") from err


def run_cmd_in_dir(
    cmd: str,
    cwd: str,
    timeout: int = 120,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Execute a shell command in a specific directory with combined/captured output."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    proc = subprocess.run(
        cmd,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=merged_env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def fetch_issue_body(repo_cwd: str, issue_number: int, repo_gh: Optional[str] = None) -> str:
    """Fetch issue description from GitHub CLI."""
    cmd = ["gh", "issue", "view", str(issue_number), "--json", "body,title"]
    if repo_gh:
        cmd.extend(["--repo", repo_gh])

    proc = subprocess.run(cmd, cwd=repo_cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    if proc.returncode != 0:
        err = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"Failed to fetch issue #{issue_number}: {err}")

    data = json.loads(proc.stdout)
    title = data.get("title", "")
    body = data.get("body", "")
    return f"# {title}\n\n{body}"


class GitWorktreeManager:
    """Manages an isolated git worktree lifecycle for autonomous repair."""

    def __init__(self, repo_dir: str, branch_name: str, base_ref: str = "HEAD", worktree_path: Optional[str] = None):
        self.repo_dir = os.path.abspath(repo_dir)
        self.branch_name = branch_name
        self.base_ref = base_ref
        self.worktree_path = os.path.abspath(worktree_path) if worktree_path else tempfile.mkdtemp(prefix="repair-wt-")
        self._created = False

    def create(self) -> str:
        """Create git worktree and branch."""
        # Ensure worktree directory exists or is clean
        if os.path.exists(self.worktree_path) and os.listdir(self.worktree_path):
            shutil.rmtree(self.worktree_path)

        cmd = [
            "git",
            "-C",
            self.repo_dir,
            "worktree",
            "add",
            "-B",
            self.branch_name,
            self.worktree_path,
            self.base_ref,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
        self._created = True
        return self.worktree_path

    def cleanup(self) -> None:
        """Remove worktree and prune."""
        if not self._created:
            if os.path.exists(self.worktree_path):
                shutil.rmtree(self.worktree_path, ignore_errors=True)
            return

        subprocess.run(
            ["git", "-C", self.repo_dir, "worktree", "remove", "--force", self.worktree_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "-C", self.repo_dir, "worktree", "prune"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if os.path.exists(self.worktree_path):
            shutil.rmtree(self.worktree_path, ignore_errors=True)
        self._created = False

    def __enter__(self):
        self.create()
        return self.worktree_path

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


def parse_edits_from_response(response_text: str) -> List[Dict[str, str]]:
    """Extract code edits from LLM output."""
    edits = []

    diff_blocks = re.split(r"(?:###|FILE:|File:)\s*`?([A-Za-z0-9_./\-]+)`?", response_text)
    if len(diff_blocks) > 1:
        for i in range(1, len(diff_blocks), 2):
            target_path = diff_blocks[i].strip()
            block_content = diff_blocks[i + 1]

            if "<<<<<<< SEARCH" in block_content and ">>>>>>> REPLACE" in block_content:
                parts = block_content.split("<<<<<<< SEARCH")
                for p in parts[1:]:
                    if "=======" in p and ">>>>>>> REPLACE" in p:
                        search_part, rest = p.split("=======", 1)
                        replace_part, _ = rest.split(">>>>>>> REPLACE", 1)
                        edits.append(
                            {
                                "file": target_path,
                                "type": "replace_block",
                                "search": search_part.lstrip("\r\n"),
                                "replace": replace_part.lstrip("\r\n"),
                            }
                        )
            else:
                code_match = re.search(r"```(?:\w+)?\n?(.*?)```", block_content, re.DOTALL)
                if code_match:
                    edits.append(
                        {
                            "file": target_path,
                            "type": "whole_file",
                            "content": code_match.group(1),
                        }
                    )

    if edits:
        return edits

    fenced_matches = re.finditer(
        r"(?:(?:File|FILE|Path):\s*`?([A-Za-z0-9_./\-]+)`?[\s\n]*)?```(?:[a-zA-Z0-9_\-]+)?(?:\s+(?:file=|FILE=)?[\"']?([A-Za-z0-9_./\-]+)[\"']?)?\n(.*?)```",
        response_text,
        re.DOTALL,
    )
    for m in fenced_matches:
        fpath = m.group(1) or m.group(2)
        code = m.group(3)
        if fpath:
            edits.append({"file": fpath.strip(), "type": "whole_file", "content": code})

    return edits


def apply_edits_to_worktree(worktree_dir: str, edits: List[Dict[str, str]]) -> Tuple[bool, str]:
    """Apply parsed edits to target files in the worktree, running AST pre-validation."""
    if not edits:
        return False, "No structured edits or code blocks found in model response."

    modified_files = {}

    for edit in edits:
        rel_path = edit["file"]
        abs_path = os.path.join(worktree_dir, rel_path)

        if edit["type"] == "whole_file":
            new_content = edit["content"]
        elif edit["type"] == "replace_block":
            if not os.path.exists(abs_path):
                return False, f"Target file '{rel_path}' does not exist for search-and-replace block."
            current = modified_files.get(rel_path)
            if current is None:
                with open(abs_path, "r", encoding="utf-8") as f:
                    current = f.read()
            search = edit["search"]
            replace = edit["replace"]
            if search not in current:
                return False, f"Search block not found in file '{rel_path}'."
            new_content = current.replace(search, replace, 1)
        else:
            continue

        val_result = validate_source_syntax(rel_path, new_content)
        if not val_result["valid"]:
            return False, f"AST/Syntax validation error in '{rel_path}' at line {val_result['line']}: {val_result['error']}"

        modified_files[rel_path] = new_content

    for rel_path, content in modified_files.items():
        abs_path = os.path.join(worktree_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

    return True, f"Successfully modified {len(modified_files)} files: {', '.join(modified_files.keys())}"


def format_conventional_commit(task_spec: Dict[str, Any], issue_number: Optional[int] = None) -> str:
    """Generate conventional commit message."""
    target_files = task_spec.get("target_files", [])
    scope = ""
    if target_files:
        first_file = target_files[0]
        base_name = os.path.basename(first_file).split(".")[0]
        scope = f"({base_name})"

    changes = task_spec.get("required_changes", [])
    summary = changes[0] if changes else "resolve deterministic issue"
    summary = re.sub(r"[`*#]", "", summary).strip()
    if len(summary) > 60:
        summary = summary[:57] + "..."

    header = f"fix{scope}: {summary}"
    body_lines = []
    if issue_number:
        body_lines.append(f"Closes #{issue_number}")

    if changes:
        body_lines.append("\nChanges:")
        for c in changes:
            body_lines.append(f"- {c}")

    if task_spec.get("verification_command"):
        body_lines.append(f"\nVerified with: `{task_spec['verification_command']}`")

    return f"{header}\n\n" + "\n".join(body_lines).strip()


class RepairWorker:
    """Coordinates autonomous edit-test validation loop."""

    def __init__(
        self,
        repo_dir: str,
        task_spec: Dict[str, Any],
        issue_number: Optional[int] = None,
        model: str = DEFAULT_MODEL,
        ollama_host: str = DEFAULT_OLLAMA_HOST,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        regression_cmd: Optional[str] = None,
        logger=None,
    ):
        self.repo_dir = os.path.abspath(repo_dir)
        self.task_spec = task_spec
        self.issue_number = issue_number
        self.model = model
        self.ollama_host = ollama_host
        self.max_iterations = max_iterations
        self.regression_cmd = regression_cmd
        self.logger = logger or (lambda msg: print(msg, flush=True))

    def run(self) -> Dict[str, Any]:
        """Execute autonomous repair loop."""
        start_time = time.time()
        self.logger(f"Starting Local Repair Worker for issue {self.issue_number or 'manual'}")
        self.logger(f"Model: {self.model} via {self.ollama_host}")
        self.logger(f"Target files: {self.task_spec.get('target_files')}")
        self.logger(f"Verification command: {self.task_spec.get('verification_command')}")

        branch_suffix = f"issue-{self.issue_number}" if self.issue_number else f"task-{int(start_time)}"
        repair_branch = f"agent/local-repair-{branch_suffix}"

        worktree_mgr = GitWorktreeManager(self.repo_dir, repair_branch)

        try:
            worktree_dir = worktree_mgr.create()
            self.logger(f"Created isolated worktree at: {worktree_dir} on branch {repair_branch}")

            ver_cmd = self.task_spec["verification_command"]
            self.logger(f"Running initial reproduction check: `{ver_cmd}`")
            code, stdout, stderr = run_cmd_in_dir(ver_cmd, worktree_dir)
            if code == 0:
                self.logger("Warning: Verification command passed initially! Checking if reproduction required failure.")
                initial_output = (stdout + "\n" + stderr).strip()
            else:
                self.logger(f"Reproduction confirmed failure (exit {code}).")
                initial_output = (stdout + "\n" + stderr).strip()

            current_error_trace = initial_output
            iteration = 0
            resolved = False

            while iteration < self.max_iterations:
                iteration += 1
                self.logger(f"--- Iteration {iteration}/{self.max_iterations} ---")

                file_contexts = []
                for rel_path in self.task_spec.get("target_files", []):
                    full_p = os.path.join(worktree_dir, rel_path)
                    content = ""
                    if os.path.exists(full_p):
                        with open(full_p, "r", encoding="utf-8") as f:
                            content = f.read()
                    file_contexts.append(f"File: `{rel_path}`\n```\n{content}\n```")

                joined_contexts = "\n\n".join(file_contexts)
                prompt = (
                    f"You are an expert autonomous code repair agent fixing a specific bug.\n"
                    f"Task instructions:\n"
                    f"Target files:\n"
                    + "\n".join(f"- {f}" for f in self.task_spec.get("target_files", []))
                    + f"\n\nRequired changes:\n"
                    + "\n".join(f"- {c}" for c in self.task_spec.get("required_changes", []))
                    + f"\n\nVerification command:\n`{ver_cmd}`\n\n"
                    f"Error trace / test failure:\n"
                    f"```\n{current_error_trace[-3000:]}\n```\n\n"
                    f"Current target file contents:\n{joined_contexts}\n\n"
                    f"Output the complete fixed file content for each modified file in a standard markdown fenced code block, "
                    f"with the file path clearly indicated above the code block like:\n"
                    f"File: `path/to/file`\n```[lang]\n...full updated content...\n```\n"
                    f"Do not include conversational filler."
                )

                self.logger(f"Querying model {self.model}...")
                model_reply = query_ollama(prompt, model=self.model, ollama_host=self.ollama_host)
                self.logger(f"Model replied ({len(model_reply)} chars). Parsing edits...")

                edits = parse_edits_from_response(model_reply)
                if not edits:
                    self.logger("Could not parse structured edits from model response.")
                    current_error_trace = "Parser error: No valid code blocks or target files found in response."
                    continue

                applied, msg = apply_edits_to_worktree(worktree_dir, edits)
                self.logger(f"Edit application: {msg}")
                if not applied:
                    current_error_trace = f"Syntax / AST error: {msg}"
                    continue

                self.logger(f"Re-running verification command: `{ver_cmd}`...")
                v_code, v_stdout, v_stderr = run_cmd_in_dir(ver_cmd, worktree_dir)
                if v_code == 0:
                    self.logger(f"Verification command passed on iteration {iteration}!")
                    resolved = True
                    break
                else:
                    self.logger(f"Verification failed on iteration {iteration} (exit {v_code}).")
                    current_error_trace = (v_stdout + "\n" + v_stderr).strip()

            if not resolved:
                return {
                    "success": False,
                    "error": f"Failed to resolve issue within {self.max_iterations} iterations.",
                    "iterations": iteration,
                    "branch": repair_branch,
                    "elapsed_seconds": round(time.time() - start_time, 2),
                }

            if self.regression_cmd:
                self.logger(f"Running regression command: `{self.regression_cmd}`...")
                r_code, r_stdout, r_stderr = run_cmd_in_dir(self.regression_cmd, worktree_dir)
                if r_code != 0:
                    self.logger("Regression check failed!")
                    return {
                        "success": False,
                        "error": f"Regression check failed: {r_stderr[-500:]}",
                        "iterations": iteration,
                        "branch": repair_branch,
                        "elapsed_seconds": round(time.time() - start_time, 2),
                    }
                self.logger("Regression checks passed clean.")

            commit_msg = format_conventional_commit(self.task_spec, self.issue_number)
            run_cmd_in_dir("git add -A", worktree_dir)
            import shlex as _shlex
            c_code, c_stdout, c_stderr = run_cmd_in_dir(
                f"git commit -m {_shlex.quote(commit_msg)}",
                worktree_dir,
            )
            self.logger(f"Committed fix on branch {repair_branch}.")

            _, diff_out, _ = run_cmd_in_dir("git diff HEAD~1 --stat", worktree_dir)

            return {
                "success": True,
                "branch": repair_branch,
                "commit_message": commit_msg,
                "diff_summary": diff_out.strip(),
                "iterations": iteration,
                "elapsed_seconds": round(time.time() - start_time, 2),
            }

        finally:
            worktree_mgr.cleanup()
            self.logger("Cleaned up isolated worktree.")


def main() -> None:
    """CLI Entrypoint for local repair worker."""
    parser = argparse.ArgumentParser(description="Local Autonomous Repair Worker ('Local Jules')")
    parser.add_argument("--repo-dir", default=".", help="Repository root directory")
    parser.add_argument("--issue", type=int, help="GitHub issue number to fetch and resolve")
    parser.add_argument("--repo-gh", help="GitHub repo full_name (e.g. arii/remote-server)")
    parser.add_argument("--spec-file", help="Path to markdown task spec file")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Local Ollama model name")
    parser.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST, help="Ollama API host")
    parser.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS, help="Max repair iterations")
    parser.add_argument("--regression-cmd", help="Full test suite command to verify zero regressions")
    parser.add_argument("--publish-pr", action="store_true", help="Push branch and create PR via gh CLI on success")

    args = parser.parse_args()

    if args.spec_file:
        with open(args.spec_file, "r", encoding="utf-8") as f:
            content = f.read()
    elif args.issue:
        content = fetch_issue_body(args.repo_dir, args.issue, args.repo_gh)
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        parser.error("Must provide --spec-file, --issue, or task markdown via stdin.")

    try:
        spec = parse_task_spec(content)
    except ValueError as err:
        sys.stderr.write(f"Task qualification error: {err}\n")
        sys.exit(1)

    worker = RepairWorker(
        repo_dir=args.repo_dir,
        task_spec=spec,
        issue_number=args.issue,
        model=args.model,
        ollama_host=args.ollama_host,
        max_iterations=args.max_iterations,
        regression_cmd=args.regression_cmd,
    )

    result = worker.run()
    print(json.dumps(result, indent=2))

    if result.get("success") and args.publish_pr:
        branch = result["branch"]
        print(f"Publishing repair PR for branch {branch}...")
        subprocess.run(["git", "-C", args.repo_dir, "push", "-u", "origin", branch], check=False)
        pr_title = result["commit_message"].splitlines()[0]
        pr_body = (
            f"Autonomous repair resolution for issue #{args.issue}.\n\n"
            f"Iterations: {result.get('iterations')}\n"
            f"Diff summary:\n```\n{result.get('diff_summary')}\n```\n"
        )
        gh_cmd = ["gh", "pr", "create", "--head", branch, "--title", pr_title, "--body", pr_body]
        if args.repo_gh:
            gh_cmd.extend(["--repo", args.repo_gh])
        subprocess.run(gh_cmd, cwd=args.repo_dir, check=False)

        # Post status and diff summary back to original issue if issue number provided
        if args.issue:
            comment_body = (
                f"Autonomous repair resolution created PR on branch `{branch}`.\n\n"
                f"Iterations: {result.get('iterations')}\n"
                f"Elapsed: {result.get('elapsed_seconds')}s\n\n"
                f"Diff summary:\n```\n{result.get('diff_summary')}\n```"
            )
            issue_cmd = ["gh", "issue", "comment", str(args.issue), "--body", comment_body]
            if args.repo_gh:
                issue_cmd.extend(["--repo", args.repo_gh])
            subprocess.run(issue_cmd, cwd=args.repo_dir, check=False)

    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()

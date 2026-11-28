#!/usr/bin/env python3
"""Deterministic Task Specification Parser.

Parses markdown task specifications defined according to the deterministic-task
issue template, extracting target files, verification commands, required changes,
and expected output/constraints.
"""

import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List


class TaskSpecError(ValueError):
    """The issue body is not scaffolded for repair.

    Subclasses ValueError so existing callers catching ValueError
    keep working; the distinct type lets the repair worker and the
    dispatch gates tell "issue lacks the scaffold" apart from other
    failures and answer actionably instead of "exit 2".
    """


# Actionable tail for unscaffolded-issue failures (issue #908):
# names the template that carries the required sections.
SCAFFOLD_HINT = (
    "add the missing section(s) from the Deterministic Task template "
    "(.github/ISSUE_TEMPLATE/deterministic-task.md: 'Target Files' + "
    "'Verification Command')"
)


def validate_target_path(path) -> bool:
    """True when a target entry stays inside the checkout.

    Root-independent pre-worktree gate: absolute paths and dotdot
    escapes reject; interior dotdot (pkg/../ok.py) is fine. The
    worker re-checks the joined path with realpath, which additionally
    catches symlink escapes this cannot see.
    """
    if not isinstance(path, str) or not path.strip():
        return False
    if os.path.isabs(path):
        return False
    norm = os.path.normpath(path)
    return norm != ".." and not norm.startswith(".." + os.sep)


def _clean_markdown_comments(text: str) -> str:
    """Removes HTML/Markdown comments (<!-- ... -->)."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _clean_code_ticks(item: str) -> str:
    """Strips leading/trailing inline code ticks and whitespace."""
    item = item.strip()
    if item.startswith("`") and item.endswith("`") and len(item) >= 2:
        item = item[1:-1].strip()
    return item


def _extract_list_items(section_text: str) -> List[str]:
    """Extracts non-empty items from a markdown list (bulleted or numbered) or lines."""
    items = []
    lines = section_text.splitlines()
    current_item = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Check if line starts a list item: - , * , + , •, or 1. , 1)
        match = re.match(r"^[-*+•]\s+(.*)$|^(\d+[\.\)])\s+(.*)$", stripped)
        if match:
            if current_item:
                items.append("\n".join(current_item).strip())
                current_item = []
            if match.group(1) is not None:
                current_item.append(match.group(1).strip())
            else:
                current_item.append(match.group(3).strip())
        else:
            if current_item:
                current_item.append(stripped)
            else:
                current_item.append(stripped)

    if current_item:
        items.append("\n".join(current_item).strip())

    return [item for item in items if item]


def _clean_command(command_text: str) -> str:
    """Extracts command string, removing fenced code blocks and extra whitespace."""
    # Check for fenced code block: ```[language] ... ```
    code_block_match = re.search(r"```(?:\w+)?\n?(.*?)```", command_text, re.DOTALL)
    if code_block_match:
        content = code_block_match.group(1)
    else:
        content = command_text

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    cleaned = "\n".join(lines).strip()
    return _clean_code_ticks(cleaned)


def parse_task_spec(markdown_body: str) -> Dict[str, Any]:
    """Deterministically parse and extract task specifications from markdown body.

    Returns a dict with:
      - target_files: list[str]
      - verification_command: str
      - required_changes: list[str]
      - expected_output: str

    Raises:
      TaskSpecError: If required sections (Target Files or Verification
                  Command) are missing or empty (a ValueError
                  subclass, so ValueError catches still work).
      ValueError: If markdown_body is not a string.
    """
    if not isinstance(markdown_body, str):
        raise ValueError("markdown_body must be a string")

    # Remove YAML frontmatter if present
    content = markdown_body
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2]

    # Clean HTML/markdown comments
    content = _clean_markdown_comments(content)

    # Split into sections by markdown headings (e.g., #, ##, ###)
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    matches = list(heading_pattern.finditer(content))

    sections: Dict[str, str] = {}
    for idx, match in enumerate(matches):
        raw_heading = match.group(2).strip()
        # Normalize heading key: lower case, strip colons and trailing characters
        heading_key = raw_heading.strip().rstrip(":").strip().lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        section_body = content[start:end].strip()
        sections[heading_key] = section_body

    # Map normalized heading keys to target attributes
    # We look for "target files", "verification command", "required changes",
    # and "expected output" (or "expected output / constraints")
    def find_section(keywords: List[str]) -> str | None:
        for k, v in sections.items():
            for kw in keywords:
                if kw in k:
                    return v
        return None

    target_files_raw = find_section(["target files", "target file"])
    verification_raw = find_section(
        ["verification command", "verification commands", "verification"]
    )
    required_changes_raw = find_section(["required changes", "required change"])
    expected_output_raw = find_section(
        ["expected output", "expected output / constraints", "constraints"]
    )

    if target_files_raw is None:
        raise TaskSpecError("Missing required section: 'Target Files'")

    # Extract target files
    raw_files = _extract_list_items(target_files_raw)
    target_files = []
    for f in raw_files:
        # Split on newline if multiple in item or clean ticks
        for line in f.splitlines():
            cleaned_file = _clean_code_ticks(line.strip())
            # Strip list symbols if any remain
            cleaned_file = re.sub(r"^[-*+•]\s+", "", cleaned_file).strip()
            cleaned_file = _clean_code_ticks(cleaned_file)
            if cleaned_file and cleaned_file not in ("-", "*", "+", "•"):
                if not validate_target_path(cleaned_file):
                    raise TaskSpecError(
                        "'Target Files' entry escapes the checkout: "
                        f"{cleaned_file[:200]!r}"
                    )
                target_files.append(cleaned_file)

    if not target_files:
        raise TaskSpecError(
            "'Target Files' section is empty or contains no valid file paths"
        )

    if verification_raw is None:
        raise TaskSpecError("Missing required section: 'Verification Command'")

    verification_command = _clean_command(verification_raw)
    if not verification_command:
        raise TaskSpecError(
            "'Verification Command' section is empty or contains no valid command"
        )

    required_changes: List[str] = []
    if required_changes_raw:
        required_changes = _extract_list_items(required_changes_raw)

    expected_output = expected_output_raw.strip() if expected_output_raw else ""

    return {
        "target_files": target_files,
        "verification_command": verification_command,
        "required_changes": required_changes,
        "expected_output": expected_output,
    }


try:
    from issue_generator import evidence_cites_repo_file
except ImportError:
    try:
        from deep_agent.issue_generator import evidence_cites_repo_file
    except ImportError:
        evidence_cites_repo_file = None

_REPO_FILE_RE = re.compile(r"(?<![\w/])([\w./-]+\.\w+)(?::\d+(?:,\d+)*)?")


def extract_target_files(body: str, repo_root: str | Path) -> list[str]:
    """Derive verified repo-relative file paths from markdown body text.

    Scans paths cited in body text, backticks, and list items, verifying each
    against repo_root so only existing checkout files are returned.
    """
    if not body or not repo_root:
        return []
    repo_path = Path(repo_root)
    if not repo_path.is_dir():
        return []

    hits: list[str] = []
    if evidence_cites_repo_file is not None:
        try:
            for rel in evidence_cites_repo_file(body, str(repo_path)):
                if validate_target_path(rel) and rel not in hits:
                    hits.append(rel)
        except Exception:
            pass

    raw_candidates: list[str] = []
    for match in _REPO_FILE_RE.finditer(body):
        raw_candidates.append(match.group(1))

    for span in re.findall(r"`([^`\n]+)`", body):
        span = span.strip()
        if re.search(r"\.\w+", span):
            raw_candidates.append(span)

    for item in raw_candidates:
        candidate = item.strip().strip("`'\" \t\r\n")
        candidate = re.sub(r":\d+(?:,\d+)*$", "", candidate)
        candidate = re.sub(r":\d+:\d+$", "", candidate)
        candidate = candidate.lstrip("./")
        if (
            not candidate
            or candidate.startswith(".git/")
            or ".." in candidate.split("/")
        ):
            continue
        if not validate_target_path(candidate):
            continue
        full_path = repo_path / candidate
        if full_path.is_file() and candidate not in hits:
            hits.append(candidate)

    return hits


def derive_verification_command(
    body: str, repo_root: str | Path, target_files: list[str] | None = None
) -> str | None:
    """Derive an allowlisted verification command from body, target files, or repo."""
    from backend.verify_allowlist import validate_verify_command

    repo_path = Path(repo_root) if repo_root else None

    # 1. Search for allowlisted command inside body (fenced code blocks, backticks, lines)
    if body:
        for block in re.findall(r"```(?:\w+)?\n?(.*?)```", body, re.DOTALL):
            cmd = _clean_code_ticks(block.strip())
            if validate_verify_command(cmd):
                return cmd
        for tick in re.findall(r"`([^`\n]+)`", body):
            cmd = tick.strip()
            if validate_verify_command(cmd):
                return cmd
        for line in body.splitlines():
            line = line.strip().strip("$#` ")
            if validate_verify_command(line):
                return line

    files = target_files or []

    # 2. Check if any target file is itself a test file
    for tf in files:
        base = os.path.basename(tf).lower()
        parts = tf.lower().split("/")
        is_test = (
            base.startswith("test_")
            or base.endswith(
                (
                    "_test.py",
                    ".test.ts",
                    ".test.tsx",
                    ".spec.ts",
                    ".spec.tsx",
                    ".test.js",
                    ".spec.js",
                )
            )
            or "tests" in parts
            or "test" in parts
        )
        if is_test:
            if tf.endswith(".py"):
                cmd = f"pytest {tf}"
                if validate_verify_command(cmd):
                    return cmd
            elif tf.endswith((".ts", ".tsx", ".js", ".jsx")):
                cmd = f"vitest run {tf}"
                if validate_verify_command(cmd):
                    return cmd
                cmd = f"npx vitest run {tf}"
                if validate_verify_command(cmd):
                    return cmd

    # 3. Check for matching test file in repo for each target file
    if repo_path and repo_path.is_dir():
        for tf in files:
            name, ext = os.path.splitext(os.path.basename(tf))
            if ext == ".py":
                candidates = [
                    f"tests/test_{name}.py",
                    f"test/test_{name}.py",
                    os.path.join(os.path.dirname(tf), f"test_{name}.py"),
                    os.path.join(os.path.dirname(tf), "tests", f"test_{name}.py"),
                ]
                for cand in candidates:
                    if (repo_path / cand).is_file():
                        cmd = f"pytest {cand}"
                        if validate_verify_command(cmd):
                            return cmd
            elif ext in (".ts", ".tsx", ".js", ".jsx"):
                candidates = [
                    os.path.join(os.path.dirname(tf), f"{name}.test{ext}"),
                    os.path.join("src", "test", f"{name}.test{ext}"),
                    os.path.join("tests", f"{name}.test{ext}"),
                ]
                for cand in candidates:
                    if (repo_path / cand).is_file():
                        cmd = f"vitest run {cand}"
                        if validate_verify_command(cmd):
                            return cmd

    # 4. Fallback to repo test command
    if repo_path and repo_path.is_dir():
        pkg_json = repo_path / "package.json"
        if pkg_json.is_file():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {})
                test_script = scripts.get("test", "")
                if test_script and validate_verify_command(test_script):
                    return test_script
                if "vitest" in test_script or "vitest" in data.get(
                    "devDependencies", {}
                ):
                    cmd = "vitest run"
                    if validate_verify_command(cmd):
                        return cmd
            except Exception:
                pass

        if (repo_path / "tests").is_dir():
            has_py = any(
                p.endswith(".py")
                for _, _, fnames in os.walk(repo_path / "tests")
                for p in fnames
            )
            if has_py:
                cmd = "pytest tests/"
                if validate_verify_command(cmd):
                    return cmd
        if (repo_path / "pyproject.toml").is_file() or (
            repo_path / "pytest.ini"
        ).is_file():
            cmd = "pytest"
            if validate_verify_command(cmd):
                return cmd

    return None


def scaffold_task_spec(
    body: str, repo_root: str | Path
) -> tuple[dict | None, str | None]:
    """Derive target files and verification command for an unscaffolded issue.

    Returns ({'target_files': [...], 'verification_command': '...'}, None) on success,
    or (None, failure_reason) on failure.
    """
    targets = extract_target_files(body, repo_root)
    if not targets:
        return None, "no target files found in issue body that exist in repo tree"

    cmd = derive_verification_command(body, repo_root, targets)
    if not cmd:
        return None, "no allowlisted verification command could be derived"

    return {
        "target_files": targets,
        "verification_command": cmd,
    }, None


def format_scaffold_sections(target_files: list[str], verification_command: str) -> str:
    """Format Target Files and Verification Command markdown sections."""
    lines = [
        "## Target Files",
    ]
    for tf in target_files:
        lines.append(f"- `{tf}`")
    lines.extend(
        [
            "",
            "## Verification Command",
            "",
            "```",
            verification_command,
            "```",
        ]
    )
    return "\n".join(lines)


def main(argv=None) -> None:
    """CLI entrypoint to parse task spec from file or stdin."""
    args = sys.argv[1:] if argv is None else argv
    if args:
        filepath = args[0]
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = sys.stdin.read()

    try:
        parsed = parse_task_spec(content)
        print(json.dumps(parsed, indent=2))
    except ValueError as err:
        sys.stderr.write(f"Error parsing task specification: {err}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

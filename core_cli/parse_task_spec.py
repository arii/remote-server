#!/usr/bin/env python3
"""Deterministic Task Specification Parser.

Parses markdown task specifications defined according to the deterministic-task
issue template, extracting target files, verification commands, required changes,
and expected output/constraints.
"""

import json
import re
import sys
from typing import Any, Dict, List


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
        # Check if line starts a list item: - , * , + , or 1. , 1)
        match = re.match(r"^[-*+]\s+(.*)$|^(\d+[\.\)])\s+(.*)$", stripped)
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
      ValueError: If required sections (Target Files or Verification Command)
                  are missing or empty.
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
    verification_raw = find_section(["verification command", "verification commands", "verification"])
    required_changes_raw = find_section(["required changes", "required change"])
    expected_output_raw = find_section(["expected output", "expected output / constraints", "constraints"])

    if target_files_raw is None:
        raise ValueError("Missing required section: 'Target Files'")

    # Extract target files
    raw_files = _extract_list_items(target_files_raw)
    target_files = []
    for f in raw_files:
        # Split on newline if multiple in item or clean ticks
        for line in f.splitlines():
            cleaned_file = _clean_code_ticks(line.strip())
            # Strip list symbols if any remain
            cleaned_file = re.sub(r"^[-*+]\s+", "", cleaned_file).strip()
            cleaned_file = _clean_code_ticks(cleaned_file)
            if cleaned_file and cleaned_file not in ("-", "*", "+"):
                target_files.append(cleaned_file)

    if not target_files:
        raise ValueError("'Target Files' section is empty or contains no valid file paths")

    if verification_raw is None:
        raise ValueError("Missing required section: 'Verification Command'")

    verification_command = _clean_command(verification_raw)
    if not verification_command:
        raise ValueError("'Verification Command' section is empty or contains no valid command")

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

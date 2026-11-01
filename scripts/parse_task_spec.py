#!/usr/bin/env python3
"""Task specification parser.

Parses issue bodies or markdown specifications to extract target files
and verification commands for autonomous code repair.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import TypedDict


class ParsedTaskSpec(TypedDict):
    target_files: list[str]
    verification_cmd: str
    description: str


def parse_task_spec(issue_body: str) -> ParsedTaskSpec:
    """Parse task specification from an issue body.

    Extracts:
      - Target files (under sections like 'Target Files', 'Files:', etc.)
      - Verification command (under sections like 'Verification Command', etc.)
      - Clean description of the task
    """
    target_files: list[str] = []
    verification_cmd = ""
    description_lines: list[str] = []

    lines = issue_body.splitlines()
    current_section = ""

    # Patterns for section headers
    target_files_header = re.compile(
        r"^(?:#+\s*)?(?:target\s+files?|files?\s+to\s+modify|files?)\b",
        re.IGNORECASE,
    )
    verification_header = re.compile(
        r"^(?:#+\s*)?(?:verification\s+commands?|verification|test\s+commands?)\b",
        re.IGNORECASE,
    )

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check section boundaries
        header_clean = stripped.lstrip("#").strip().rstrip(":")
        if target_files_header.match(header_clean):
            current_section = "target_files"
            i += 1
            continue
        elif verification_header.match(header_clean):
            current_section = "verification_cmd"
            i += 1
            continue
        elif stripped.startswith("#"):
            current_section = "other"

        if current_section == "target_files":
            if stripped.startswith("#"):
                current_section = "other"
                continue
            if stripped:
                # Remove markdown list markers, backticks, commas
                cleaned = re.sub(r"^[-*•\d\.\s]+", "", stripped).strip()
                cleaned = cleaned.strip("`'\",")
                # Split whitespace or commas if multiple files per line
                parts = [p.strip("`'\",") for p in re.split(r"[\s,]+", cleaned) if p.strip("`'\",")]
                for part in parts:
                    if part and not part.startswith("#"):
                        target_files.append(part)
        elif current_section == "verification_cmd":
            if stripped.startswith("#"):
                current_section = "other"
                continue
            if stripped:
                # If enclosed in a code block or line, extract the command
                if stripped.startswith("```"):
                    # Collect until closing fence
                    cmd_lines = []
                    i += 1
                    while i < len(lines) and not lines[i].strip().startswith("```"):
                        cmd_lines.append(lines[i])
                        i += 1
                    if not verification_cmd:
                        verification_cmd = "\n".join(cmd_lines).strip()
                else:
                    cmd_line = stripped.strip("`")
                    if not verification_cmd:
                        verification_cmd = cmd_line
        else:
            description_lines.append(line)

        i += 1

    # Fallback regex search if not found in section headers
    if not target_files:
        tf_match = re.search(
            r"(?:target\s+files?|files?):\s*([^\n\r]+)",
            issue_body,
            re.IGNORECASE,
        )
        if tf_match:
            raw = tf_match.group(1).strip()
            parts = [p.strip("`'\", ") for p in re.split(r"[,\s]+", raw) if p.strip("`'\", ")]
            target_files.extend(parts)

    if not verification_cmd:
        vc_match = re.search(
            r"(?:verification\s+command|verify\s+with|test\s+command):\s*[`]?([^`\n\r]+)[`]?",
            issue_body,
            re.IGNORECASE,
        )
        if vc_match:
            verification_cmd = vc_match.group(1).strip()

    # Deduplicate while preserving order
    seen = set()
    deduped_targets = []
    for tf in target_files:
        if tf not in seen:
            seen.add(tf)
            deduped_targets.append(tf)

    return {
        "target_files": deduped_targets,
        "verification_cmd": verification_cmd.strip(),
        "description": "\n".join(description_lines).strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse task specification from issue body")
    parser.add_argument("file", nargs="?", help="File containing task specification (reads stdin if omitted)")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = sys.stdin.read()

    result = parse_task_spec(content)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

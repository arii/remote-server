#!/usr/bin/env python3
"""AST syntax pre-validator.

Fast AST syntax checks before running tests or compilers.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys


def sanitize_relative_path(path_str: str) -> str:
    """Sanitize path to ensure safe relative access."""
    clean = os.path.normpath(path_str.strip()).lstrip("/\\")
    if clean.startswith("..") or ".." in clean.split(os.sep):
        raise ValueError(f"Unsafe path traversal detected: {path_str}")
    return clean


def validate_source_syntax(file_path: str, content: str) -> tuple[bool, str]:
    """Validate syntax of code content for a given file.

    Returns:
        (is_valid, error_message): Tuple where is_valid is True if syntax passes,
        False otherwise with error_message detailing the parse issue.
    """
    try:
        clean_path = sanitize_relative_path(file_path)
    except ValueError as e:
        return False, str(e)

    ext = os.path.splitext(clean_path)[1].lower()

    if ext == ".py":
        try:
            ast.parse(content, filename=clean_path)
            return True, ""
        except SyntaxError as e:
            msg = f"SyntaxError in {clean_path} at line {e.lineno}, col {e.offset}: {e.msg}\n{e.text or ''}"
            return False, msg.strip()
        except Exception as e:
            return False, f"AST parse error in {clean_path}: {e}"

    if ext == ".json":
        try:
            json.loads(content)
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"JSONDecodeError in {clean_path} at line {e.lineno}, col {e.colno}: {e.msg}"

    # Default pass for other file extensions
    return True, ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate syntax of source files")
    parser.add_argument("file", help="Path to file to validate")
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        content = f.read()

    valid, err = validate_source_syntax(args.file, content)
    if not valid:
        print(f"FAILED: {err}", file=sys.stderr)
        sys.exit(1)
    else:
        print("OK: Syntax valid")
        sys.exit(0)


if __name__ == "__main__":
    main()

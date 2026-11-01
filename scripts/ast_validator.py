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


def validate_source_syntax(file_path: str, content: str) -> tuple[bool, str]:
    """Validate syntax of code content for a given file.

    Returns:
        (is_valid, error_message): Tuple where is_valid is True if syntax passes,
        False otherwise with error_message detailing the parse issue.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".py":
        try:
            ast.parse(content, filename=file_path)
            return True, ""
        except SyntaxError as e:
            msg = f"SyntaxError in {file_path} at line {e.lineno}, col {e.offset}: {e.msg}\n{e.text or ''}"
            return False, msg.strip()
        except Exception as e:
            return False, f"AST parse error in {file_path}: {e}"

    if ext == ".json":
        try:
            json.loads(content)
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"JSONDecodeError in {file_path} at line {e.lineno}, col {e.colno}: {e.msg}"

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

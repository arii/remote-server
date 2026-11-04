#!/usr/bin/env python3
"""AST and syntax pre-validation filter for code edits.

Validates syntax before running expensive integration test suites.
Python parses via ast; JS-family files use node --check (.js) or the
TypeScript transpiler (.ts/.tsx/.jsx, syntax only). When the
toolchain is absent the bracket heuristic applies and the result
carries a "degraded" label so a valid never silently means
compiler-verified.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, Tuple


def _check_bracket_balance(code: str) -> Tuple[bool, Optional[str], Optional[int]]:
    """Check bracket, parenthesis, and brace balance.

    Used as fallback when external compilers/interpreters are not available or
    for file formats with non-standard syntax (JSX/TSX).
    Ignores brackets inside single quotes, double quotes, template literals,
    single-line comments (//), and multi-line comments (/* ... */).
    """
    stack = []
    matching = {")": "(", "]": "[", "}": "{"}
    lines = code.splitlines()

    in_single = False
    in_double = False
    in_template = False
    in_block_comment = False

    for line_idx, line in enumerate(lines, start=1):
        i = 0
        while i < len(line):
            ch = line[i]

            if in_block_comment:
                if ch == "*" and i + 1 < len(line) and line[i + 1] == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue

            if in_single:
                if ch == "\\":
                    i += 2
                    continue
                if ch == "'":
                    in_single = False
                i += 1
                continue

            if in_double:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_double = False
                i += 1
                continue

            if in_template:
                if ch == "\\":
                    i += 2
                    continue
                if ch == "`":
                    in_template = False
                i += 1
                continue

            # Outside comments and string literals
            if ch == "/" and i + 1 < len(line):
                next_ch = line[i + 1]
                if next_ch == "/":
                    # Single-line comment: skip remaining characters on this line
                    break
                elif next_ch == "*":
                    in_block_comment = True
                    i += 2
                    continue

            if ch == "'":
                in_single = True
                i += 1
                continue
            elif ch == '"':
                in_double = True
                i += 1
                continue
            elif ch == "`":
                in_template = True
                i += 1
                continue

            if ch in "({[":
                stack.append((ch, line_idx))
            elif ch in ")}]":
                if not stack:
                    return False, f"Unmatched '{ch}' at line {line_idx}", line_idx
                top, top_line = stack.pop()
                if matching[ch] != top:
                    return (
                        False,
                        f"Mismatched closing bracket '{ch}' at line {line_idx}, expected closing for '{top}' from line {top_line}",
                        line_idx,
                    )

            i += 1

    if in_block_comment:
        return False, "Unclosed block comment", len(lines)
    if in_single:
        return False, "Unclosed single quote string literal", len(lines)
    if in_double:
        return False, "Unclosed double quote string literal", len(lines)
    if in_template:
        return False, "Unclosed template literal", len(lines)

    if stack:
        top, top_line = stack[-1]
        return False, f"Unclosed '{top}' opened at line {top_line}", top_line

    return True, None, None


def _validate_python(code: str) -> Dict[str, Any]:
    """Validate Python code syntax using ast.parse."""
    try:
        ast.parse(code)
        return {"valid": True, "error": None, "line": None}
    except SyntaxError as e:
        line_num = e.lineno
        offset = e.offset
        msg = e.msg or "SyntaxError"
        if line_num is not None and offset is not None:
            error_str = f"SyntaxError: {msg} at line {line_num}, offset {offset}"
        elif line_num is not None:
            error_str = f"SyntaxError: {msg} at line {line_num}"
        else:
            error_str = f"SyntaxError: {msg}"
        return {"valid": False, "error": error_str, "line": line_num}


_TS_DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "ts_check.js")


def _ts_lib_dir() -> Optional[str]:
    """Directory containing typescript.js, or None.

    TS_LIB_DIR first (worker image), else the repo's own frontend
    toolchain (dev checkouts with node_modules installed).
    """
    env = os.environ.get("TS_LIB_DIR", "").strip()
    if env and os.path.isfile(os.path.join(env, "typescript.js")):
        return env
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dev = os.path.join(repo, "frontend", "node_modules", "typescript",
                       "lib")
    if os.path.isfile(os.path.join(dev, "typescript.js")):
        return dev
    return None


def _transpile_check(node_bin: str, file_path: str,
                     code: str) -> Optional[Dict[str, Any]]:
    """Syntax-check via the TypeScript transpiler. None when the
    toolchain is unusable (caller falls back to brackets)."""
    lib = _ts_lib_dir()
    if lib is None or not os.path.isfile(_TS_DRIVER):
        return None
    ext = os.path.splitext(file_path)[1].lower()
    with tempfile.NamedTemporaryFile("w", suffix=ext or ".ts",
                                     delete=False,
                                     encoding="utf-8") as f:
        f.write(code)
        tmp_name = f.name
    try:
        env = dict(os.environ)
        env["TS_LIB_DIR"] = lib
        proc = subprocess.run(
            [node_bin, _TS_DRIVER, tmp_name],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    if payload.get("fatal") or not isinstance(
            payload.get("errors"), list):
        return None
    errors = payload["errors"]
    if not errors:
        return {"valid": True, "error": None, "line": None,
                "degraded": None}
    first = errors[0] if isinstance(errors[0], dict) else {}
    line = first.get("line")
    msg = str(first.get("message", "Syntax error"))
    return {"valid": False,
            "error": f"{msg} (line {line})" if line else msg,
            "line": line if isinstance(line, int) else None,
            "degraded": None}


def _bracket_result(code: str, reason: str) -> Dict[str, Any]:
    """Bracket-heuristic verdict, always labeled degraded: a valid
    here means balanced, never compiler-verified."""
    is_balanced, err, line_num = _check_bracket_balance(code)
    if not is_balanced:
        return {"valid": False, "error": err, "line": line_num,
                "degraded": reason}
    return {"valid": True, "error": None, "line": None,
            "degraded": reason}


def _validate_javascript_typescript(file_path: str, code: str) -> Dict[str, Any]:
    """Validate JS/TS via node --check (.js), the TypeScript
    transpiler (.ts/.tsx/.jsx), or labeled bracket fallback."""
    ext = os.path.splitext(file_path)[1].lower()
    node_bin = shutil.which("node")

    # .ts/.tsx/.jsx go through the TypeScript transpiler (syntax
    # only, no module resolution): node --check cannot parse types
    # or JSX, and brackets false-pass real errors (issue #488).
    if ext in (".ts", ".tsx", ".jsx"):
        if node_bin:
            res = _transpile_check(node_bin, file_path, code)
            if res is not None:
                return res
        return _bracket_result(
            code, "bracket heuristic (node/typescript unavailable)")

    # Plain JS: node --check when available, labeled fallback otherwise.
    if node_bin:
        try:
            with tempfile.NamedTemporaryFile("w", suffix=ext, delete=False, encoding="utf-8") as f:
                f.write(code)
                tmp_name = f.name
            try:
                proc = subprocess.run(
                    [node_bin, "--check", tmp_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            finally:
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

            if proc.returncode != 0:
                err_output = proc.stderr or proc.stdout
                # Extract line number if present
                line_match = re.search(r":(\d+)", err_output)
                line_num = int(line_match.group(1)) if line_match else None
                first_err_line = err_output.strip().splitlines()[-1] if err_output.strip() else "Syntax error"
                for l in err_output.splitlines():
                    if "SyntaxError" in l or "Error" in l:
                        first_err_line = l.strip()
                        break
                error_msg = f"{first_err_line} (line {line_num})" if line_num else first_err_line
                return {"valid": False, "error": error_msg, "line": line_num,
                        "degraded": None}
            else:
                return {"valid": True, "error": None, "line": None,
                        "degraded": None}
        except Exception:
            # Fall back to bracket balance heuristics if subprocess execution fails
            pass

    return _bracket_result(
        code, "bracket heuristic (node/typescript unavailable)")


def validate_source_syntax(file_path: str, code: str) -> Dict[str, Any]:
    """Validate source syntax for a given file path and code content.

    Returns:
        {
            "valid": bool,
            "error": str or None,
            "line": int or None,
            "degraded": str or None (JS/TS only: set when the
                bracket heuristic stood in for the toolchain)
        }
    """
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    if ext == ".py":
        return _validate_python(code)
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return _validate_javascript_typescript(file_path, code)
    else:
        # Unsupported file types are considered valid
        return {"valid": True, "error": None, "line": None}


def main(argv=None) -> None:
    """CLI runner for ast_validator."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("Usage: ast_validator.py <file_path>")
        sys.exit(1)

    file_path = args[0]
    if not os.path.exists(file_path):
        print(f"Error: File '{file_path}' does not exist.")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        code = f.read()

    result = validate_source_syntax(file_path, code)
    if result.get("degraded"):
        print(f"DEGRADED: {result['degraded']}")
    if result["valid"]:
        print(f"VALID: {file_path}")
        sys.exit(0)
    else:
        print(f"INVALID: {file_path}")
        print(f"Line: {result['line']}")
        print(f"Error: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

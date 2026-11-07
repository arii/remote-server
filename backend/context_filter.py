#!/usr/bin/env python3
"""context_filter — LLM context pre-filtering for CPU inference & triage.

On CPU inference (Ryzen 7 PRO 5850U), prompt token prefill is the primary
latency bottleneck. Unfiltered git diffs containing lockfiles, minified files,
or SVGs and multi-thousand line CI logs exhaust context and cause multi-minute
prefill stalls.

This module provides:
1. Diff pre-filtering:
   - Excludes generated, lock, asset, and minified files by default.
   - Truncates large file diffs exceeding a configurable line threshold.
2. CI log truncation & assertion/panic extraction:
   - Filters out boilerplate CI setup/teardown.
   - Extracts failed steps, assertion failures, panic/stack traces, and exit summaries.
3. Context budget enforcement:
   - Enforces a configurable token/character ceiling (e.g., 4k tokens / 16,000 chars)
     to guarantee prompt prefill finishes in under 15 seconds on CPU.
"""

import fnmatch
import os
import re
import sys

DEFAULT_EXCLUDED_PATTERNS = [
    # Lockfiles
    "*pnpm-lock.yaml",
    "*package-lock.json",
    "*yarn.lock",
    "*Cargo.lock",
    "*poetry.lock",
    "*Gemfile.lock",
    "*composer.lock",
    "*flake.lock",
    # Minified assets & source maps
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.map",
    # Build output dirs
    "dist/*",
    "*/dist/*",
    # Vector and binary assets
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    # Build artifacts & databases
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
]

DEFAULT_MAX_FILE_LINES = 200
DEFAULT_MAX_TOKENS = 4000
CHARS_PER_TOKEN = 4  # Standard approximation for code/log tokenizers

# Structural files hidden behind the pr-diff badge (lockfiles plus
# minified/generated bundles). A subset of the default exclusions:
# binary/image/font assets stay filtered from the diff text but are
# not reported as structural.
STRUCTURAL_DIFF_PATTERNS = [
    # Lockfiles
    "*pnpm-lock.yaml",
    "*package-lock.json",
    "*yarn.lock",
    "*Cargo.lock",
    "*poetry.lock",
    "*Gemfile.lock",
    "*composer.lock",
    "*flake.lock",
    # Minified/generated bundles & build outputs
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "dist/*",
    "*/dist/*",
]


def is_structural_path(path: str) -> bool:
    """Return True if path is a structural file (lockfile or
    minified/generated bundle). Used by the pr-diff endpoint to
    report hidden_structural behind the diff badge."""
    return is_excluded_path(path, STRUCTURAL_DIFF_PATTERNS)


def structural_hidden_files(diff_text: str) -> list:
    """File paths in a unified diff that the structural predicate
    hides, in diff order. Never raises: unparseable input yields []."""
    if not diff_text:
        return []
    try:
        lines = diff_text.splitlines()
    except Exception:
        return []
    paths: list = []
    chunk: list = []

    def flush(c):
        if not c:
            return
        head = []
        for ln in c:
            if ln.startswith("@@ "):
                break
            head.append(ln)
        path = _extract_filepath_from_diff_header(head)
        if path and is_structural_path(path):
            paths.append(path)

    for line in lines:
        if line.startswith("diff --git ") and chunk:
            flush(chunk)
            chunk = []
        chunk.append(line)
    flush(chunk)
    return paths


def estimate_tokens(text: str) -> int:
    """Fast approximation of token count based on length and whitespace/punctuation.

    Useful both programmatically for context budget estimations and as a CLI diagnostic helper.
    """
    if not text:
        return 0
    # Roughly 3.5 to 4 chars per token for code/markdown/diffs
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def is_excluded_path(path: str, patterns=None) -> bool:
    """Return True if path matches any excluded pattern.

    Excludes lockfiles, minified bundles, vector/binary assets, and build artifacts.
    Public diagnostic and programmatic utility used by filter_diff.
    """
    if not path:
        return False
    patterns = patterns if patterns is not None else DEFAULT_EXCLUDED_PATTERNS
    basename = os.path.basename(path)
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(basename, pat):
            return True
    return False


def _extract_filepath_from_diff_header(header_lines):
    """Extract destination or source file path from diff header lines.

    Parses standard git diff header lines ('+++ b/...', '--- a/...', or 'diff --git a/...').
    """
    for line in header_lines:
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("--- a/"):
            return line[6:].strip()
        if line.startswith("diff --git a/"):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


def filter_diff(diff_text: str, max_file_lines: int = DEFAULT_MAX_FILE_LINES,
                excluded_patterns=None) -> str:
    """Filter unified git diff to strip lockfiles/assets and truncate large files."""
    if not diff_text:
        return ""

    if excluded_patterns is None:
        excluded_patterns = DEFAULT_EXCLUDED_PATTERNS

    lines = diff_text.splitlines(keepends=True)
    file_chunks = []
    current_chunk = []

    for line in lines:
        if line.startswith("diff --git ") and current_chunk:
            file_chunks.append(current_chunk)
            current_chunk = []
        current_chunk.append(line)
    if current_chunk:
        file_chunks.append(current_chunk)

    filtered_chunks = []

    for chunk in file_chunks:
        # Separate headers and hunks
        header_lines = []
        hunk_lines = []
        in_hunks = False
        for line in chunk:
            if line.startswith("@@ ") or in_hunks:
                in_hunks = True
                hunk_lines.append(line)
            else:
                header_lines.append(line)

        filepath = _extract_filepath_from_diff_header(header_lines)

        # Check exclusion
        if filepath and is_excluded_path(filepath, excluded_patterns):
            filtered_chunks.append(
                f"[diff pre-filter: excluded generated/binary/lockfile '{filepath}']\n"
            )
            continue

        # Check size threshold
        if max_file_lines and len(hunk_lines) > max_file_lines:
            truncated_hunks = hunk_lines[:max_file_lines]
            omitted = len(hunk_lines) - max_file_lines
            trailer = f"\n[diff pre-filter: truncated {omitted} lines from '{filepath or 'file'}']\n"
            filtered_chunks.append("".join(header_lines) + "".join(truncated_hunks) + trailer)
        else:
            filtered_chunks.append("".join(chunk))

    return "".join(filtered_chunks)


# Patterns identifying high-signal error and trace sections in CI logs
ERROR_START_PATTERNS = [
    re.compile(r"^\s*Traceback \(most recent call last\):", re.I),
    re.compile(r"^\s*thread '[^']+' panicked at", re.I),
    re.compile(r"^\s*panic:", re.I),
    re.compile(r"^\s*AssertionError", re.I),
    re.compile(r"^\s*FAILED\b", re.I),
    re.compile(r"^\s*FAIL\b", re.I),
    re.compile(r"^\s*Error:\s*", re.I),
    re.compile(r"^\s*npm ERR!", re.I),
    re.compile(r"##\[error\]", re.I),
    re.compile(r"^\s*=== RUN\b", re.I),
    re.compile(r"^\s*--- FAIL:", re.I),
]

ERROR_SIGNAL_PATTERNS = [
    re.compile(r"\bfailed with exit code\b", re.I),
    re.compile(r"\bProcess completed with exit code [^0]\b", re.I),
    re.compile(r"\bAssertionError\b", re.I),
    re.compile(r"\bException\b", re.I),
    re.compile(r"\bSyntaxError\b", re.I),
    re.compile(r"\bTypeError\b", re.I),
    re.compile(r"\bpanic:\b", re.I),
    re.compile(r"\bError:\b", re.I),
    re.compile(r"\bFAILED\b"),
]

SETUP_BOILERPLATE_PATTERNS = [
    re.compile(r"##\[group\]Run actions/checkout"),
    re.compile(r"##\[group\]Run actions/setup-"),
    re.compile(r"Setting up auth"),
    re.compile(r"Getting Git version", re.I),
    re.compile(r"Fetching the repository", re.I),
    re.compile(r"Determining the checkout info", re.I),
]


def _is_setup_boilerplate(line: str) -> bool:
    """Return True if line is CI setup/teardown boilerplate without error signal."""
    if not line:
        return False
    if not any(p.search(line) for p in SETUP_BOILERPLATE_PATTERNS):
        return False
    if any(p.search(line) for p in ERROR_START_PATTERNS):
        return False
    if any(p.search(line) for p in ERROR_SIGNAL_PATTERNS):
        return False
    return True


def extract_ci_error_traces(raw_log: str, max_lines: int = 150) -> str:
    """Extract failed steps, stack traces, panics, assertion errors, and exit summaries.

    Filters out CI setup/teardown boilerplate matching SETUP_BOILERPLATE_PATTERNS
    before block extraction so checkout/setup noise never consumes the line budget.
    """
    if not raw_log:
        return ""

    lines = raw_log.splitlines()
    total_lines = len(lines)
    lines = [ln for ln in lines if not _is_setup_boilerplate(ln)]
    if not lines:
        return ""
    if len(lines) <= max_lines:
        filtered = "\n".join(lines)
        if raw_log.endswith("\n") and not filtered.endswith("\n"):
            filtered += "\n"
        return filtered

    extracted_blocks = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check if line initiates an error trace or panic block
        is_start = any(p.search(line) for p in ERROR_START_PATTERNS)
        has_signal = any(p.search(line) for p in ERROR_SIGNAL_PATTERNS)

        if is_start or has_signal:
            # Capture context window: up to 3 lines prior, and up to 30 lines after
            start_idx = max(0, i - 2)
            end_idx = min(len(lines), i + 25)

            # Extend if the block continues showing trace lines (e.g. indentation or 'File "..."')
            while end_idx < len(lines) and (
                lines[end_idx].startswith("  ") or
                lines[end_idx].startswith("\t") or
                lines[end_idx].startswith("at ") or
                any(p.search(lines[end_idx]) for p in ERROR_SIGNAL_PATTERNS)
            ):
                end_idx += 1
                if end_idx - start_idx > 50:
                    break

            block = lines[start_idx:end_idx]
            extracted_blocks.append((start_idx, end_idx, "\n".join(block)))
            i = end_idx
        else:
            i += 1

    if not extracted_blocks:
        # Fallback: take the tail of the log where failures typically reside
        tail = lines[-max_lines:]
        return f"[ci log pre-filter: extracted tail {len(tail)} of {total_lines} lines]\n" + "\n".join(tail)

    # Merge overlapping blocks
    merged_lines = []
    last_end = -1
    for start, end, text in extracted_blocks:
        if start > last_end and last_end != -1:
            merged_lines.append(f"\n[... skipped {start - last_end} non-failing log lines ...]\n")
        merged_lines.append(text)
        last_end = end

    # Include exit code / summary tail if not already covered
    if last_end < len(lines) - 5:
        tail_lines = lines[-5:]
        merged_lines.append("\n[... skipped to exit summary ...]\n" + "\n".join(tail_lines))

    result = "\n".join(merged_lines)
    result_lines = result.splitlines()
    if len(result_lines) > max_lines:
        # Truncate to max_lines keeping header note and tail
        result = (f"[ci log pre-filter: compressed {total_lines} lines -> {max_lines} lines]\n" +
                  "\n".join(result_lines[-max_lines:]))

    return result


def enforce_context_budget(text: str, max_tokens: int = DEFAULT_MAX_TOKENS,
                           chars_per_token: int = CHARS_PER_TOKEN) -> str:
    """Enforce token ceiling on context payloads to guarantee fast CPU prompt prefill."""
    if not text:
        return ""

    max_chars = max_tokens * chars_per_token
    if len(text) <= max_chars:
        return text

    # Truncate text from middle or end to stay strictly within budget
    head_chars = int(max_chars * 0.75)
    tail_chars = max_chars - head_chars - 120

    truncated_msg = f"\n\n[context pre-filter: truncated context to enforce {max_tokens} token budget]\n\n"
    if tail_chars > 0:
        return text[:head_chars] + truncated_msg + text[-tail_chars:]
    return text[:max_chars - len(truncated_msg)] + truncated_msg


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="LLM Context Pre-filtering tool")
    parser.add_argument("--diff", action="store_true", help="Filter git diff on stdin")
    parser.add_argument("--ci-log", action="store_true", help="Extract CI failure traces on stdin")
    parser.add_argument("--budget", action="store_true", help="Enforce context budget on stdin")
    parser.add_argument("--max-file-lines", type=int, default=DEFAULT_MAX_FILE_LINES,
                        help=f"Max lines per file diff (default {DEFAULT_MAX_FILE_LINES})")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help=f"Max tokens budget (default {DEFAULT_MAX_TOKENS})")

    args = parser.parse_args(argv)
    input_text = sys.stdin.read()

    output = input_text
    if args.diff:
        output = filter_diff(output, max_file_lines=args.max_file_lines)
    if args.ci_log:
        output = extract_ci_error_traces(output)
    if args.budget or (not args.diff and not args.ci_log):
        output = enforce_context_budget(output, max_tokens=args.max_tokens)

    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())

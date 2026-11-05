"""Shared CLI entrypoint helpers for deep-agent/ scripts.

Stdlib-only: safe to import from system python3 (validators, readers)
as well as the venv entrypoints. Only patterns repeated 3+ times
across the entrypoints live here; one-off validation stays local.

Helpers return exit codes instead of exiting, so main() functions
stay plain `-> int` and directly testable.
"""

import base64
import os
import sys


def bootstrap():
    """Put this directory first on sys.path for sibling imports.

    Idempotent: under the normal `python3 deep-agent/foo.py`
    invocation the script directory is already sys.path[0], so this
    is a no-op. Call it before importing sibling modules (ledger,
    s5tools, ...).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def repo_root():
    """Checkout root for this deep-agent install (S5_CHECKOUT override).

    Derived from this file's location so worktree and non-production
    runs resolve their own checkout's .env and chroma_db instead of
    the production paths (#540): dotenv loading and the RAG index
    stay hermetic per checkout. Production behavior unchanged: there
    this file lives under the production checkout's deep-agent dir.
    """
    override = os.environ.get("S5_CHECKOUT", "")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def usage_error(*lines):
    """Print usage lines to stderr. Returns 2 (caller: `return` it)."""
    for line in lines:
        print(line, file=sys.stderr)
    return 2


def sorry(message):
    """Print a Sorry line to stdout. Returns 1 (caller: `return` it).

    Exit-1 contract: the reply goes on stdout so the server's chunk
    readers (SSE/phone) show it; stderr-only output would be
    invisible to them.
    """
    print(message, flush=True)
    return 1


def b64decode(raw):
    """Decode a base64 argv payload to str, or None when invalid.

    Never raises: bad base64, non-UTF-8 bytes, and non-str input all
    yield None so the caller can fall through to its usage error.
    Note: like base64.b64decode, characters outside the alphabet are
    ignored, so "!!!" decodes to "" (callers still reject it downstream
    as an empty payload).
    """
    try:
        if not isinstance(raw, str):
            return None
        return base64.b64decode(raw).decode()
    except Exception:
        return None


def _flag(argv, *names):
    """Pop the first matching --flag from argv; returns (found, rest)."""
    rest = list(argv)
    for name in names:
        if name in rest:
            rest.remove(name)
            return True, rest
    return False, rest


def _option(argv, *names):
    """Pop --opt <value> from argv; returns (value-or-None, rest)."""
    rest = list(argv)
    for name in names:
        if name in rest:
            idx = rest.index(name)
            if idx + 1 >= len(rest):
                return None, rest
            value = rest[idx + 1]
            del rest[idx:idx + 2]
            return value, rest
    return None, rest


def main(argv=None):
    """Subcommands: run-pipeline | eval-pipeline. Returns exit code."""
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        return usage_error(
            "usage: cli.py run-pipeline --intent <text> --repo <path>",
            "               [--issue <id>] [--submit-pr]",
            "       cli.py eval-pipeline")
    command, args = args[0], args[1:]
    if command == "eval-pipeline":
        if args:
            return usage_error("usage: cli.py eval-pipeline")
        import eval_pipeline
        return eval_pipeline.main()
    if command != "run-pipeline":
        return usage_error(f"unknown command: {command}",
                           "want: run-pipeline | eval-pipeline")
    intent, args = _option(args, "--intent")
    repo, args = _option(args, "--repo")
    issue, args = _option(args, "--issue")
    submit, args = _flag(args, "--submit-pr")
    if not intent or not repo or args:
        return usage_error(
            "usage: cli.py run-pipeline --intent <text> --repo <path>",
            "               [--issue <id>] [--submit-pr]")
    try:
        issue_id = int(issue) if issue else None
    except ValueError:
        return usage_error(f"bad --issue: {issue!r}")
    import pipeline as pipeline_mod
    pipe = pipeline_mod.Pipeline(
        repo, intent, issue_id=issue_id, submit_pr=submit)
    result = pipe.run()
    print(json_dumps(result))
    return 0 if result["status"] == pipeline_mod.APPROVED else 1


def json_dumps(payload):
    """Compact JSON for CLI output (stdlib, testable)."""
    import json as _json
    return _json.dumps(payload, indent=2)


if __name__ == "__main__":
    sys.exit(main())

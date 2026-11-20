"""Classify changes unrelated to the terminal image."""

import os
import re
import subprocess
from pathlib import Path


def terminal_unaffected(paths: list[bytes]) -> bool:
    """Skip terminal builds only for frontend or Markdown-only docs changes."""
    return bool(paths) and all(
        (
            path.startswith(b"frontend/")
            or (path.startswith(b"docs/") and path.endswith(b".md"))
        )
        for path in paths
    )


def changed_paths(base: str, head: str) -> list[bytes]:
    """Read both sides of renames and preserve unusual filename bytes."""
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in (base, head)):
        raise ValueError("Expected full commit SHAs for the PR diff")
    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            f"{base}...{head}",
            "--",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [path for path in result.stdout.split(b"\0") if path]


def main() -> None:
    is_terminal_unaffected = False
    if os.environ["EVENT_NAME"] == "pull_request":
        is_terminal_unaffected = terminal_unaffected(
            changed_paths(os.environ["BASE_SHA"], os.environ["HEAD_SHA"])
        )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"terminal_unaffected={str(is_terminal_unaffected).lower()}\n")


if __name__ == "__main__":
    main()

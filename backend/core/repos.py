"""Repo path resolution and file listing (split from backend.core)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.core.validation import validate_repo_name


def get_repos_dir() -> Path:
    """Repos root: env override, host path, then container bind path.

    The terminal/dev container bind-mounts the host repos dir at
    /srv/repos without a /home/ari/repos alias; fall through so
    discovery works in both containers.
    """
    configured = Path(os.environ.get("BUTTONS_REPOS_DIR", "/home/ari/repos"))
    if configured.is_dir():
        return configured
    sibling = Path("/srv/repos")
    if sibling.is_dir():
        return sibling
    return configured


def resolve_repo_path(repo: str) -> Path:
    name = repo.strip().removeprefix("arii/").replace("..", "").strip("/")
    return get_repos_dir() / name


def _checkout_path(name: str) -> Path:
    """Validated checkout dir; remote-server falls back to the stack
    dir. Raises FileNotFoundError when the checkout (or its .git)
    is absent."""
    path = resolve_repo_path(name)
    if name == "remote-server" and not path.exists():
        stack = Path(os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server"))
        if stack.is_dir():
            path = stack
    git_dir = path / ".git"
    if not (git_dir.is_dir() or git_dir.is_file()):
        raise FileNotFoundError(f"unknown repo checkout: {name!r}")
    return path


def get_checkout_path(repo: str) -> Path:
    """Validated checkout dir for repo-relative reads (#653).

    Same name validation and stack fallback as the status/diff
    paths; routers use it for @-mention resolution and file
    listing. Raises ValueError on a bad name, FileNotFoundError
    when the checkout (or its .git) is absent.
    """
    return _checkout_path(validate_repo_name(repo))


# @-mention file listing (#653): cap keeps the composer dropdown
# light; skip-dirs keep caches and machine state out of it.
REPO_FILES_CAP = 200
REPO_FILES_SKIP_DIRS = frozenset(
    {"node_modules", "__pycache__", "ledger", "traces"})


def list_repo_files(repo: str, prefix: str = "",
                    limit: int = REPO_FILES_CAP) -> dict[str, Any]:
    """Repo-relative file paths for @-mention completion (#653).

    Sorted os.walk over the validated checkout, skipping dotfiles/
    dot-dirs plus REPO_FILES_SKIP_DIRS, files only. ``prefix`` (a
    leading "/" is stripped) filters by startswith; a prefix
    containing ".." matches nothing (boundary rejection). Collects
    one past ``limit`` so ``truncated`` is exact. Returns {"repo",
    "prefix", "files", "truncated"}. Same error contract as
    get_repo_status.
    """
    name = validate_repo_name(repo)
    root = _checkout_path(name)
    want = (prefix or "").lstrip("/").strip()
    if ".." in want.split("/"):
        return {"repo": name, "prefix": want, "files": [],
                "truncated": False}
    files: list[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in REPO_FILES_SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, filename), root)
            if want and not rel.startswith(want):
                continue
            if not os.path.isfile(os.path.join(dirpath, filename)):
                continue
            if len(files) > limit:
                truncated = True
                break
            files.append(rel)
        if truncated:
            break
    if len(files) > limit:
        truncated = True
        files = files[:limit]
    files.sort()
    return {"repo": name, "prefix": want, "files": files,
            "truncated": truncated}

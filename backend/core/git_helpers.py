"""Git helpers: status, diff, actions, metrics (split from backend.core)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.core.repos import _checkout_path, get_repos_dir
from backend.core.validation import (
    BRANCH_NAME_RE,
    REPO_NAME_RE,
    validate_repo_name,
)


def validate_action_branch(repo: str, branch, run=None) -> str:
    """Optional action branch (#708): "" when omitted or blank (the
    endpoint behaves exactly as today), otherwise the stripped name
    after an existence check against the repo checkout (git
    rev-parse refs/heads/<name>). Raises ValueError on a malformed
    name, an unknown repo, or a branch the checkout lacks. ``run``
    is the injectable subprocess.run seam for tests."""
    if branch is None:
        return ""
    if not isinstance(branch, str):
        raise ValueError(f"invalid branch name: {branch!r}")
    name = branch.strip()
    if not name:
        return ""
    # Detached-HEAD checkouts (CI runners) report the branch as HEAD,
    # which never exists under refs/heads — treat as the current
    # checkout, same as omitted, instead of 400ing every dispatch.
    if name == "HEAD":
        return ""
    if (not BRANCH_NAME_RE.match(name) or ".." in name
            or "//" in name or name.endswith((".lock", "/", "."))):
        raise ValueError(f"invalid branch name: {branch!r}")
    try:
        path = _checkout_path(validate_repo_name(repo))
    except FileNotFoundError:
        raise ValueError(f"unknown branch: {name!r}")
    proc = _git_run(run, ["rev-parse", "--verify", "--quiet",
                          f"refs/heads/{name}"], path)
    if proc.returncode != 0:
        raise ValueError(f"unknown branch: {name!r}")
    return name


def _parse_gh_remote(url: str | None) -> str | None:
    """owner/repo from a git remote URL, or None (port of buttons/server.py)."""
    import re

    if not url:
        return None
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    m = re.match(r"^(?:git@github\.com:|https://github\.com/|"
                 r"ssh://git@github\.com/)(.+)$", url)
    if not m or not REPO_NAME_RE.match(m.group(1).split("/")[0]) \
            or "/" not in m.group(1):
        return None
    owner, _, repo_part = m.group(1).partition("/")
    if not owner or not repo_part or "/" in repo_part \
            or not REPO_NAME_RE.match(repo_part):
        return None
    return f"{owner}/{repo_part}"


def _parse_ahead_behind(header: str) -> tuple[int, int]:
    """(ahead, behind) from a ``status --short --branch`` header.

    ``## main...origin/main [ahead 1, behind 2]``; a missing
    upstream or count reads as 0. Pure helper for list_repos (#937).
    """
    import re

    ahead = re.search(r"ahead (\d+)", header or "")
    behind = re.search(r"behind (\d+)", header or "")
    return (int(ahead.group(1)) if ahead else 0,
            int(behind.group(1)) if behind else 0)


def _git_run(run, cmd: list[str], path: Path, timeout: int = 30):
    import subprocess

    run = run or subprocess.run
    return run(
        ["git", "-c", f"safe.directory={path}", "-C", str(path)] + cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, text=True,
    )


def list_repos(repos_dir: Path | str | None = None,
               stack_dir: Path | str | None = None,
               run=None) -> list[dict[str, Any]]:
    """Every discovered checkout as {name, full, branch, clean,
    ahead, behind, status_count}.

    Port of buttons/server.py _discover_repos, minus the td/gh
    runtime fields: the special stack checkout (remote-server)
    plus every repos-dir entry with a .git. Entries that fail
    git probing are skipped, never mapped to another repo; a
    checkout without a parseable GitHub origin keeps the
    default full name ``arii/<name>``. ``ahead``/``behind`` parse
    the branch header (#937); ``status_count`` counts dirty
    entries. ``repos_dir``/``stack_dir``/``run`` are injectable
    seams for tests.
    """
    import subprocess

    run = run or subprocess.run
    root = Path(repos_dir) if repos_dir is not None else get_repos_dir()
    stack = Path(stack_dir) if stack_dir is not None else Path(
        os.environ.get("BACKEND_STACK_DIR", "/home/ari/remote-server"))

    candidates: list[tuple[str, Path]] = []
    stack_git = stack / ".git"
    if stack.is_dir() and (stack_git.is_dir() or stack_git.is_file()):
        candidates.append(("remote-server", stack))
    try:
        entries = sorted(os.listdir(root))
    except Exception:
        entries = []
    for name in entries:
        if name == "remote-server" or not REPO_NAME_RE.match(name):
            continue
        path = root / name
        git_path = path / ".git"
        if not (git_path.is_dir() or git_path.is_file()):
            continue
        candidates.append((name, path))

    repos: list[dict[str, Any]] = []
    for name, path in candidates:
        try:
            origin = run(
                ["git", "-c", f"safe.directory={path}", "-C", str(path),
                 "config", "--get", "remote.origin.url"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True,
            )
            full = _parse_gh_remote(origin.stdout.strip()) \
                if origin.returncode == 0 else None
            branch_proc = _git_run(
                run, ["rev-parse", "--abbrev-ref", "HEAD"], path)
            branch = branch_proc.stdout.strip() \
                if branch_proc.returncode == 0 else ""
            status_proc = _git_run(
                run, ["status", "--short", "--branch"], path)
            if status_proc.returncode != 0:
                continue
            header = next(
                (ln for ln in status_proc.stdout.splitlines()
                 if ln.startswith("##")), "")
            ahead, behind = _parse_ahead_behind(header)
            lines = [ln for ln in status_proc.stdout.splitlines()
                     if ln and not ln.startswith("##")]
            repos.append({"name": name, "full": full or f"arii/{name}",
                          "branch": branch, "clean": len(lines) == 0,
                          "ahead": ahead, "behind": behind,
                          "status_count": len(lines)})
        except Exception:
            continue
    return sorted(repos, key=lambda r: r["name"])


def _git_failed(proc, verb: str) -> RuntimeError:
    err = (proc.stderr or "").strip()
    detail = err.splitlines()[-1] if err else f"git {verb} failed"
    return RuntimeError(detail)


def get_repo_status(repo: str) -> dict[str, Any]:
    """Read-only git status for one repo checkout.

    Returns {"repo", "branch", "short", "clean"}. Raises
    ValueError on a bad name, FileNotFoundError when the checkout
    (or its .git) is absent."""
    import subprocess

    name = validate_repo_name(repo)
    path = _checkout_path(name)
    branch_proc = subprocess.run(
        ["git", "-c", f"safe.directory={path}", "-C", str(path),
         "rev-parse", "--abbrev-ref", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, text=True,
    )
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else ""
    status_proc = subprocess.run(
        ["git", "-c", f"safe.directory={path}", "-C", str(path),
         "status", "--short", "--branch"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, text=True,
    )
    if status_proc.returncode != 0:
        raise _git_failed(status_proc, "status")
    short = status_proc.stdout
    lines = [ln for ln in short.splitlines()
             if ln and not ln.startswith("##")]
    return {"repo": name, "branch": branch, "short": short,
            "clean": len(lines) == 0}


# Full-diff truncation cap for the diff modal (#663): the modal
# shows status + stat in full and flags a cut diff.
DIFF_CAP = 20000

# Per-file summary cap for the diff modal (#927): the file list
# is a preview, the counts stay honest via files_truncated.
DIFF_FILES_SHOWN = 200

# Raw status lines kept in the payload (#927): --short already
# collapses untracked dirs, so this only bites on pathological
# trees; clean is always computed from the full output.
STATUS_SHORT_LINES_CAP = 500


def _numstat_counts(numstat_out: str) -> dict[str, tuple[int, int]]:
    """Added/deleted line counts per path from git diff --numstat."""
    counts: dict[str, tuple[int, int]] = {}
    for line in (numstat_out or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            counts[parts[2]] = (int(parts[0]), int(parts[1]))
        except ValueError:
            continue  # Binary ("-\t-\tpath") or garbage.
    return counts


def _parse_diff_files(status_out: str,
                      counts: dict[str, tuple[int, int]]
                      ) -> list[dict[str, Any]]:
    """Per-file summaries from status --short + numstat counts."""
    files: list[dict[str, Any]] = []
    for line in (status_out or "").splitlines():
        if len(line) < 4 or line[2] != " ":
            continue
        x, y, rest = line[0], line[1], line[3:]
        if not rest:
            continue
        count = counts.get(rest, (0, 0))
        if count == (0, 0) and " -> " in rest:
            count = counts.get(rest.split(" -> ", 1)[1], (0, 0))
        code = f"{x}{y}".strip()
        files.append({"path": rest, "status": code or "M",
                      "staged": x not in " ?!",
                      "unstaged": y not in " ?!",
                      "additions": count[0], "deletions": count[1]})
    return files


def get_repo_diff(repo: str, run=None) -> dict[str, Any]:
    """Read-only status + diff for one repo checkout (#663, #927).

    Returns {"repo", "branch", "clean", "status_short",
    "status_truncated", "diff_stat", "diff", "truncated", "files",
    "files_truncated}. The diff covers staged AND unstaged work
    (diff HEAD); files carries per-file summaries parsed from
    status --short plus numstat counts. Same error contract as
    get_repo_status. ``run`` injects tests.
    """
    import subprocess

    run = run or subprocess.run
    name = validate_repo_name(repo)
    path = _checkout_path(name)

    def git(*args):
        return run(
            ["git", "-c", f"safe.directory={path}", "-C", str(path),
             *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True,
        )

    def diff_head(*args):
        """diff HEAD with an unborn-HEAD fallback to plain diff."""
        proc = git("diff", "HEAD", *args)
        if proc.returncode == 0:
            return proc
        head = git("rev-parse", "--verify", "--quiet", "HEAD")
        if head.returncode == 0:
            raise _git_failed(proc, "diff HEAD")
        plain = git("diff", *args)
        if plain.returncode != 0:
            raise _git_failed(proc, "diff HEAD")
        return plain

    branch_proc = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else ""
    status_proc = git("status", "--short")
    if status_proc.returncode != 0:
        raise _git_failed(status_proc, "status")
    stat_proc = diff_head("--stat")
    diff_proc = diff_head()
    numstat_proc = diff_head("--numstat")
    short = status_proc.stdout or ""
    lines = [ln for ln in short.splitlines() if ln]
    status_lines = short.splitlines()
    status_truncated = len(status_lines) > STATUS_SHORT_LINES_CAP
    if status_truncated:
        short = "\n".join(status_lines[:STATUS_SHORT_LINES_CAP]) + "\n"
    diff = diff_proc.stdout or ""
    truncated = len(diff) > DIFF_CAP
    files = _parse_diff_files(status_proc.stdout,
                              _numstat_counts(numstat_proc.stdout))
    files_truncated = len(files) > DIFF_FILES_SHOWN
    return {"repo": name, "branch": branch, "clean": len(lines) == 0,
            "status_short": short, "status_truncated": status_truncated,
            "diff_stat": stat_proc.stdout, "diff": diff[:DIFF_CAP],
            "truncated": truncated, "files": files[:DIFF_FILES_SHOWN],
            "files_truncated": files_truncated}


# One-click git actions for the diff modal (#927): longer timeout
# for fetch/rebase/stash/clean, fixed-vocabulary details (raw git
# output is never quoted: it trips the History/autofile failure
# regexes and can echo remote URLs).
REPO_ACTION_TIMEOUT = 120
CONSOLE_STASH_PREFIX = "console diff-modal"


def _action_result(action: str, repo: str, ok: bool, clean: bool,
                   detail: str) -> dict[str, Any]:
    return {"ok": ok, "repo": repo, "action": action, "clean": clean,
            "detail": detail}


def _action_status_lines(run, path: Path) -> list[str] | None:
    """Current status lines, or None when git itself fails."""
    import subprocess

    try:
        proc = _git_run(run, ["status", "--short"], path)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return [ln for ln in (proc.stdout or "").splitlines() if ln]


def repo_stash(repo: str, run=None) -> dict[str, Any]:
    """Stash all local work incl. untracked files (#927).

    Returns {"ok", "repo", "action", "clean", "detail"}. Raises
    ValueError on a bad name, FileNotFoundError when the checkout
    is absent. ``run`` injects tests.
    """
    import subprocess
    import time

    name = validate_repo_name(repo)
    path = _checkout_path(name)
    try:
        pre = _action_status_lines(run, path)
        if pre is not None and not pre:
            return _action_result("stash", name, True, True,
                                   "already clean - nothing to stash")
        proc = _git_run(
            run, ["stash", "push", "-u", "-m",
                  f"{CONSOLE_STASH_PREFIX} {int(time.time())}"],
            path, timeout=REPO_ACTION_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return _action_result("stash", name, False, False,
                               "timed out stashing; retry")
    if proc.returncode != 0:
        out = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
        if "no local changes" in out:
            return _action_result("stash", name, True, True,
                                   "already clean - nothing to stash")
        return _action_result("stash", name, False, False,
                               "stash failed; stash or commit by hand, "
                               "then retry")
    post = _action_status_lines(run, path)
    return _action_result("stash", name, True,
                           post is not None and not post,
                           "changes stashed")


def repo_discard_untracked(repo: str, run=None) -> dict[str, Any]:
    """Remove untracked files/dirs, never ignored ones (#927).

    git clean -fd without -x: build output and env files stay.
    Same return/error contract as repo_stash.
    """
    import subprocess

    name = validate_repo_name(repo)
    path = _checkout_path(name)
    try:
        pre = _action_status_lines(run, path)
        untracked = [ln for ln in (pre or [])
                     if ln.startswith("??")] if pre is not None else None
        if untracked is not None and not untracked:
            return _action_result(
                "discard-untracked", name, True, not pre,
                "no untracked files to discard")
        proc = _git_run(run, ["clean", "-fd"], path,
                        timeout=REPO_ACTION_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return _action_result("discard-untracked", name, False, False,
                               "timed out discarding; retry")
    if proc.returncode != 0:
        return _action_result(
            "discard-untracked", name, False, False,
            "discard failed; remove untracked files by hand, then retry")
    removed = len([ln for ln in (proc.stdout or "").splitlines()
                   if ln.startswith("Removing ")])
    post = _action_status_lines(run, path)
    detail = f"discarded {removed} untracked path(s)" if removed \
        else "untracked files discarded"
    return _action_result("discard-untracked", name, True,
                           post is not None and not post, detail)


def repo_sync(repo: str, run=None) -> dict[str, Any]:
    """Fetch origin and rebase onto origin/main (#927).

    Refuses dirty trees with a plain message; a failed rebase is
    always aborted so the tree is left unchanged. Same
    return/error contract as repo_stash.
    """
    import subprocess

    name = validate_repo_name(repo)
    path = _checkout_path(name)
    try:
        pre = _action_status_lines(run, path)
        if pre:
            return _action_result(
                "sync", name, False, False,
                "tree has local changes; stash or commit before syncing")
        fetch = _git_run(run, ["fetch", "origin"], path,
                         timeout=REPO_ACTION_TIMEOUT)
        if fetch.returncode != 0:
            return _action_result("sync", name, False, pre is not None
                                   and not pre,
                                   "fetch failed; check network, then retry")
        verify = _git_run(run, ["rev-parse", "--verify", "--quiet",
                                "origin/main"], path)
        if verify.returncode != 0:
            return _action_result("sync", name, False, pre is not None
                                   and not pre,
                                   "origin/main not found; sync by hand")
        rebase = _git_run(run, ["rebase", "origin/main"], path,
                          timeout=REPO_ACTION_TIMEOUT)
        if rebase.returncode == 0:
            post = _action_status_lines(run, path)
            out = f"{rebase.stdout or ''}\n{rebase.stderr or ''}"
            detail = "already up to date with origin/main" \
                if "up to date" in out else "rebased onto origin/main"
            return _action_result("sync", name, True,
                                   post is not None and not post, detail)
        out = f"{rebase.stdout or ''}\n{rebase.stderr or ''}".lower()
        try:
            _git_run(run, ["rebase", "--abort"], path,
                     timeout=REPO_ACTION_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            pass
        post = _action_status_lines(run, path)
        detail = "rebase conflicted; aborted, tree unchanged - " \
            "resolve by hand" if "conflict" in out \
            else "rebase failed; aborted, tree unchanged - sync by hand"
        return _action_result("sync", name, False,
                               post is not None and not post, detail)
    except (subprocess.TimeoutExpired, OSError):
        return _action_result("sync", name, False, False,
                               "timed out syncing; retry")


# Top marker hits returned per metrics read; the count itself is
# complete (the strip shows "N TODOs", hits are a preview).
TODO_HITS_SHOWN = 8


def get_repo_metrics(repo: str, run=None) -> dict[str, Any]:
    """Read-only worklist metrics for one repo checkout (#897).

    Returns {"repo", "branch", "clean", "status_count",
    "todo_count", "todo_hits", "diff_stat", "diff_files"}. Same
    error contract as get_repo_status. ``run`` injects tests. All
    three reads are local and unmetered, so the console prefetches
    on every repo select with zero model turns.
    """
    import subprocess

    run = run or subprocess.run
    name = validate_repo_name(repo)
    path = _checkout_path(name)

    def git(*args):
        return run(
            ["git", "-c", f"safe.directory={path}", "-C", str(path),
             *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True,
        )

    branch_proc = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_proc.stdout.strip() \
        if branch_proc.returncode == 0 else ""
    status_proc = git("status", "--short")
    if status_proc.returncode != 0:
        raise _git_failed(status_proc, "status")
    stat_proc = git("diff", "--stat")
    if stat_proc.returncode != 0:
        raise _git_failed(stat_proc, "diff --stat")
    short = status_proc.stdout
    stat = stat_proc.stdout
    diff_files = len([ln for ln in stat.splitlines() if "|" in ln])

    # Tracked files only: git grep is gitignore-aware, so vendored
    # build output (.pnpm-store, dist) never pollutes the count.
    todo_proc = git("grep", "-n", "-i", "-E",
                    r"\b(TODO|FIXME|XXX|HACK)\b", "--", ".")
    if todo_proc.returncode not in (0, 1):
        raise RuntimeError(
            f"todo grep failed: "
            f"{(todo_proc.stderr or '').strip()[:200]}")
    hits = []
    count = 0
    for line in (todo_proc.stdout or "").splitlines():
        if not line.strip():
            continue
        count += 1
        if len(hits) >= TODO_HITS_SHOWN:
            continue
        file_part, _, rest = line.partition(":")
        lineno, _, text = rest.partition(":")
        try:
            lineno = int(lineno.strip())
        except ValueError:
            continue
        hits.append({"file": file_part.strip(), "line": lineno,
                     "text": text.strip()[:120]})
    return {"repo": name, "branch": branch,
            "clean": len([ln for ln in short.splitlines()
                          if ln]) == 0,
            "status_count": len([ln for ln in short.splitlines()
                                 if ln]),
            "todo_count": count, "todo_hits": hits,
            "diff_stat": stat, "diff_files": diff_files}

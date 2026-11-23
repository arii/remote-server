"""Shared deterministic helpers for S5 agent slices (deep-agent/).

Stdlib only: system-python tests import this without the venv. Wraps the
subprocess discipline every slice needs — always a timeout, never a shell,
validation before execution, capped outputs — plus repo sandboxing so tools
cannot escape their checkout.

New slices: import these instead of re-implementing gh/td/file access.
Do NOT import langchain here (keeps system-python tests working).
Do NOT put secrets in here; the model router (`model_router.py`, same package) and the
ledger (`ledger.py`) own their own concerns.
"""

import collections
import html
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CHECKOUTS = {"arii/remote-server": "/srv/remote-server"}

DEFAULT_TIMEOUT = 120
MAX_OUT = 4000

# Verify-command allowlist, shared by coder's run_check and the
# chat/ask run_repo_command tool (#633). Moved verbatim from coder.py:
# argv prefixes (pinned cwd, capped output).
# NOTE: no ("npx", "tsc") — it fetches the `tsc` squat placeholder,
# not TypeScript, so it stays rejected (see #517 for the twin fix).
CHECK_PREFIXES = (
    ("python3", "-m", "pytest"),
    ("pytest",),
    ("pnpm", "exec", "vitest"),
    ("pnpm", "test"),
    ("pnpm", "run", "lint"),
    ("pnpm", "run", "type-check"),
    ("npm", "test"),
    ("go", "test"),
    ("git", "diff"),
    ("git", "status"),
    ("git", "log"),
)
# Per-repo verify profiles (#1377). Extra prefixes apply only to the named
# repo; every other repo, and an unknown or missing repo, keeps CHECK_PREFIXES
# unchanged (fail closed). Extra prefixes go through the same argument
# grammar as the defaults.
FRONTEND_VITEST_PREFIX = ("pnpm", "--filter", "frontend", "exec", "vitest", "run")
REPO_CHECK_PREFIXES = {
    "arii/remote-server": (FRONTEND_VITEST_PREFIX,),
    "arii/portfolio": (
        ("pnpm", "run", "test"),
        ("pnpm", "run", "build"),
        ("pnpm", "run", "audit:fallow"),
    ),
}
CHECK_TIMEOUT = 300
CHECK_OUTPUT_LIMIT = 4000
TEST_FILE_RE = re.compile(r"^(?:backend/)?tests/test_[A-Za-z0-9_]+\.py$")
UNITTEST_MODULE_RE = re.compile(
    r"(?:backend\.)?tests\.test_[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*"
)


def check_prefixes(repo=None):
    """Allowed command prefixes for repo (defaults when repo is unknown)."""
    if isinstance(repo, str):
        return CHECK_PREFIXES + REPO_CHECK_PREFIXES.get(repo, ())
    return CHECK_PREFIXES


def check_allowed(argv, repo=None):
    """True when argv starts with an allowlisted verify prefix, or
    runs one repo test file directly (python3 tests/test_x.py).
    repo adds that repo's profile prefixes (REPO_CHECK_PREFIXES)."""
    tupled = tuple(argv)
    unittest_argv = tupled[1:] if tupled[:1] == ("PYTHONPATH=./deep-agent",) else tupled
    if unittest_argv[:3] == ("python3", "-m", "unittest"):
        return len(unittest_argv) > 3 and all(
            not module.endswith(".py") and UNITTEST_MODULE_RE.fullmatch(module)
            for module in unittest_argv[3:]
        )
    if unittest_argv != tupled:
        return False
    if len(tupled) >= 2 and tupled[0] == "python3" and TEST_FILE_RE.match(tupled[1]):
        return True
    # 1230: allow only the exact frozen-lockfile install (optional --dir),
    # never bare `pnpm install` or other pnpm subcommands.
    if tupled[:2] == ("pnpm", "install"):
        rest = list(tupled[2:])
        if "--dir" in rest:
            i = rest.index("--dir")
            if len(rest) < i + 2:
                return False
            del rest[i : i + 2]
        return rest == ["--frozen-lockfile"]
    for prefix in check_prefixes(repo):
        if tupled[: len(prefix)] == prefix:
            return _check_args_safe(prefix, tupled[len(prefix) :])
    return False


# Argument grammar for the prefix allowlist. A prefix alone let a model
# (or injected text it read) append options such as `git diff --no-index
# -- /dev/null /etc/passwd` (read any file), `git log --output=PATH` or
# `pytest --basetemp=PATH` (write or wipe any path), or `go test -exec
# CMD` (run any program). Options are matched EXACTLY (git and pytest
# accept abbreviated long options, so prefix matching cannot be trusted)
# and every positional argument must stay inside the checkout.
_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9_ ()\-.,]+")
_SAFE_FORMAT_RE = re.compile(r"[A-Za-z0-9 %:_.,\-\[\]()/]+")
_SAFE_PATH_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./:@+=,\-\[\]*]*")
_DIGITS_RE = re.compile(r"[0-9]{1,6}")

_GIT_DIFF_FLAGS = frozenset(
    (
        "--stat", "--name-only", "--name-status", "--shortstat", "--numstat",
        "--summary", "--cached", "--staged", "--no-color", "--color=never",
        "--check", "--exit-code", "--quiet", "-p", "--patch", "--no-patch",
        "-w", "-b", "--ignore-all-space", "--ignore-space-change",
        "--no-renames", "--raw", "--compact-summary", "--minimal",
        "--histogram", "--patience",
    )
)  # fmt: skip
_GIT_LOG_FLAGS = frozenset(
    (
        "--oneline", "--graph", "--decorate", "--no-decorate", "--stat",
        "--name-only", "--name-status", "--shortstat", "--numstat", "-p",
        "--patch", "--no-color", "--color=never", "--merges", "--no-merges",
        "--first-parent", "--reverse", "--abbrev-commit", "--summary",
        "--follow",
    )
)  # fmt: skip
_GIT_STATUS_FLAGS = frozenset(
    (
        "-s", "--short", "-b", "--branch", "--porcelain", "--porcelain=v1",
        "--porcelain=v2", "--long", "-uno", "--untracked-files=no",
        "--untracked-files=normal", "--untracked-files=all",
    )
)  # fmt: skip
_PYTEST_FLAGS = frozenset(
    (
        "-q", "-qq", "-v", "-vv", "-vvv", "-x", "-s", "-l", "--lf", "--ff",
        "--nf", "--co", "--collect-only", "--no-header", "--no-summary",
        "-ra", "-rA", "-rf", "-rE", "-rs", "-rx", "-rX", "-rfE",
        "--strict-markers", "--disable-warnings",
    )
)  # fmt: skip
_PYTEST_ENUMS = ("--tb=",)
_PYTEST_TB = frozenset(("auto", "long", "short", "line", "native", "no"))
_VITEST_FLAGS = frozenset(
    (
        "run", "--run", "--reporter=default", "--reporter=dot",
        "--reporter=verbose", "--passWithNoTests",
    )
)  # fmt: skip
_GO_FLAGS = frozenset(("-v", "-short", "-race", "-failfast"))


def _safe_positional(token):
    """A relative path, revision or test id that cannot leave the checkout.

    Rejects absolute paths, a leading '-' or ':', '~', and any '..' path
    component. Revision ranges (a..b, a...b) keep their dots because the
    dots sit inside a component, not as a component.
    """
    if not isinstance(token, str) or not token or "\x00" in token:
        return False
    if token[0] in "/-:~" or "\\" in token:
        return False
    if any(part == ".." for part in token.split("/")):
        return False
    return bool(_SAFE_PATH_RE.fullmatch(token))


def _args_with_flags(args, flags, value_flags=None, prefix_values=None):
    """Walk args: exact flags, `flag value` pairs, `--opt=value` forms and
    positionals. Everything unknown is rejected."""
    value_flags = value_flags or {}
    prefix_values = prefix_values or {}
    i = 0
    seen_dashdash = False
    while i < len(args):
        a = args[i]
        if seen_dashdash:
            if not _safe_positional(a):
                return False
        elif a == "--":
            seen_dashdash = True
        elif a in flags:
            pass
        elif a in value_flags:
            i += 1
            if i >= len(args) or not value_flags[a](args[i]):
                return False
        elif a.startswith("-"):
            for lead, check in prefix_values.items():
                if a.startswith(lead) and check(a[len(lead) :]):
                    break
            else:
                return False
        elif not _safe_positional(a):
            return False
        i += 1
    return True


def _digits(value):
    return bool(_DIGITS_RE.fullmatch(value))


def _git_args_safe(sub, args):
    if sub == "diff":
        return _args_with_flags(
            args,
            _GIT_DIFF_FLAGS,
            prefix_values={"-U": _digits, "--unified=": _digits},
        )
    if sub == "log":
        return _args_with_flags(
            args,
            _GIT_LOG_FLAGS,
            value_flags={"-n": _digits},
            prefix_values={
                "-n": _digits,
                "--max-count=": _digits,
                "-": _digits,
                "--since=": lambda v: bool(_SAFE_VALUE_RE.fullmatch(v)),
                "--until=": lambda v: bool(_SAFE_VALUE_RE.fullmatch(v)),
                "--author=": lambda v: bool(_SAFE_VALUE_RE.fullmatch(v)),
                "--grep=": lambda v: bool(_SAFE_VALUE_RE.fullmatch(v)),
                "--pretty=": lambda v: bool(_SAFE_FORMAT_RE.fullmatch(v)),
                "--format=": lambda v: bool(_SAFE_FORMAT_RE.fullmatch(v)),
            },
        )
    if sub == "status":
        return _args_with_flags(args, _GIT_STATUS_FLAGS)
    return False


def _pytest_args_safe(args):
    def value(v):
        return bool(_SAFE_VALUE_RE.fullmatch(v)) and not v.startswith("-")

    return _args_with_flags(
        args,
        _PYTEST_FLAGS,
        value_flags={"-k": value, "-m": value, "-p": lambda v: v == "no:cacheprovider"},
        prefix_values={
            "--tb=": lambda v: v in _PYTEST_TB,
            "--maxfail=": _digits,
            "--durations=": _digits,
        },
    )


def _check_args_safe(prefix, args):
    if prefix[0] == "git":
        return _git_args_safe(prefix[1], list(args))
    if prefix in (("pytest",), ("python3", "-m", "pytest")):
        return _pytest_args_safe(list(args))
    if prefix == ("pnpm", "exec", "vitest"):
        return _args_with_flags(
            list(args),
            _VITEST_FLAGS,
            value_flags={"-t": lambda v: bool(_SAFE_VALUE_RE.fullmatch(v))},
        )
    if prefix == FRONTEND_VITEST_PREFIX:
        return (
            len(args) == 1
            and _safe_positional(args[0])
            and bool(
                re.fullmatch(
                    r"(?:[\w.-]+/)*[\w.-]+\.(?:test|spec)\.[cm]?[jt]sx?", args[0]
                )
            )
        )
    if prefix == ("go", "test"):
        return _args_with_flags(
            list(args),
            _GO_FLAGS,
            value_flags={"-run": lambda v: bool(_SAFE_VALUE_RE.fullmatch(v))},
            prefix_values={"-count=": _digits},
        )
    # pnpm test, pnpm run lint|type-check, npm test: run checkout-defined
    # scripts; no extra options, only checkout-relative positionals.
    return _args_with_flags(list(args), frozenset())


def allowed_commands_text(repo=None):
    """Human list of the allowlisted command heads (tool prompts)."""
    return ", ".join(" ".join(p) for p in check_prefixes(repo)) + (
        ", pnpm install --frozen-lockfile"
        ", python3 -m unittest [backend.]tests.test_<module>"
        ", PYTHONPATH=./deep-agent python3 -m unittest [backend.]tests.test_<module>"
    )


def verification_guidance(repo=None):
    heads = ", ".join(" ".join(p) for p in check_prefixes(repo))
    extra = ""
    if repo == "arii/portfolio":
        extra = (
            f"This repo ({repo}) is not Python-first: use its own pnpm scripts "
            "from the allowed heads instead of pytest/unittest. "
        )
    if repo == "arii/remote-server":
        extra = (
            f"For a frontend test use {' '.join(FRONTEND_VITEST_PREFIX)} "
            "<path relative to frontend>, not root pnpm exec vitest. "
        )
    return (
        "Verification policy for run_check (overrides commands suggested in the issue):\n"
        f"Allowed command heads: {heads}; python3 [backend/]tests/test_NAME.py; "
        "python3 -m unittest [backend.]tests.test_NAME[.<TestCase>[.<test_method>]] "
        "(optionally prefixed with PYTHONPATH=./deep-agent).\n"
        "Replace NAME with an existing test module; read the repository's tests first. "
        "For Python changes prefer targeted python3 -m unittest "
        "[backend.]tests.test_NAME or python3 -m pytest with existing test paths. "
        "Run one command per call, without shell operators. "
        f"{extra}"
        "Do not use npm run test:backend, arbitrary npm scripts, or dependency installs. "
        "Git inspection alone does not verify an implementation."
    )


def parse_check_command(command, repo=None):
    argv = shlex.split(command)
    if not argv or not check_allowed(argv, repo):
        raise ValueError(
            "command must start with one of: " + allowed_commands_text(repo)
        )
    return argv[1:] if argv[0] == "PYTHONPATH=./deep-agent" else argv


def check_env(checkout):
    """Child env for verification commands run in checkout.

    The checkout root leads PYTHONPATH (existing entries kept
    behind it) so pytest collection imports the repo's own
    packages — without this an agent burns whole runs retrying a
    collection error it cannot fix, because the allowlist forbids
    env-prefix commands (#1160 gap 1). The deep-agent/ sibling dir
    follows the root so cross-component imports (s5tools, runs)
    resolve exactly as under the npm test scripts, which export
    PYTHONPATH=./deep-agent. Harmless for non-python
    commands (node/go/git ignore PYTHONPATH). Never raises.
    """
    try:
        base = os.environ.get("PYTHONPATH", "")
        path = (
            checkout
            + os.pathsep
            + os.path.join(checkout, "deep-agent")
            + (os.pathsep + base if base else "")
        )
        return {"PYTHONPATH": path}
    except Exception:
        return {"PYTHONPATH": checkout}


@contextmanager
def verification_env(checkout):
    with tempfile.TemporaryDirectory(prefix="agent-verification-") as state:
        env = {
            **os.environ,
            **check_env(checkout),
            "DEEP_AGENT_STATE_DIR": state,
            "EVAL_LEDGER": os.path.join(state, "ledger.jsonl"),
            "BUTTONS_JOBS_RUNS_PATH": os.path.join(state, "jobs_runs.json"),
            "BUTTONS_JOBS_SCHEDULE_PATH": os.path.join(state, "jobs_schedules.json"),
        }
        cur_bin = os.path.dirname(sys.executable)
        path = env.get("PATH", "")
        if cur_bin and not path.startswith(cur_bin + os.pathsep) and path != cur_bin:
            env["PATH"] = cur_bin + os.pathsep + path
        env.pop("GIT_EXTERNAL_DIFF", None)
        yield env


def exec_argv(argv):
    """argv to actually run. Git read-only commands are launched with the
    checkout's helper hooks disabled: a checkout's own .git/config or
    .gitattributes can name diff.external, a textconv driver, or
    core.fsmonitor, each of which makes `git diff|log|status` execute a
    program. Verification commands (pytest, go, npm) run checkout code by
    design; the git inspect commands must stay read-only."""
    argv = list(argv)
    if argv[:1] != ["git"] or len(argv) < 2:
        return argv
    head = [
        "git",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=",
    ]
    extra = ["--no-ext-diff", "--no-textconv"] if argv[1] in ("diff", "log") else []
    return head + [argv[1]] + extra + argv[2:]


def run_repo_command(checkout, command, repo=None):
    """Run an allowlisted verification command in checkout. Never
    raises: returns "exit=<n>\\n<tail>", "REJECTED: ...", "TIMEOUT
    ...", or "exec failed: ...". argv is tupled from shlex — never
    shell=True — so metachars are inert arg bytes, never control."""
    try:
        argv = parse_check_command(command, repo)
    except ValueError as exc:
        return f"REJECTED: bad command line: {exc}"
    try:
        with verification_env(checkout) as env:
            proc = subprocess.run(
                exec_argv(argv),
                cwd=checkout,
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT,
                env=env,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {CHECK_TIMEOUT}s: {command[:200]}"
    except OSError as exc:
        return f"exec failed: {exc}"
    out = ((proc.stdout or "") + (proc.stderr or ""))[-CHECK_OUTPUT_LIMIT:]
    return f"exit={proc.returncode}\n{out}"


def valid_repo(repo):
    """True for owner/name strings safe to interpolate into commands."""
    return isinstance(repo, str) and bool(REPO_RE.match(repo))


def valid_number(value):
    """Positive int from str digits or int, else None. Rejects bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def checkout_for(repo):
    """Checkout path for a repo; raises ValueError on bad input."""
    if not valid_repo(repo):
        raise ValueError(f"bad repo: {repo!r}")
    if repo in CHECKOUTS:
        return CHECKOUTS[repo]
    return f"/srv/repos/{repo.split('/', 1)[1]}"


def repo_for_checkout(path):
    """Owner/name for a checkout path, or '' when unknown.

    Inverse of checkout_for for agent tools that need the repo but
    only know the checkout (bare names gain the arii/ owner prefix,
    mirroring retrieval.normalize_repo).
    """
    if not isinstance(path, str) or not path:
        return ""
    for repo, root in CHECKOUTS.items():
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return repo
    parts = path.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "repos" and REPO_RE.match("arii/" + parts[-1]):
        return "arii/" + parts[-1]
    return ""


PR_JSON_FIELDS = (
    "number,title,author,state,isDraft,url,reviewDecision,statusCheckRollup"
)

DIFF_CAP = 6000


def _validated_pr_target(number, repo):
    """Shared number/repo validation for PR reads. Returns (num, text)
    or an error string."""
    num = valid_number(number)
    if num is None:
        return "PR_ERROR: bad PR number"
    text = repo.strip() if isinstance(repo, str) else ""
    if text and "/" not in text:
        text = "arii/" + text
    if not valid_repo(text):
        return "PR_ERROR: bad repo (want owner/name)"
    return num, text


def read_pr(number, repo, timeout=60, diff=False):
    """Read-only GitHub PR summary via gh. Never raises.

    Private repos are invisible to keyless search_web, and td-based
    PR reads need a project_config.json the checkout may not carry,
    so chat agents read PRs through gh directly (auth comes from the
    environment, same as lane tooling). Failures return
    "PR_ERROR: <one-line reason>". diff=True appends the capped
    patch (review agents otherwise cannot see what changed).
    """
    target = _validated_pr_target(number, repo)
    if isinstance(target, str):
        return target
    num, text = target
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(num), "--repo", text, "--json", PR_JSON_FIELDS],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"PR_ERROR: gh failed: {exc}"[:MAX_OUT]
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"PR_ERROR: {(err[0] if err else 'gh error')[:200]}"
    try:
        snap = json.loads(proc.stdout or "")
    except (ValueError, TypeError):
        return "PR_ERROR: gh returned non-JSON"
    if not isinstance(snap, dict):
        return "PR_ERROR: gh returned unexpected payload"
    author = snap.get("author") or {}
    checks = snap.get("statusCheckRollup") or []
    states = collections.Counter(
        str(c.get("conclusion") or c.get("status") or "?").upper()
        for c in checks
        if isinstance(c, dict)
    )
    check_line = ", ".join(f"{k}:{v}" for k, v in sorted(states.items())) or "no checks"
    line = (
        f"PR #{snap.get('number')} [{snap.get('state')}"
        f"{' DRAFT' if snap.get('isDraft') else ''}] "
        f"{snap.get('title')} by {(author.get('login') if isinstance(author, dict) else author) or '?'} "
        f"review={snap.get('reviewDecision') or 'none'} "
        f"checks=({check_line}) {snap.get('url') or ''}"
    )[:MAX_OUT]
    if not diff:
        return line
    return line + read_pr_diff(num, text, timeout=timeout)


def read_pr_diff(num, repo, timeout=60, limit=DIFF_CAP):
    """Capped unified diff for a validated PR target. Returns the
    patch block or a one-line unavailable note; never raises."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "diff", str(num), "--repo", repo],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"\nDIFF: unavailable ({exc})"
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return "\nDIFF: unavailable (empty)"
    patch = (proc.stdout or "").strip()
    if len(patch) > limit:
        patch = patch[:limit] + "\n... [truncated]"
    return "\nDIFF:\n" + patch


def run(argv, cwd=None, timeout=DEFAULT_TIMEOUT):
    """Run argv (no shell). Returns stripped stdout; raises RuntimeError
    with capped stderr on failure or timeout."""
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) for a in argv)
    ):
        raise ValueError("argv must be a non-empty list of str")
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout}s: {' '.join(argv[:3])}")
    except OSError as exc:
        raise RuntimeError(f"exec failed: {exc}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:200]
        raise RuntimeError(f"rc={proc.returncode}: {' '.join(argv[:3])}: {err}")
    return (proc.stdout or "").strip()


def td(args, cwd, timeout=300):
    """td CLI in a repo checkout. Returns stdout text (parse JSON yourself
    when the command supports --json)."""
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        raise ValueError(f"bad cwd: {cwd!r}")
    return run(["td", *args], cwd=cwd, timeout=timeout)


def _checkout_root(checkout):
    """Real checkout root, or None when the checkout is missing.

    The sandbox must not trust callers: a falsy/non-str checkout
    rejects instead of collapsing the prefix guard (empty string)
    or crashing it (None).
    """
    if not isinstance(checkout, str) or not checkout:
        return None
    return os.path.realpath(checkout)


def _inside(root, full):
    return full == root or full.startswith(root + os.sep)


def active_checkout_path(checkout, path):
    """Resolve a path inside the checkout, not one of its nested worktrees."""
    base = os.path.abspath(checkout)
    root = os.path.realpath(checkout)
    joined = os.path.join(checkout, path)
    candidate = os.path.abspath(joined)
    full = os.path.realpath(joined)
    if not _inside(base, candidate) or not _inside(root, full):
        raise ValueError("path escapes the checkout")
    for parent, target in ((base, candidate), (root, full)):
        if ".worktrees" in os.path.relpath(target, parent).split(os.sep):
            raise ValueError("path is in a nested worktree")
    return full


def list_dir(checkout, path="", limit=200):
    """List names in a checkout directory. Returns newline-separated
    names or an error string (routine outcomes, not exceptions)."""
    root = _checkout_root(checkout)
    if root is None:
        return "REJECTED: bad checkout"
    full = os.path.realpath(os.path.join(root, path or ""))
    if not _inside(root, full):
        return "REJECTED: path escapes the checkout"
    try:
        names = sorted(os.listdir(full))
    except OSError as exc:
        return f"list failed: {exc}"
    return "\n".join(names[:limit]) or "(empty)"


def read_repo_file(checkout, path, limit=3000, offset=0):
    """Read a repo-relative file from char offset, up to limit chars.

    Negatives clamp to 0; missing/REJECTED shapes stay routine,
    not exceptions."""
    root = _checkout_root(checkout)
    if root is None:
        return "REJECTED: bad checkout"
    try:
        full = active_checkout_path(checkout, path or "")
    except ValueError as exc:
        return f"REJECTED: {exc}"
    if not os.path.isfile(full):
        return "not found: no such file"
    try:
        start = max(0, int(offset))
    except (TypeError, ValueError):
        return "REJECTED: bad offset"
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            f.seek(start)
            return f.read(limit)
    except OSError as exc:
        return f"read failed: {exc}"


def grep_repo(checkout, pattern, limit=2000):
    """Literal grep over a checkout (skips .git/node_modules/.venv and
    generated/agent dirs). Returns matches or a no-matches/REJECTED
    string."""
    if not pattern or len(pattern) > 120:
        return "REJECTED: bad pattern"
    try:
        proc = subprocess.run(
            [
                "grep",
                "-rn",
                "-m",
                "20",
                "--exclude-dir=.git",
                "--exclude-dir=.worktrees",
                "--exclude-dir=node_modules",
                "--exclude-dir=.pnpm-store",
                "--exclude-dir=.pnpm",
                "--exclude-dir=.venv",
                "--exclude-dir=__pycache__",
                "--exclude-dir=.boomtick",
                "--exclude-dir=chroma_db",
                "--exclude-dir=.claude",
                "--exclude-dir=.muse",
                "--exclude=ledger.jsonl",
                "--",
                pattern,
                ".",
            ],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"grep failed: {exc}"
    out = (proc.stdout or "").strip()
    return out[:limit] or "no matches"


FETCH_DEFAULT_TIMEOUT = 20
FETCH_DEFAULT_MAX_BYTES = 262144
FETCH_MAX_REDIRECTS = 3
FETCH_DRAIN_BOUND = 1024 * 1024
FETCH_CHUNK = 65536


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse automatic redirect-following: each hop is re-validated
    by fetch_url instead (urllib would follow blindly)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _host_blocked(host, allow_loopback=False):
    """Error text when host must not be fetched, else None.

    Every resolved IP must be globally routable: loopback,
    link-local (169.254/16 metadata), private (10/8...), multicast,
    reserved, and unspecified ranges all fail closed, as do DNS
    failures. allow_loopback is a test seam for local HTTP doubles
    (loopback only — every other range still blocks).
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return f"DNS failed for {host}: {exc}"
    except Exception as exc:
        return f"cannot resolve {host}: {exc}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return f"unparseable address for {host}"
        if allow_loopback and ip.is_loopback:
            continue
        # Union, not is_global alone: stdlib marks multicast
        # (224.0.0.0/4) global, and only is_global catches 100.64/10.
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or not ip.is_global
        ):
            return f"blocked non-public address {ip} for {host}"
    return None


def _one_line(exc, limit=200):
    return " ".join(str(exc).split())[:limit]


def fetch_url(
    url,
    timeout=FETCH_DEFAULT_TIMEOUT,
    max_bytes=FETCH_DEFAULT_MAX_BYTES,
    _allow_loopback=False,
):
    """Fetch a URL's text. Never raises: failures return
    "FETCH_ERROR: <one-line reason>".

    SSRF guard: http(s) only, every hop's resolved IPs must be
    globally routable, at most FETCH_MAX_REDIRECTS redirects (each
    re-validated), per-request timeout. Bodies decode as UTF-8 with
    errors replaced and truncate past max_bytes with a
    "[truncated N bytes]" marker (N+ when more than the drain bound
    remains). Known limit: resolve-then-connect is not atomic, so a
    rebinding hostname could swap answers between check and connect
    (TOCTOU hardening is out of scope).
    """
    if not isinstance(url, str) or not url.strip():
        return "FETCH_ERROR: empty url"
    max_bytes = max(1, int(max_bytes or 0))
    current = url.strip()
    opener = urllib.request.build_opener(_NoAutoRedirect)
    opener.addheaders = [("User-Agent", "S5-agent-fetch")]
    for _ in range(FETCH_MAX_REDIRECTS + 1):
        try:
            parts = urllib.parse.urlsplit(current)
        except Exception as exc:
            return f"FETCH_ERROR: bad url: {_one_line(exc)}"
        if parts.scheme not in ("http", "https"):
            return f"FETCH_ERROR: scheme must be http(s): {parts.scheme or '(none)'}"
        host = parts.hostname or ""
        if not host:
            return "FETCH_ERROR: url has no host"
        blocked = _host_blocked(host, allow_loopback=_allow_loopback)
        if blocked is not None:
            return f"FETCH_ERROR: {blocked}"
        req = urllib.request.Request(current, method="GET")
        try:
            resp = opener.open(req, timeout=timeout)
            try:
                status = getattr(resp, "status", 200)
                if status < 200 or status >= 300:
                    return f"FETCH_ERROR: HTTP {status}"
                chunks = []
                remaining = max_bytes + FETCH_DRAIN_BOUND
                eof = False
                while remaining > 0:
                    chunk = resp.read(min(FETCH_CHUNK, remaining))
                    if not chunk:
                        eof = True
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                text = data[:max_bytes].decode("utf-8", errors="replace")
                if len(data) <= max_bytes and eof:
                    return text
                over = len(data) - max_bytes
                plus = "" if eof else "+"
                return f"{text}\n[truncated {over}{plus} bytes]"
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                exc.close()
            except Exception:
                pass
            # Refused auto-follow surfaces 30x here (never as a
            # returned response): re-validate the next hop in-loop.
            if 300 <= code < 400:
                try:
                    location = (exc.headers or {}).get("Location", "") or ""
                except Exception:
                    location = ""
                if not location:
                    return f"FETCH_ERROR: redirect {code} without Location"
                current = urllib.parse.urljoin(current, location)
                continue
            return f"FETCH_ERROR: HTTP {code}"
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.timeout):
                return f"FETCH_ERROR: timed out after {timeout}s: {host}"
            return f"FETCH_ERROR: {_one_line(reason)}"
        except socket.timeout:
            return f"FETCH_ERROR: timed out after {timeout}s: {host}"
        except (OSError, ValueError) as exc:
            return f"FETCH_ERROR: {_one_line(exc)}"
        except Exception as exc:
            return f"FETCH_ERROR: {_one_line(exc)}"
    return "FETCH_ERROR: too many redirects"


SEARCH_PROVIDER_HOST = "old-search.marginalia.nu"
SEARCH_DEFAULT_TIMEOUT = 20
SEARCH_MAX_RESULTS = 10
SEARCH_MAX_QUERY = 200
SEARCH_MAX_WAIT_S = 10
SEARCH_TITLE_CAP = 200
SEARCH_SNIPPET_CAP = 400

_SEARCH_CARD_RE = re.compile(r'<section[^>]*class="card search-result"[^>]*>')
_SEARCH_TITLE_RE = re.compile(
    r'<a[^>]*class="title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
_SEARCH_DESC_RE = re.compile(r'<p class="description">(.*?)</p>', re.S)
_SEARCH_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_WAIT_RE = re.compile(r"Wait For A Moment")
_SEARCH_SST_RE = re.compile(r'href="(/search\?[^"]*?&sst=[^"]+)"')
_SEARCH_WAIT_SECS_RE = re.compile(r'data-tr="(\d+)"')


def _clean_search_text(raw, cap):
    """De-tagged, entity-decoded, single-spaced text, capped."""
    text = _SEARCH_TAG_RE.sub("", raw or "")
    return " ".join(html.unescape(text).split())[:cap]


def _parse_search_results(page, limit):
    """(url, title, snippet) hits from a provider result page.

    Cards are siblings, so each segment between card openings holds
    exactly one hit: the first title anchor and description in it.
    Non-http(s) hrefs never become hits."""
    hits = []
    for segment in _SEARCH_CARD_RE.split(page)[1:]:
        if len(hits) >= limit:
            break
        title = _SEARCH_TITLE_RE.search(segment)
        if title is None:
            continue
        url = html.unescape(title.group(1)).strip()
        try:
            parts = urllib.parse.urlsplit(url)
        except Exception:
            continue
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        desc = _SEARCH_DESC_RE.search(segment)
        hits.append(
            (
                url,
                _clean_search_text(title.group(2), SEARCH_TITLE_CAP),
                _clean_search_text(desc.group(1) if desc else "", SEARCH_SNIPPET_CAP),
            )
        )
    return hits


def search_web(
    query,
    limit=5,
    timeout=SEARCH_DEFAULT_TIMEOUT,
    _allow_loopback=False,
    _base_url=None,
):
    """Keyless web search. Never raises: failures return
    "SEARCH_ERROR: <one-line reason>", no matches return
    "no results".

    One canonical provider (SEARCH_PROVIDER_HOST), fetched through
    fetch_url so the SSRF guard, per-request timeout, and redirect
    discipline apply unchanged. When the provider answers its
    documented bot-barrage wait page, honor the embedded countdown
    and follow its retry link once, then parse. Output is numbered
    title/URL/snippet hits for pairing with fetch_url (search the
    web to discover a URL, fetch_url to read it).
    """
    try:
        if not isinstance(query, str) or not query.strip():
            return "SEARCH_ERROR: empty query"
        words = query.strip()
        if len(words) > SEARCH_MAX_QUERY:
            return f"SEARCH_ERROR: query too long (max {SEARCH_MAX_QUERY} chars)"
        try:
            count = int(limit)
        except (TypeError, ValueError):
            return "SEARCH_ERROR: bad limit"
        count = max(1, min(SEARCH_MAX_RESULTS, count))
        base = _base_url or f"https://{SEARCH_PROVIDER_HOST}"
        url = base + "/search?query=" + urllib.parse.quote(words)
        body = fetch_url(url, timeout=timeout, _allow_loopback=_allow_loopback)
        if body.startswith("FETCH_ERROR:"):
            return "SEARCH_ERROR: " + body[len("FETCH_ERROR:") :].strip()
        if _SEARCH_WAIT_RE.search(body):
            retry = _SEARCH_SST_RE.search(body)
            if retry is None:
                return "SEARCH_ERROR: provider asked to wait but gave no retry link"
            asked = _SEARCH_WAIT_SECS_RE.search(body)
            wait = min(SEARCH_MAX_WAIT_S, int(asked.group(1)) if asked else 2)
            time.sleep(wait)
            again = base + html.unescape(retry.group(1))
            body = fetch_url(again, timeout=timeout, _allow_loopback=_allow_loopback)
            if body.startswith("FETCH_ERROR:"):
                return "SEARCH_ERROR: " + body[len("FETCH_ERROR:") :].strip()
            if _SEARCH_WAIT_RE.search(body):
                return "SEARCH_ERROR: provider still rate-limiting"
        hits = _parse_search_results(body, count)
        if not hits:
            return "no results"
        lines = []
        for pos, (hit_url, title, snippet) in enumerate(hits, 1):
            lines.append(f"{pos}. {title}\n   {hit_url}\n   {snippet}")
        return "\n".join(lines)
    except Exception as exc:
        return f"SEARCH_ERROR: {_one_line(exc)}"


def link_worktree_node_modules(root, store):
    """Symlink <store>/node_modules into <root>/frontend when absent (#1235).

    Best-effort: returns True on link, False otherwise; never raises.
    """
    if not store:
        return False
    try:
        store_nm = os.path.join(store, "node_modules")
        target = os.path.join(root, "frontend", "node_modules")
        if not os.path.isdir(store_nm) or os.path.lexists(target):
            return False
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.symlink(store_nm, target, target_is_directory=True)
        return True
    except OSError:
        return False

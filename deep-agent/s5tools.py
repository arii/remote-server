"""Shared deterministic helpers for S5 agent slices (deep-agent/).

Stdlib only: system-python tests import this without the venv. Wraps the
subprocess discipline every slice needs — always a timeout, never a shell,
validation before execution, capped outputs — plus repo sandboxing so tools
cannot escape their checkout.

New slices: import these instead of re-implementing gh/td/file access.
Do NOT import langchain here (keeps system-python tests working).
Do NOT put secrets in here; the model router (`model_router.py`) and the
ledger (`ledger.py`) own their own concerns.
"""

import ipaddress
import os
import re
import shlex
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request

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
CHECK_TIMEOUT = 300
CHECK_OUTPUT_LIMIT = 4000
TEST_FILE_RE = re.compile(r"^tests/test_[A-Za-z0-9_]+\.py$")


def check_allowed(argv):
    """True when argv starts with an allowlisted verify prefix, or
    runs one repo test file directly (python3 tests/test_x.py)."""
    tupled = tuple(argv)
    if len(tupled) >= 2 and tupled[0] == "python3" \
            and TEST_FILE_RE.match(tupled[1]):
        return True
    return any(tupled[:len(p)] == p for p in CHECK_PREFIXES)


def allowed_commands_text():
    """Human list of the allowlisted command heads (tool prompts)."""
    return ", ".join(" ".join(p) for p in CHECK_PREFIXES)


def run_repo_command(checkout, command):
    """Run an allowlisted verification command in checkout. Never
    raises: returns "exit=<n>\\n<tail>", "REJECTED: ...", "TIMEOUT
    ...", or "exec failed: ...". argv is tupled from shlex — never
    shell=True — so metachars are inert arg bytes, never control."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return f"REJECTED: bad command line: {exc}"
    if not argv or not check_allowed(argv):
        return ("REJECTED: command must start with one of: "
                + allowed_commands_text())
    try:
        proc = subprocess.run(
            argv, cwd=checkout, capture_output=True, text=True,
            timeout=CHECK_TIMEOUT)
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


def run(argv, cwd=None, timeout=DEFAULT_TIMEOUT):
    """Run argv (no shell). Returns stripped stdout; raises RuntimeError
    with capped stderr on failure or timeout."""
    if not isinstance(argv, list) or not argv or not all(
        isinstance(a, str) for a in argv
    ):
        raise ValueError("argv must be a non-empty list of str")
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
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


def read_repo_file(checkout, path, limit=3000):
    """Read a repo-relative file. Returns content or a not-found/REJECTED
    string (routine outcomes, not exceptions)."""
    root = _checkout_root(checkout)
    if root is None:
        return "REJECTED: bad checkout"
    full = os.path.realpath(os.path.join(root, path or ""))
    if not _inside(root, full):
        return "REJECTED: path escapes the checkout"
    if not os.path.isfile(full):
        return "not found: no such file"
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
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
            ["grep", "-rn", "-m", "20", "--exclude-dir=.git",
             "--exclude-dir=node_modules", "--exclude-dir=.venv",
             "--exclude-dir=__pycache__", "--exclude-dir=.boomtick",
             "--exclude-dir=chroma_db", "--exclude-dir=.claude",
             "--exclude-dir=.muse",
             "--exclude=ledger.jsonl",
             "--", pattern, "."],
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
        if (ip.is_loopback or ip.is_link_local or ip.is_private
                or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or not ip.is_global):
            return f"blocked non-public address {ip} for {host}"
    return None


def _one_line(exc, limit=200):
    return " ".join(str(exc).split())[:limit]


def fetch_url(url, timeout=FETCH_DEFAULT_TIMEOUT,
              max_bytes=FETCH_DEFAULT_MAX_BYTES,
              _allow_loopback=False):
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
            return (f"FETCH_ERROR: scheme must be http(s): "
                    f"{parts.scheme or '(none)'}")
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
                return (f"FETCH_ERROR: timed out after "
                        f"{timeout}s: {host}")
            return f"FETCH_ERROR: {_one_line(reason)}"
        except socket.timeout:
            return f"FETCH_ERROR: timed out after {timeout}s: {host}"
        except (OSError, ValueError) as exc:
            return f"FETCH_ERROR: {_one_line(exc)}"
        except Exception as exc:
            return f"FETCH_ERROR: {_one_line(exc)}"
    return "FETCH_ERROR: too many redirects"

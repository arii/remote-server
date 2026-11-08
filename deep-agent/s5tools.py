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

import collections
import html
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import time
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
        path = (checkout + os.pathsep
                + os.path.join(checkout, "deep-agent")
                + (os.pathsep + base if base else ""))
        return {"PYTHONPATH": path}
    except Exception:
        return {"PYTHONPATH": checkout}


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
            timeout=CHECK_TIMEOUT, env={**os.environ,
                                        **check_env(checkout)})
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
    if len(parts) >= 2 and parts[-2] == "repos" and REPO_RE.match(
            "arii/" + parts[-1]):
        return "arii/" + parts[-1]
    return ""


PR_JSON_FIELDS = ("number,title,author,state,isDraft,url,"
                  "reviewDecision,statusCheckRollup")


def read_pr(number, repo, timeout=60):
    """Read-only GitHub PR summary via gh. Never raises.

    Private repos are invisible to keyless search_web, and td-based
    PR reads need a project_config.json the checkout may not carry,
    so chat agents read PRs through gh directly (auth comes from the
    environment, same as lane tooling). Failures return
    "PR_ERROR: <one-line reason>".
    """
    num = valid_number(number)
    if num is None:
        return "PR_ERROR: bad PR number"
    text = repo.strip() if isinstance(repo, str) else ""
    if text and "/" not in text:
        text = "arii/" + text
    if not valid_repo(text):
        return "PR_ERROR: bad repo (want owner/name)"
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(num), "--repo", text,
             "--json", PR_JSON_FIELDS],
            capture_output=True, text=True, timeout=timeout)
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
        for c in checks if isinstance(c, dict))
    check_line = (", ".join(f"{k}:{v}" for k, v in sorted(states.items()))
                  or "no checks")
    line = (f"PR #{snap.get('number')} [{snap.get('state')}"
            f"{' DRAFT' if snap.get('isDraft') else ''}] "
            f"{snap.get('title')} by {(author.get('login') if isinstance(author, dict) else author) or '?'} "
            f"review={snap.get('reviewDecision') or 'none'} "
            f"checks=({check_line}) {snap.get('url') or ''}")
    return line[:MAX_OUT]


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


SEARCH_PROVIDER_HOST = "old-search.marginalia.nu"
SEARCH_DEFAULT_TIMEOUT = 20
SEARCH_MAX_RESULTS = 10
SEARCH_MAX_QUERY = 200
SEARCH_MAX_WAIT_S = 10
SEARCH_TITLE_CAP = 200
SEARCH_SNIPPET_CAP = 400

_SEARCH_CARD_RE = re.compile(
    r'<section[^>]*class="card search-result"[^>]*>')
_SEARCH_TITLE_RE = re.compile(
    r'<a[^>]*class="title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
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
        hits.append((
            url,
            _clean_search_text(title.group(2), SEARCH_TITLE_CAP),
            _clean_search_text(
                desc.group(1) if desc else "", SEARCH_SNIPPET_CAP),
        ))
    return hits


def search_web(query, limit=5, timeout=SEARCH_DEFAULT_TIMEOUT,
               _allow_loopback=False, _base_url=None):
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
            return (f"SEARCH_ERROR: query too long "
                    f"(max {SEARCH_MAX_QUERY} chars)")
        try:
            count = int(limit)
        except (TypeError, ValueError):
            return "SEARCH_ERROR: bad limit"
        count = max(1, min(SEARCH_MAX_RESULTS, count))
        base = _base_url or f"https://{SEARCH_PROVIDER_HOST}"
        url = base + "/search?query=" + urllib.parse.quote(words)
        body = fetch_url(url, timeout=timeout,
                         _allow_loopback=_allow_loopback)
        if body.startswith("FETCH_ERROR:"):
            return "SEARCH_ERROR: " + body[len("FETCH_ERROR:"):].strip()
        if _SEARCH_WAIT_RE.search(body):
            retry = _SEARCH_SST_RE.search(body)
            if retry is None:
                return ("SEARCH_ERROR: provider asked to wait but "
                        "gave no retry link")
            asked = _SEARCH_WAIT_SECS_RE.search(body)
            wait = min(SEARCH_MAX_WAIT_S,
                       int(asked.group(1)) if asked else 2)
            time.sleep(wait)
            again = base + html.unescape(retry.group(1))
            body = fetch_url(again, timeout=timeout,
                             _allow_loopback=_allow_loopback)
            if body.startswith("FETCH_ERROR:"):
                return ("SEARCH_ERROR: " +
                        body[len("FETCH_ERROR:"):].strip())
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

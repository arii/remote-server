"""Ask investigator tool library (no CLI entry: use via AuditorGraph).

Read-only tools plus one allowlisted run_repo_command verify tool
plus one SSRF-guarded fetch_url tool, all scoped to the repo's
checkout. Consumed as LangChain tools by auditor.py and
issue_generator.py; there is no standalone runner.
"""

import json
import subprocess

import cli

cli.bootstrap()

from langchain_core.tools import tool  # noqa: E402

import retrieval  # noqa: E402
import runs  # noqa: E402
import s5tools  # noqa: E402
import impact  # noqa: E402


_repo = ""
_checkout = ""


@tool
def search_docs(query: str, repo: str = "") -> str:
    """Search S5 runbooks and docs for how-to and process answers.

    Args:
        query: Short question (2-10 words).
        repo: Optional owner/name (or bare checkout name) to
            search one repo; blank searches this checkout.
    """
    if not query or not query.strip() or len(query) > 500:
        return "REJECTED: bad query"
    try:
        filt = retrieval.normalize_repo(repo)
        target = s5tools.checkout_for(filt) if filt else _checkout
    except ValueError:
        return "REJECTED: bad repo filter"
    return retrieval.search_td(query, target, top_k=3)


@tool
def read_repo_file(path: str) -> str:
    """Read a file from the repo checkout (read-only).

    Args:
        path: Repo-relative path, e.g. buttons/server.py.
    """
    # Same sandbox + messages as the inline version this replaces;
    # one implementation lives in s5tools now (like grep_repo).
    return s5tools.read_repo_file(_checkout, path)


@tool
def grep_repo(pattern: str) -> str:
    """Search file contents in the repo checkout (read-only grep).

    Always search the shortest distinctive fragment: for an HTTP
    route use the path only (e.g. '/buttons/api/ask'), never the
    method plus path ('POST ...' matches docs, never handler code).
    Hits in docs or eval files are mentions, not the implementation:
    answer from the file whose code handles the match.

    Args:
        pattern: Short literal search text.
    """
    # Delegate to the shared helper so log/index/ledger noise
    # (.boomtick/, chroma_db/, .claude/, .muse/, ledger.jsonl) never
    # floods the -m 20 window and buries real hits like server.py.
    return s5tools.grep_repo(_checkout, pattern)


def _json_or_none(text):
    try:
        return json.loads(text or "")
    except json.JSONDecodeError:
        return None


def _td_error(number, snap):
    """Format a td JSON error with the PR number so agents relay it."""
    msg = (snap.get("message") or "").strip()[:200] or "unknown error"
    code = snap.get("code", "")
    suffix = f" (code {code})" if code != "" else ""
    return f"PR #{number}: td error: {msg}{suffix}"


@tool
def pr_state(number: str) -> str:
    """Summarize a GitHub PR: state, reviews, checks (read-only).

    Args:
        number: PR number as digits, e.g. "153".
    """
    # String type: the tool wrapper coerces True to 1 for int args,
    # which would smuggle a bool past validation into the command.
    if not isinstance(number, str) or not number.isdigit() or int(number) <= 0:
        return "REJECTED: bad PR number"
    try:
        proc = subprocess.run(
            ["td", "--json", "gh", "pr-view", "--pr", str(number), "--repo", _repo],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"td failed: {exc}"
    if proc.returncode != 0:
        # td exits nonzero for API errors but still prints the JSON
        # error on stdout (stderr stays empty): parse it first.
        snap = _json_or_none(proc.stdout)
        if isinstance(snap, dict) and snap.get("status") == "error":
            return _td_error(number, snap)
        return f"td error: {(proc.stderr or '').strip()[:200]}"
    try:
        snap = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return f"td error: bad json: {(proc.stdout or '').strip()[:200]}"
    if not isinstance(snap, dict):
        return f"td error: bad json: {(proc.stdout or '').strip()[:200]}"
    if snap.get("status") == "error":
        return _td_error(number, snap)
    summary = snap.get("checksSummary") or {}
    return _short_line(
        f"{snap.get('state')} draft={snap.get('draft')} "
        f"review={snap.get('reviewDecision')} "
        f"mergeable={snap.get('mergeable')} "
        f"checks={summary.get('passed')}/{summary.get('total')} "
        f"failed={summary.get('failed')} "
        f"pending={summary.get('pending')} "
        f"title={snap.get('title')}",
        limit=400,
    )


@tool
def run_repo_command(command: str) -> str:
    """Run an allowlisted verification command in the repo checkout.

    Use AFTER reasoning to verify a claim — run the failing test,
    check git state — not to explore; read and grep first. Never
    executes anything else: anything outside the allowlist comes
    back as REJECTED text.

    Args:
        command: Full command line (allowlisted tests/lint/git
            shapes only).
    """
    return s5tools.run_repo_command(_checkout, command)


@tool
def fetch_url(url: str) -> str:
    """Fetch a URL's text: upstream docs, API references, changelogs.

    Use when you have a URL in hand (never guess deep links;
    use search_web to discover one). Returns decoded text capped
    at 256KB with a truncation marker; private/loopback/metadata
    addresses, non-http(s) schemes, and failures come back as
    FETCH_ERROR text. No search, no JS rendering, no auth, no POST.

    Args:
        url: Full http(s) URL to GET.
    """
    return s5tools.fetch_url(url)


@tool
def search_web(query: str, limit: int = 5) -> str:
    """Search the web for URLs about a topic (keyless discovery).

    Use when you need upstream docs, changelogs, or references
    you don't already have a URL for. Returns ranked
    title/URL/snippet hits; then call fetch_url on a hit to read
    it. Failures come back as SEARCH_ERROR text, no matches as
    "no results".

    Args:
        query: Search keywords, e.g. "ollama changelog".
        limit: Max hits, 1-10 (default 5).
    """
    return s5tools.search_web(query, limit=limit)


@tool
def search_turns(query: str, kind: str = "", limit: int = 5) -> str:
    """Search past agent turns for prior outcomes (read-only recall).

    Use when the user asks about earlier work: how something was
    fixed before, what triage concluded, whether this failure was
    seen already. Keyword match over each turn's script, model,
    thread, prompt, final answer, and tool names, newest first,
    capped lines.

    Args:
        query: Keywords from the earlier outcome, e.g. "triage verdict".
        kind: Optional row-kind filter, e.g. "deep-agent-run".
        limit: Max hits, 1-5 (default 5).
    """
    return runs.search_turns(query, kind=kind, limit=limit)


@tool
def run_ux_capture(
    route: str = "",
    all_routes: bool = False,
    desktop: bool = False,
    mobile: bool = False,
    screenshots_only: bool = False,
) -> str:
    """Capture the checkout's UI via td ux (Playwright).

    Runs in the repo checkout (which must carry scripts/ux-*.ts;
    baseUrl comes from its ux-audit.config.json) and writes
    artifacts/ux-audit. Slow (minutes over many viewports).

    Args:
        route: Single route to capture, e.g. "/hrm".
        all_routes: Capture every discovered route.
        desktop: Desktop viewports only.
        mobile: Mobile viewports only.
        screenshots_only: Screenshots without the audit checks.
    """
    try:
        result = impact.run_ux_capture(
            _checkout,
            route=route or "",
            all_routes=all_routes,
            desktop=desktop,
            mobile=mobile,
            screenshots_only=screenshots_only,
        )
    except ValueError as exc:
        return f"REJECTED: {exc}"
    if not result.get("ok"):
        return f"CAPTURE_ERROR: {result.get('error', 'unknown')}"
    rows = result.get("results", [])
    done = sum(1 for r in rows if isinstance(r, dict) and r.get("status") == "success")
    return (
        f"captured {done}/{len(rows)} route(s) to "
        f"{impact.UX_DIR} (snapshot before the next run)"
    )


@tool
def diff_versions(before: str, after: str) -> str:
    """Diff two ux captures into severity-ranked findings.

    Pure JSON + screenshot comparison, no model: compares
    DOM-side audit JSON (overflow, axe violations, images, tap
    targets, above-the-fold) and screenshot pairs (changed
    regions) plus lighthouse drops.

    Args:
        before: Base capture dir (checkout-relative or tmp).
        after: Head capture dir (checkout-relative or tmp).
    """
    base_dir = impact.resolve_capture_dir(_checkout, before)
    head_dir = impact.resolve_capture_dir(_checkout, after)
    if base_dir is None or head_dir is None:
        return "REJECTED: capture dirs must be existing dirs inside the checkout or tmp"
    try:
        return impact.format_diff_text(impact.diff_versions(base_dir, head_dir))
    except ValueError as exc:
        return f"REJECTED: {exc}"


@tool
def ask_diff(question: str, before: str, after: str) -> str:
    """Ask a local model about a capture diff (zero Gemini).

    Sends the ranked diff findings plus downscaled crops of
    the changed screenshot regions to the local model (Ollama
    first, Nebius fallback); full-res screenshots never ride
    the turn.

    Args:
        question: What to judge, e.g. "what visually changed".
        before: Base capture dir (checkout-relative or tmp).
        after: Head capture dir (checkout-relative or tmp).
    """
    base_dir = impact.resolve_capture_dir(_checkout, before)
    head_dir = impact.resolve_capture_dir(_checkout, after)
    if base_dir is None or head_dir is None:
        return "REJECTED: capture dirs must be existing dirs inside the checkout or tmp"
    try:
        base_snap = impact.collect_capture(base_dir)
        head_snap = impact.collect_capture(head_dir)
        result = impact.diff_versions(base_snap, head_snap)
        evidence = impact.format_evidence_for_model(question, result)
        crops = impact.changed_crops(base_snap, head_snap)
    except ValueError as exc:
        return f"REJECTED: {exc}"
    return impact.ask_diff(question, evidence, images=crops)


@tool
def lighthouse_read(capture: str = "") -> str:
    """Summarize one capture's lighthouse scores.

    Args:
        capture: Capture dir (checkout-relative or tmp);
            '' reads the checkout's current artifacts/ux-audit.
    """
    target = impact.resolve_capture_dir(_checkout, capture or impact.UX_DIR)
    if target is None:
        return "REJECTED: capture dir must exist inside the checkout or tmp"
    return impact.lighthouse_read(target)["text"]


def _short_line(content, limit=160):
    """First line of a tool result, capped for phone-sized tails.

    The REPORT block streams live to the phone; a visible ellipsis
    marks truncation so a cut line never reads as complete.
    """
    line = content.strip().splitlines()[0]
    return line if len(line) <= limit else line[:limit] + "…"

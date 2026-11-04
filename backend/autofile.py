"""Auto-file tracking issues for repeated failures (#825).

`historyUtils.ts` collapses repeat failures into "xN - same failure
repeated" rows, but the signal dies in the view: nothing files an
issue. This scanner closes that loop for the v1 kinds (CI-ish `run`
rows and `webhook` rows): when one signature fails >= threshold
times inside the window, exactly one tracking issue is filed through
the existing create-issue path.

Signature semantics mirror the history collapse (`sameFailure` =
same kind + summary) plus the repo target, so equal failures in two
checkouts file - and dedup - independently. Idempotency rides a
machine marker in the issue body (`auto-filed-signature: <hash>`):
a tick skips any signature whose marker already sits on an open
issue, and closing that issue re-arms filing (no local stamp, so a
fresh streak after close files a fresh issue).

Filing only: no auto-assign, no auto-close, no repair dispatch, and
no new failure detection beyond the collapse-rule port below. The
day-job entry is `core_cli.autofile_scan` (engine action
`autofile-scan`, seeded daily per checkout like the #822 notify
loop); this module never raises out of `scan_repo` except for an
invalid repo name.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from backend import ledger
from backend.gh import repo_gh, run_gh_json

AUTOFILE_THRESHOLD = 3
AUTOFILE_WINDOW_SECONDS = 24 * 3600
AUTOFILE_KINDS = ("run", "webhook")
AUTOFILE_MARKER_PREFIX = "auto-filed-signature:"
AUTOFILE_HASH_LEN = 12
AUTOFILE_SCAN_LIMIT = 500
AUTOFILE_TITLE_MAX = 200
AUTOFILE_BODY_MAX = 4000
AUTOFILE_BODY_MEMBERS = 10
AUTOFILE_GH_TIMEOUT = 60

# Python mirror of historyUtils.ts RULES: known noisy patterns map
# to short human sentences, everything else falls back to a generic
# label plus the kind. Kept in the same order - first match wins on
# both sides, so the summaries (and therefore the signatures) agree.
_PLAIN_RULES = [
    (re.compile(r"fast-?forward", re.I),
     "Pull could not fast-forward - the remote has new commits"),
    (re.compile(r"fetch.?first|non-fast", re.I),
     "Push was rejected - the remote has new commits"),
    (re.compile(r"connection refused|econnrefused|network "
                r"unreachable|timeout", re.I),
     "Network request failed - the service could not be reached"),
    (re.compile(r"playwright|chromium executable missing|"
                r"browser.*crash", re.I),
     "Automated browser test could not start - browser is missing "
     "or crashed"),
    (re.compile(r"annotation|check-run", re.I),
     "Automated check reported a failure"),
    (re.compile(r"conflict", re.I),
     "Changes conflict - manual review is needed"),
    (re.compile(r"permission|forbidden|unauthorized|401|403",
                re.I),
     "Action was not permitted - check access"),
]

_ERROR_WORD_RE = re.compile(
    r"error|fail|exception|stderr|at\s+\S+\(|traceback", re.I)
_ROUTINE_RE = re.compile(
    r"^(chat (started|finished|aborted)|step (auto-?done|done|"
    r"started)|created|job$)", re.I)
_RAW_FAIL_RE = re.compile(
    r"fail|error|refus|reject|conflict|not permitted|aborted",
    re.I)
_SUMMARY_FAIL_RE = re.compile(
    r"fail|error|refused|rejected|conflict|not permitted|"
    r"aborted", re.I)
_STATUS_FAIL_RE = re.compile(r"fail|error", re.I)
_ASKED_PREFIX_RE = re.compile(r"^asked:\s+", re.I)
_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$", re.I)


def plain_language(raw, fallback_kind) -> str:
    """Port of historyUtils.ts plainLanguage (same rules, same
    order, same fallbacks)."""
    text = (raw or "").strip() if isinstance(raw, str) else ""
    for rx, label in _PLAIN_RULES:
        if rx.search(text):
            return label
    if not text:
        return f"{fallback_kind or 'Event'} - no details recorded"
    first = text.split("\n")[0][:120]
    if _ERROR_WORD_RE.search(first):
        return f"{fallback_kind or 'Event'} failed - see detail trace"
    return first


def _raw_text(entry: dict):
    """Ledger row's raw event text: event ?? action ?? kind (nullish,
    like the TS - an empty string stays an empty string)."""
    raw = entry.get("event")
    if raw is None:
        raw = entry.get("action")
    if raw is None:
        raw = entry.get("kind")
    return raw


def _first_line(text) -> str:
    return text.split("\n")[0].strip() if isinstance(text, str) \
        else ""


def _err_text(entry: dict) -> str:
    err = entry.get("error")
    return err.strip() if isinstance(err, str) else ""


def _base_summary(entry: dict) -> str:
    """Collapse base: a backend-supplied error wins verbatim (capped),
    otherwise the plain-language raw text. Same input the routine
    test and the failure test see in historyUtils.ts."""
    err_text = _err_text(entry)
    if err_text:
        return err_text[:200]
    return plain_language(_raw_text(entry) or "",
                          entry.get("kind") or "")


def entry_summary(entry: dict) -> str:
    """Port of the ledgerToEntry summary path (non-cli-turn kinds):
    a supplied error stands alone; otherwise the base plus the
    outcome clause from detail/prompt/command when it adds
    information."""
    base = _base_summary(entry)
    if _err_text(entry):
        return base
    if _ROUTINE_RE.match(base):
        return base
    task = _ASKED_PREFIX_RE.sub(
        "", _first_line(entry.get("detail"))) \
        or _first_line(entry.get("prompt")) \
        or _first_line(entry.get("command"))
    clause = plain_language(task, entry.get("kind") or "") \
        if task else ""
    lower, base_lower = clause.lower(), base.lower()
    if clause and lower != base_lower \
            and not lower.startswith(base_lower):
        return f"{base} - {clause}"
    return base


def entry_failed(entry: dict, raw, summary: str) -> bool:
    """Port of entryFailed/isFailure: explicit ok/exit/status flags
    first, then the failure-word regexes over raw + summary."""
    if entry.get("ok") is False:
        return True
    exit_code = entry.get("exit")
    if isinstance(exit_code, int) \
            and not isinstance(exit_code, bool) \
            and exit_code != 0:
        return True
    status = entry.get("status")
    if status is not None and _STATUS_FAIL_RE.search(str(status)):
        return True
    if _RAW_FAIL_RE.search(raw or ""):
        return True
    return bool(_SUMMARY_FAIL_RE.search(summary or ""))


def failure_signature(entry: dict):
    """(kind, repo, summary) for a countable failure, else None.

    History collapse groups by kind + summary (sameFailure); the
    autofile key adds the repo target so equal failures in two
    checkouts file independently. Routine lifecycle rows are never
    failures (toRow marks them muted, never "same failure
    repeated"), and rows without a repo target are unfileable.
    """
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind")
    if kind not in AUTOFILE_KINDS:
        return None
    repo = entry.get("repo")
    if not repo or not isinstance(repo, str):
        return None
    base = _base_summary(entry)
    if _ROUTINE_RE.match(base):
        return None
    summary = entry_summary(entry)
    if not entry_failed(entry, _raw_text(entry) or "", summary):
        return None
    return (kind, repo, summary)


def signature_hash(key) -> str:
    """Stable short hash of a signature key (the dedup token)."""
    text = json.dumps(list(key), sort_keys=True)
    return hashlib.sha1(text.encode()).hexdigest()[:AUTOFILE_HASH_LEN]


def marker_for(key) -> str:
    """Machine dedup marker line carried in the filed issue body."""
    return f"{AUTOFILE_MARKER_PREFIX} {signature_hash(key)}"


def _entry_ts(entry):
    ts = entry.get("ts") if isinstance(entry, dict) else None
    if isinstance(ts, bool):
        return None
    return ts if isinstance(ts, (int, float)) else None


def find_repeated_failures(entries, now: float,
                           threshold: int = AUTOFILE_THRESHOLD,
                           window_seconds:
                               int = AUTOFILE_WINDOW_SECONDS) -> dict:
    """sig-key -> newest-first member list for signatures failing >=
    threshold times in (now - window, now]. Only rows with a numeric
    ts inside the window count: undated rows can't be proven fresh,
    and future rows are clock skew, not failures."""
    since = now - window_seconds
    groups: dict = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        ts = _entry_ts(entry)
        if ts is None or ts < since or ts > now:
            continue
        key = failure_signature(entry)
        if key is None:
            continue
        groups.setdefault(key, []).append(entry)
    out = {}
    for key, members in groups.items():
        if len(members) >= threshold:
            members.sort(key=lambda e: _entry_ts(e) or 0,
                         reverse=True)
            out[key] = members
    return out


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _member_text(entry: dict, kind: str) -> str:
    text = _first_line(entry.get("detail")) \
        or _first_line(entry.get("error")) \
        or _first_line(entry.get("command")) \
        or _first_line(entry.get("action"))
    if kind == "run":
        exit_code = entry.get("exit")
        if isinstance(exit_code, int) \
                and not isinstance(exit_code, bool):
            text = f"[exit {exit_code}] {text}" if text \
                else f"[exit {exit_code}]"
    return text[:160]


def _compare_link(members: list, gh: str):
    """Newest webhook member carrying both observed SHAs yields a
    real compare link; anything else yields None (never a fake
    link)."""
    for entry in members:
        observed = entry.get("observed")
        if not isinstance(observed, dict):
            continue
        head = observed.get("head") or ""
        origin = observed.get("origin") or ""
        if _SHA_RE.match(head) and _SHA_RE.match(origin):
            return (f"https://github.com/{gh}/compare/"
                    f"{head}...{origin}")
    return None


def build_issue(key, members: list, gh: str,
                window_seconds:
                    int = AUTOFILE_WINDOW_SECONDS) -> tuple:
    """(title, body) for one repeated-failure signature. Title fits
    the create path's 200-char cap; the body fits its 4000-char cap
    with the marker always last and intact (occurrences trim first,
    never the marker)."""
    kind, repo, summary = key
    count = len(members)
    one_line = " ".join(summary.split())
    head = f"[autofile] {kind} failures x{count}: "
    room = AUTOFILE_TITLE_MAX - len(head)
    title = head + (one_line if len(one_line) <= room
                    else one_line[:max(0, room - 3)] + "...")
    hours = window_seconds / 3600
    window_label = f"{hours:g}h"
    first = _fmt_time(_entry_ts(members[-1]) or 0)
    last = _fmt_time(_entry_ts(members[0]) or 0)
    lines = [
        f"# Repeated failure: {summary}",
        "",
        f"Auto-filed: {count} same-signature {kind} failures in "
        f"repo {repo} within the last {window_label} "
        f"(threshold >={AUTOFILE_THRESHOLD}).",
        "",
        "## Signature",
        "",
        f"- kind: {kind}",
        f"- repo: {repo}",
        f"- summary: {summary}",
        "",
        "## Window",
        "",
        f"- failures: {count}",
        f"- first: {first}",
        f"- last: {last}",
        "",
        "## Occurrences",
        "",
    ]
    shown = members[:AUTOFILE_BODY_MEMBERS]
    for entry in shown:
        at = _fmt_time(_entry_ts(entry) or 0)
        text = _member_text(entry, kind)
        lines.append(f"- {at} - {text}" if text else f"- {at}")
    if len(members) > len(shown):
        lines.append(f"- ...and {len(members) - len(shown)} more")
    lines += [
        "",
        "## Links",
        "",
        f"- repo: https://github.com/{gh}",
    ]
    if kind == "run":
        lines.append(f"- actions: https://github.com/{gh}/actions")
    compare = _compare_link(members, gh) if kind == "webhook" \
        else None
    if compare:
        lines.append(f"- compare: {compare}")
    lines += [
        f"- history: console History tab (kind filter: `{kind}`)",
        "",
        "---",
        marker_for(key),
        "",
        "_Closing this issue re-arms filing: a fresh streak files "
        "a fresh issue._",
    ]
    body = "\n".join(lines)
    if len(body) > AUTOFILE_BODY_MAX:
        # Rebuild without occurrences: the fixed sections always fit
        # (~700 chars), so the marker survives intact.
        kept = []
        skip = False
        for line in lines:
            if line == "## Occurrences":
                skip = True
            elif line == "## Links":
                skip = False
            if skip and (line.startswith("- ") or line == ""
                         or line == "## Occurrences"):
                continue
            kept.append(line)
        body = "\n".join(kept)
        if len(body) > AUTOFILE_BODY_MAX:
            body = body[:AUTOFILE_BODY_MAX - 3] + "..."
            # Marker rescue: truncation must never eat the marker.
            marker = marker_for(key)
            if marker not in body:
                body = body[:AUTOFILE_BODY_MAX - len(marker) - 1] \
                    + "\n" + marker
    return title, body


def open_issue_has_marker(gh: str, marker: str,
                          runner=None) -> bool:
    """True when an open issue already carries the marker (server-
    side --search over title/body, the gh equivalent the issue
    specifies). Propagates gh failures: the caller treats a failed
    check as skip-with-error, never as proof of absence."""
    rows = run_gh_json(
        gh, ["issue", "list", "--repo", gh, "--state", "open",
             "--search", marker, "--json", "number", "--limit",
             "5"],
        timeout=AUTOFILE_GH_TIMEOUT, runner=runner)
    return bool(rows) if isinstance(rows, list) else False


def _default_filer(repo: str, title: str, body: str) -> str:
    """File through the existing create-issue handler (same
    validation, same shape limits). labels=[] short-circuits
    _validate_create_labels with no extra gh call."""
    from backend.api.routers.issues import CreateIssueRequest, \
        create_issue

    res = create_issue(CreateIssueRequest(
        repo=repo, title=title, body=body, labels=[]))
    return res.get("url", "") if isinstance(res, dict) else ""


def _default_reader(since: float, limit: int) -> list:
    rows: list = []
    for kind in AUTOFILE_KINDS:
        rows.extend(ledger.read(kind=kind, limit=limit,
                                since=since))
    return rows


def scan_repo(repo: str, gh: str | None = None,
              now: float | None = None,
              threshold: int = AUTOFILE_THRESHOLD,
              window_seconds: int = AUTOFILE_WINDOW_SECONDS,
              reader=None, marker_check=None,
              filer=None) -> dict:
    """One repo's autofile tick: file each over-threshold signature
    once, skip signatures whose marker issue is still open. Returns
    {repo, gh, scanned, signatures, filed, skipped, errors}; per-
    signature failures land in errors (fail-closed: a failed marker
    check files nothing). reader/marker_check/filer are injectable
    seams for tests."""
    at = time.time() if now is None else now
    gh_path = gh or repo_gh(repo)
    read = reader or _default_reader
    check = marker_check or open_issue_has_marker
    write = filer or _default_filer
    entries = [e for e in (read(since=at - window_seconds,
                                limit=AUTOFILE_SCAN_LIMIT) or [])
               if isinstance(e, dict) and e.get("repo") == repo]
    groups = find_repeated_failures(entries, at, threshold,
                                    window_seconds)
    result: dict = {"repo": repo, "gh": gh_path,
                    "scanned": len(entries),
                    "signatures": len(groups), "filed": [],
                    "skipped": [], "errors": []}
    for key in sorted(groups):
        digest = signature_hash(key)
        members = groups[key]
        try:
            present = check(gh_path, marker_for(key))
        except Exception as exc:
            result["errors"].append(
                {"signature": digest,
                 "error": f"marker check failed: {exc}"[:300]})
            continue
        if present:
            result["skipped"].append(
                {"signature": digest, "count": len(members),
                 "marker": marker_for(key)})
            continue
        title, body = build_issue(key, members, gh_path,
                                  window_seconds)
        try:
            url = write(repo, title, body)
        except Exception as exc:
            result["errors"].append(
                {"signature": digest,
                 "error": f"file failed: {exc}"[:300]})
            continue
        result["filed"].append(
            {"signature": digest, "count": len(members),
             "marker": marker_for(key), "url": url or ""})
    return result

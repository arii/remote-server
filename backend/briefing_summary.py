#!/usr/bin/env python3
"""briefing_summary — readable repo briefing for the S5 buttons page.

Why this lives here, not in td: td has no briefing summarization
that agentic tools can call, and its output modes both fail a human
reader — the CLI defaults to JSON (machine consumption), while human
mode for search-prs only prints "Found N PRs" with no per-PR detail.
So the buttons side parses td's JSON plus `gh` enrichment and prints
a briefing a person can act on from a phone.

Scope: any repo the server discovers. The caller
(buttons/server.py, via the TD prefix) runs this with cwd = the
repo checkout and .env sourced, so `td` and `gh` (authed in the
terminal container) work.

Modes:
  --briefing          print the readable briefing to stdout
  --briefing-json     print analyze() as JSON (the live Briefing
                      view on the buttons page renders this)
  --repo NAME         short repo name (the page picker's value).
                      Requires --gh: without it the default gh
                      path would brief the wrong repo.
  --gh OWNER/REPO     GitHub path for the gh api paths. td runs
                      with cwd = that repo's checkout, so it
                      picks up that repo's project_config.json;
                      the gh paths must name the same repo.
  --plan-dir DIR      write DIR/<date>-briefing-plan.md: the briefing,
                      then a Next-actions checklist DERIVED from the
                      findings (one checkbox per attention item, not
                      generic lines), then the raw td JSON as an
                      appendix (audit trail; the Plans excerpt reads
                      from the top of the file, so the appendix never
                      pollutes what the page shows)

Data (all stdlib, all defensive — an unknown field shape degrades
one section, never the whole briefing):
  gh pr list --state open
      number, title, author, headRefName, baseRefName, isDraft,
      mergeStateStatus, updatedAt, url. REST, not search: GitHub's
      search index lags merges by minutes and briefing surfaced
      merged PRs as open (#446). NOTE: no CI state, no reviews,
      no changed files — that is why gh enrichment exists.
  gh pr list --state merged
      Recently-merged heads, to relabel status-board rows whose
      branch still shows Open after its PR merged (branches here
      are not auto-deleted, so the board row lingers).
  gh pr view N --json ...statusCheckRollup,reviewDecision,mergeable...
      CI from statusCheckRollup (failing / pending / passing /
      no checks), reviewDecision, mergeable.
  td --json gh status-board   work board (branch / issue / status)
  td --json gh detect-conflicts --all   overlap scan (shape varies;
      parsed defensively, see _overlap_prs). NOTE: despite the command
      name this reports PR-vs-PR file overlap, not merge conflicts.
  td --json repo workflow-runs   recent runs; failures only shown.
  td --json gh read-pr-reviews --pr N   per-PR review substance
      (GitHub review objects: user.login, state, body). The v1
      briefing used only reviewDecision (the verdict); v2 shows what
      reviewers actually said, because the local agent's job is to
      read existing expert work, not redo it.
  gh api repos/<repo>/actions/runs/ID/jobs   failing
      job + step names for each failing run (the likely cause).

Attention ranking (first match wins per PR, in this order):
  conflicted > CI failing > changes requested > overlaps > stale (>30 days).
  A changes-requested item now quotes the review excerpt, so the
  action names what to address instead of just who objected.
REVIEW_REQUIRED is deliberately NOT attention: in a solo repo most
open PRs sit at REVIEW_REQUIRED, so flagging them would make every
briefing shout. Drafts are listed, never attention items.

Single-run worst case (#487): the serial chain is pr-list-open 60s
  + pr-list-merged 30s + status-board 60s + detect-conflicts 120s
  + workflow-runs 60s, plus per-PR enrichment
  capped at ENRICHMENT_CAP PRs × (gh pr view 30s + read-pr-reviews
  30s), plus one 30s jobs call per failing run. Enrichment covers
  the most-recently-updated open PRs first; when open PRs exceed
  the cap the briefing carries an explicit enrichment_note
  ("showing enrichment for N of M open PRs") instead of silently
  stopping.
"""

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone

try:
    from backend import context_filter
except ImportError:
    import context_filter

STALE_DAYS = 30
RAW_CAP = 20_000  # raw appendix cap, chars

# Per-PR enrichment bound (#487): both the gh pr view loop and the
# read-pr-reviews loop enrich at most this many PRs, most-recently-
# updated first. Single-run worst case for enrichment is therefore
# 2 * ENRICHMENT_CAP subprocesses * 30s timeout each (10 min at the
# default cap of 10), regardless of how many PRs are open. PRs past
# the cap keep their list row but get no CI/review enrichment, and
# fetch() records an explicit enrichment_note so the view never
# implies full coverage.
ENRICHMENT_CAP = 10

# The repo briefed: --repo is the short name the page uses,
# --gh is the GitHub owner/repo path for the gh api paths. There
# is deliberately no repo table here — the server discovers
# repos from ~/repos and passes both flags, so a new repo needs
# no summarizer change. The td commands run with cwd = the repo
# checkout (the server's TD prefix cds there), so td picks up
# that repo's project config; the gh api paths must name the
# same repo, which is why --repo without --gh is an error: the
# default gh path would silently brief the wrong repo.
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
GH_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPO_KEY = "tech-dancer"
REPO_GH = "arii/tech-dancer"
FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}


def getf(d, *names, default=None):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return default


def _json_error_message(blob):
    """A useful message from a td --json error blob, or ''.

    td --json failures print a JSON error blob whose last line is
    '}' — surfacing the blob's own message fields instead keeps
    the UI from showing a bare brace."""
    if not isinstance(blob, dict):
        return ""
    for key in ("message", "error", "detail"):
        val = blob.get(key)
        if isinstance(val, str):
            val = val.strip()
            if val and val != "None":
                return val
    typ = blob.get("type")
    if isinstance(typ, str):
        typ = typ.strip()
        if typ and typ != "None":
            return typ
    return ""


def run_json(cmd, timeout, cwd=None):
    """Run cmd, return (parsed JSON or None, raw stdout, error str)."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout,
                              text=True, cwd=cwd)
    except Exception as exc:
        return None, "", str(exc)
    raw = proc.stdout or ""
    if proc.returncode != 0:
        # td --json failures print a JSON error blob (on stderr,
        # sometimes stdout) — prefer its message over the last line.
        for stream in (proc.stderr or "", raw):
            try:
                blob = json.loads(stream)
            except Exception:
                continue
            detail = _json_error_message(blob)
            if detail:
                return None, raw, detail[:160]
        pools = [(proc.stderr or "").strip().splitlines()]
        # td sometimes reports the failure on stdout instead.
        pools.append(raw.strip().splitlines())
        detail = ""
        for pool in pools:
            meaningful = [line for line in pool
                          if any(ch.isalnum() for ch in line)]
            if meaningful:
                detail = meaningful[-1].strip()
                break
        return None, raw, (detail[:160] if detail
                           else f"exit {proc.returncode}")
    try:
        return json.loads(raw), raw, ""
    except Exception:
        return None, raw, "output was not JSON"


def _config_repo():
    """github_repo of the checkout td discovers from our cwd (or '')."""
    try:
        with open("project_config.json", encoding="utf-8") as fh:
            return json.load(fh).get("github_repo", "") or ""
    except Exception:
        return ""


def _target_checkout_cwd():
    """Local checkout dir for the briefing target, if it has td config."""
    for base in ("/home/ari/repos", "/srv/repos"):
        path = os.path.join(base, REPO_KEY)
        if os.path.isfile(os.path.join(path, "project_config.json")):
            return path
    return ""


def fetch_pr_diff(pr_number: int, max_file_lines: int = 200) -> str:
    """Fetch PR diff via td and pre-filter using context_filter.filter_diff()."""
    data, _, _ = run_json(
        ["td", "--json", "gh", "pr-diff", "--pr", str(pr_number),
         "--repo", REPO_GH], 30)
    text = (data or {}).get("diffText") or ""
    if not text:
        return ""
    try:
        return context_filter.filter_diff(text, max_file_lines=max_file_lines)
    except Exception:
        return ""


def ci_summary(rollup):
    """(label, failing check names) from a gh statusCheckRollup list."""
    if rollup is None:
        return "unknown", []
    if not rollup:
        return "no checks", []
    failing = [getf(c, "name", default="?") for c in rollup
               if getf(c, "conclusion") in FAILING_CONCLUSIONS]
    if failing:
        return "failing", failing
    if any(getf(c, "status") != "COMPLETED" for c in rollup):
        return "pending", []
    return "passing", []


def _deep_list(node, key, depth=0):
    """First list found under `key` in a nested payload (td wraps
    data in envelopes that vary by command/version)."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        val = node.get(key)
        if isinstance(val, list):
            return val
        for sub in node.values():
            found = _deep_list(sub, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_list(item, key, depth + 1)
            if found is not None:
                return found
    return None


def _excerpt(body, limit=200):
    """One line of review text, whitespace-collapsed, truncated."""
    text = re.sub(r"<!--.*?-->", "", str(body or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _normalize_review(raw):
    """(user, state, body, submitted) from a GitHub review object."""
    if not isinstance(raw, dict):
        return None
    user = raw.get("user")
    user = user.get("login") if isinstance(user, dict) else user
    state = str(getf(raw, "state", default="") or "").upper()
    return (str(user or "?"),
            state,
            _excerpt(getf(raw, "body", default="")),
            str(getf(raw, "submitted_at", "submittedAt",
                     default="") or ""))


def _overlap_prs(node, out, in_conf=False):
    """Collect {number: {"files": [...], "with": [...]}} from a
    detect-conflicts payload of unknown shape.

    NOTE on naming: `td gh detect-conflicts` does not detect merge
    conflicts — it detects file overlap between open PRs (two PRs
    touching the same paths). A dict counts when it pairs a PR number
    (or a 'prs' list of PR numbers, as td emits) with a truthy overlap
    indicator (a files list, hasConflicts) or when it sits inside a
    conflicts list — e.g. {"conflicts": [{"prs": [4814, 4826],
    "files": ["package.json"]}]}. "with" holds the other PR numbers
    paired in the same dict. Unknown shapes yield nothing rather than
    a guessed overlap."""
    if isinstance(node, dict):
        num = getf(node, "pr", "prNumber", "pr_number", "number")
        nums = [num] if isinstance(num, int) else []
        prs_val = node.get("prs")
        if isinstance(prs_val, list):
            nums.extend(n for n in prs_val if isinstance(n, int))
        files = []
        for key in ("conflicts", "conflictedFiles", "conflicted_files",
                    "files"):
            val = node.get(key)
            if isinstance(val, list) and val and key != "conflicts":
                files = [str(v) for v in val]
            elif isinstance(val, str) and val:
                files = [val]
        flag = node.get("hasConflicts", node.get("has_conflicts"))
        if nums and (files or flag is True or in_conf):
            for n in nums:
                entry = out.setdefault(n, {"files": [], "with": []})
                for f in files:
                    if f not in entry["files"]:
                        entry["files"].append(f)
                for peer in nums:
                    if peer != n and peer not in entry["with"]:
                        entry["with"].append(peer)
        for key, val in node.items():
            _overlap_prs(val, out,
                         in_conf or key in ("conflicts", "conflictedFiles",
                                            "conflicted_files"))
    elif isinstance(node, list):
        for item in node:
            _overlap_prs(item, out, in_conf)


PR_LIST_FIELDS = ("number,title,author,headRefName,baseRefName,"
                  "isDraft,mergeStateStatus,updatedAt,url")


def _updated_key(pr):
    """Sort key for enrichment order: most-recently-updated first.

    ISO-8601 updatedAt strings sort lexicographically; missing or
    unparseable stamps sort last so PRs with no timestamp never
    crowd out dated ones."""
    updated = getf(pr, "updatedAt", "updated_at", default="") or ""
    return str(updated)


def _enrichment_targets(pr_list):
    """PRs to enrich: most-recently-updated first, capped (#487)."""
    ranked = sorted(pr_list, key=_updated_key, reverse=True)
    return ranked[:ENRICHMENT_CAP]


def fetch():
    """Gather everything; each source degrades independently."""
    data = {}
    # REST source, not td search-prs: the search index lags merges
    # by minutes (#446). gh pr list returns a bare list.
    data["prs"], data["prs_raw"], data["prs_err"] = run_json(
        ["gh", "pr", "list", "--state", "open", "--repo", REPO_GH,
         "--limit", "50", "--json", PR_LIST_FIELDS], 60)
    # Recently-merged heads for the board relabel below. Failure
    # degrades to today's board rendering (no relabel), never an
    # error section: this is enrichment, like the per-PR gh views.
    merged, _, _ = run_json(
        ["gh", "pr", "list", "--state", "merged", "--repo", REPO_GH,
         "--limit", "50", "--json", "number,headRefName"], 30)
    data["merged_heads"] = {
        row.get("headRefName") for row in (merged or [])
        if isinstance(row, dict) and row.get("headRefName")}
    # status-board/detect-conflicts have no --repo flag: they read the
    # checkout td discovers from cwd. Scope via the target checkout
    # when it exists; otherwise say so instead of showing the wrong
    # repo's data.
    scoped_cwd = _target_checkout_cwd() or None
    if not scoped_cwd and REPO_GH != _config_repo():
        unscoped = (f"unavailable for {REPO_GH} "
                    "(needs a local checkout)")
        data["board"], data["board_raw"], data["board_err"] = \
            None, "", unscoped
        data["conf"], data["conf_raw"], data["conf_err"] = \
            None, "", unscoped
    else:
        data["board"], data["board_raw"], data["board_err"] = run_json(
            ["td", "--json", "gh", "status-board"], 60, cwd=scoped_cwd)
        data["conf"], data["conf_raw"], data["conf_err"] = run_json(
            ["td", "--json", "gh", "detect-conflicts", "--all"], 120,
            cwd=scoped_cwd)
    data["runs"], data["runs_raw"], data["runs_err"] = run_json(
        ["td", "--json", "repo", "workflow-runs", "--repo", REPO_GH], 60)
    prs = data["prs"] if isinstance(data["prs"], list) else []
    data["pr_list"] = [p for p in prs if isinstance(p, dict)]
    # Per-PR enrichment, capped (#487): both loops share one bound
    # and one most-recently-updated-first order, so subprocess count
    # stays flat regardless of open PRs. PRs past the cap keep
    # their list row; the enrichment_note below says so honestly.
    targets = _enrichment_targets(data["pr_list"])
    data["enrichment_total"] = len(data["pr_list"])
    data["enrichment_cap"] = ENRICHMENT_CAP
    data["enrichment_shown"] = len(targets)
    data["enrichment_truncated"] = (
        len(data["pr_list"]) > ENRICHMENT_CAP)
    if data["enrichment_truncated"]:
        data["enrichment_note"] = (
            f"showing enrichment for {len(targets)} of "
            f"{len(data['pr_list'])} open PRs "
            f"(most-recently-updated first, cap {ENRICHMENT_CAP})")
    else:
        data["enrichment_note"] = ""
    # gh enrichment, one call per enriched PR.
    data["gh"] = {}
    for pr in targets:
        num = getf(pr, "number")
        if not isinstance(num, int):
            continue
        view, _, _ = run_json([
            "gh", "pr", "view", str(num), "--repo", REPO_GH, "--json",
            "number,title,headRefName,mergeable,mergeStateStatus,"
            "reviewDecision,statusCheckRollup,updatedAt,author,isDraft,url",
        ], 30)
        if view:
            data["gh"][num] = view
    # Review substance, one td call per enriched PR (same cap and
    # order as gh enrichment above).
    data["reviews"] = {}
    data["reviews_err"] = {}
    data["reviews_raw_parts"] = []
    for pr in targets:
        num = getf(pr, "number")
        if not isinstance(num, int):
            continue
        payload, raw, err = run_json(
            ["td", "--json", "gh", "read-pr-reviews", "--pr", str(num),
             "--repo", REPO_GH],
            30)
        if raw:
            data["reviews_raw_parts"].append(raw[:4000])
        if payload is None:
            data["reviews_err"][num] = err
            continue
        found = _deep_list(payload, "reviews")
        reviews = []
        if isinstance(found, list):
            for item in found:
                norm = _normalize_review(item)
                if norm:
                    reviews.append(norm)
        # Latest first; unparseable dates sort last.
        reviews.sort(key=lambda r: r[3], reverse=True)
        data["reviews"][num] = reviews
    # Failing-job cause for each failing run (gh api, per run).
    data["jobs"] = {}
    runs_list = []
    if data["runs"]:
        runs_list = getf(data["runs"], "workflow_runs", "workflowRuns",
                         default=[]) or []
    if not isinstance(runs_list, list):
        runs_list = []
    for run in runs_list:
        if not isinstance(run, dict):
            continue
        concl = getf(run, "conclusion")
        status = getf(run, "status")
        if not (concl == "failure" or (concl is None and status == "failure")):
            continue
        run_id = getf(run, "id")
        if run_id is None:
            continue
        jobs_payload, _, _ = run_json(
            ["gh", "api",
             f"repos/{REPO_GH}/actions/runs/{run_id}/jobs"], 30)
        jobs = _deep_list(jobs_payload, "jobs") if jobs_payload else None
        failed = []
        if isinstance(jobs, list):
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                if getf(job, "conclusion") != "failure":
                    continue
                steps = [getf(s, "name", default="?")
                         for s in (job.get("steps") or [])
                         if isinstance(s, dict)
                         and getf(s, "conclusion") == "failure"]
                failed.append((getf(job, "name", default="?"), steps[:3]))
        data["jobs"][str(run_id)] = failed[:3]
    return data


def analyze(data):
    """Structured briefing. build() renders text from this and
    --briefing-json prints it, so the page cards and the text
    briefing can never disagree about a PR. Section bodies are
    pre-rendered as line lists with the exact v2 wording, so the
    text output is unchanged by the refactor."""
    prs = data["pr_list"]
    gh = data["gh"]
    overlaps = {}
    if data["conf"]:
        _overlap_prs(data["conf"], overlaps)

    pr_infos = []
    attention = []
    for pr in prs:
        num = getf(pr, "number")
        title = getf(pr, "title", default="?")
        branch = getf(pr, "headRefName", "head_ref_name", default="?")
        draft = bool(getf(pr, "isDraft", "is_draft", default=False))
        updated = getf(pr, "updatedAt", "updated_at", default="") or ""
        updated_day = updated[:10]
        view = gh.get(num, {}) if isinstance(num, int) else {}
        rollup = getf(view, "statusCheckRollup", "status_check_rollup")
        ci, failing = ci_summary(rollup)
        merge = (getf(view, "mergeStateStatus", "merge_state_status")
                 or getf(pr, "mergeStateStatus", "merge_state_status")
                 or "?")
        merge = str(merge).lower()
        # True merge conflicts come only from GitHub's own mergeability
        # verdict (mergeStateStatus DIRTY). The td overlap scan is PR-vs-PR
        # file overlap, not mergeability — it must never set the
        # conflicted flag (it once marked every overlapping PR, drafts
        # included, as conflicted with base).
        is_conflict = merge == "dirty"
        ov = overlaps.get(num, {}) if isinstance(num, int) else {}
        ov_peers = sorted(ov.get("with", []))
        ov_files = sorted(ov.get("files", []))
        review = getf(view, "reviewDecision", "review_decision")
        pr_reviews = data["reviews"].get(num, []) \
            if isinstance(num, int) else []
        latest_by_state = {}
        for user, state, body, _submitted in pr_reviews:
            latest_by_state.setdefault(state, (user, body))
        review_suffix = ""
        if latest_by_state:
            review_suffix = " — reviews: " + "; ".join(
                f"@{user} {state}" for state, (user, _body)
                in sorted(latest_by_state.items()))
        requested = latest_by_state.get("CHANGES_REQUESTED")
        # Stale: updated more than STALE_DAYS ago (unparseable = not stale).
        stale = False
        try:
            stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - stamp).days > STALE_DAYS
        except Exception:
            pass
        label = f'#{num} "{title}"'
        detail = f"— {branch} — CI: {ci}"
        if failing:
            detail += f" ({', '.join(failing[:3])})"
        detail += f" — merge: {merge} — updated {updated_day}"
        detail += review_suffix
        if draft:
            detail += " — draft"
        # At most two excerpts per PR: the latest
        # changes-requested, then the latest approval, then the
        # latest comment with text.
        picked = []
        for want in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED"):
            for user, state, body, _s in pr_reviews:
                if state == want:
                    picked.append({"user": user, "state": state,
                                   "body": body})
                    break
            if len(picked) >= 2:
                break
        pr_infos.append({
            "number": num, "title": title, "branch": branch,
            "draft": draft, "updated": updated_day, "ci": ci,
            "failing": failing, "merge": merge,
            "conflicted": bool(is_conflict), "stale": stale,
            "overlaps": {"with": ov_peers, "files": ov_files},
            "review": review, "line": f"- {label} {detail}",
            "verdicts": [{"state": s, "user": u}
                         for s, (u, _b) in sorted(latest_by_state.items())],
            "requested": ({"user": requested[0], "body": requested[1]}
                          if requested else None),
            "reviews_picked": picked, "reviews_total": len(pr_reviews),
            "url": getf(view, "url") or getf(pr, "url"),
        })
        if draft:
            continue
        if is_conflict:
            attention.append((0, f"{label} — conflicted with base",
                             f"<!-- action:update pr:{num} -->"))
        elif ci == "failing":
            attention.append((1, f"{label} — CI failing"
                             + (f": {', '.join(failing[:3])}" if failing else ""),
                             f"<!-- action:fix-ci pr:{num} -->"))
        elif review == "CHANGES_REQUESTED":
            item = f"{label} — changes requested"
            if requested:
                user, body = requested
                item += f" by @{user}"
                if body:
                    item += f": '{body}'"
            attention.append((2, item, f"<!-- action:review pr:{num} -->"))
        elif ov_peers:
            peers = ", ".join(f"#{p}" for p in ov_peers)
            on_files = ", ".join(ov_files[:3]) if ov_files else "shared files"
            attention.append((2, f"{label} — overlaps with {peers} on "
                                f"{on_files} (coordinate merge order)",
                             f"<!-- action:review pr:{num} -->"))
        elif stale:
            attention.append((3, f"{label} — stale (updated {updated_day})",
                             f"<!-- action:review pr:{num} -->"))
    reviews_lines = []
    reviews = data["reviews"]
    reviews_err = data["reviews_err"]
    if reviews:
        for pr in prs:
            num = getf(pr, "number")
            if not isinstance(num, int) or num not in reviews:
                continue
            items = reviews[num]
            if not items:
                continue
            picked = []
            for want in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED"):
                for user, state, body, _s in items:
                    if state == want:
                        picked.append((user, state, body))
                        break
                if len(picked) >= 2:
                    break
            for i, (user, state, body) in enumerate(picked):
                verb = state.lower().replace("_", " ")
                line = f"- PR #{num} — {verb} by @{user}"
                if body:
                    line += f" — '{body}'"
                if i == len(picked) - 1 and len(items) > len(picked):
                    line += f" (+{len(items) - len(picked)} more)"
                reviews_lines.append(line)
        # No placeholder when nothing showed: empty sections stay
        # empty and the frontend shows one all-clear instead.
    elif reviews_err:
        first_err = next(iter(reviews_err.values()), "")
        reviews_lines.append(f"Reviews unavailable: {first_err}")
    if reviews_err:
        for num, err in sorted(reviews_err.items()):
            reviews_lines.append(f"Reviews unavailable for #{num}: {err}")

    board = getf(data["board"], "work", default=[]) if data["board"] else []
    board_items = []
    board_lines = []
    # Branches whose PR already merged but whose board row still says
    # Open (status-board lags like search did; branches here are not
    # auto-deleted so the row lingers). Relabel, never drop: the row
    # still names a live branch. A branch with an open PR is never
    # relabeled, even if an older same-branch PR merged.
    merged_heads = {h for h in (data.get("merged_heads") or set())
                    if h}
    open_heads = {getf(p, "headRefName", "head_ref_name")
                  for p in prs if isinstance(p, dict)}
    open_heads = {h for h in open_heads if h}
    if data["board_err"]:
        board_lines.append(f"Work board unavailable: {data['board_err']}")
    elif isinstance(board, list) and board:
        for item in board:
            # td marks missing fields with an em dash; treat it,
            # "" and "?" all as absent so lines never double-dash.
            def _present(value):
                value = (value or "").strip()
                return "" if value in ("", "?", "—") else value
            branch = _present(getf(item, "branch", default="?")) or "?"
            issue = _present(getf(item, "issue", default="?"))
            status = _present(getf(item, "status", default="?"))
            if (status.lower() == "open" and branch != "?"
                    and branch in merged_heads
                    and branch not in open_heads):
                status = "Merged"
            board_items.append({
                "branch": branch,
                "issue": issue,
                "status": status,
            })
            parts = [branch]
            if issue:
                parts.append(f"issue {issue}")
            if status:
                parts.append(status)
            board_lines.append("- " + " — ".join(parts))
    # No "no items" placeholder: empty stays empty (all-clear above).

    conflicts_list = [{"number": p["number"], "merge": "dirty"}
                      for p in pr_infos if p["conflicted"]]
    conflicts_lines = []
    if conflicts_list:
        for entry in conflicts_list:
            conflicts_lines.append(f"- PR #{entry['number']} (merge: dirty)")
    # No "none detected" placeholder: empty stays empty.
    overlaps_list = [{"number": num, "with": sorted(e["with"]),
                      "files": sorted(e["files"])}
                     for num, e in sorted(overlaps.items())]
    overlaps_lines = []
    if data["conf_err"]:
        overlaps_lines.append(f"Overlap scan unavailable: {data['conf_err']}")
    elif overlaps_list:
        for entry in overlaps_list:
            peers = ", ".join(f"#{p}" for p in entry["with"]) or "—"
            files = ", ".join(entry["files"]) if entry["files"] \
                else "shared files"
            overlaps_lines.append(f"- PR #{entry['number']} overlaps with "
                                  f"{peers} on: {files}")
    # No "none detected" placeholder: empty stays empty.

    runs = getf(data["runs"], "workflow_runs", "workflowRuns",
                default=[]) if data["runs"] else []
    failing_runs = []
    workflow_lines = []
    if data["runs_err"]:
        workflow_lines.append(
            f"Workflow run list unavailable: {data['runs_err']}")
    else:
        raw_failing = []
        if isinstance(runs, list):
            for run in runs:
                concl = getf(run, "conclusion")
                status = getf(run, "status")
                if concl == "failure" or (concl is None and status == "failure"):
                    raw_failing.append(run)
        for run in raw_failing[:5]:
            name = getf(run, "name", "workflow", "workflowName",
                        default="?")
            line = (f"- failing: {name} on "
                    f"{getf(run, 'branch', 'headBranch', 'head_branch', default='?')} "
                    f"({getf(run, 'event', default='?')})")
            run_id = getf(run, "id")
            failed_jobs = data["jobs"].get(str(run_id), []) \
                if run_id is not None else []
            failed_struct = [{"job": j, "steps": s}
                             for j, s in failed_jobs]
            if failed_jobs:
                causes = []
                for job_name, steps in failed_jobs:
                    cause = job_name
                    if steps:
                        cause += f" (steps: {', '.join(steps)})"
                    causes.append(cause)
                line += f" — failed: {'; '.join(causes)}"
            url = getf(run, "htmlUrl", "html_url", "url")
            if url:
                line += f" — {url}"
            workflow_lines.append(line)
            failing_runs.append({
                "name": name,
                "branch": getf(run, "branch", "headBranch", "head_branch",
                               default="?"),
                "event": getf(run, "event", default="?"),
                "url": url, "failed": failed_struct,
            })
        # No "no failing runs" placeholder: empty stays empty.

    for fr in failing_runs:
        run_name = fr.get("name", "workflow")
        branch_name = fr.get("branch", "main")
        event_name = fr.get("event", "push")
        failed_parts = []
        for j in fr.get("failed", []):
            jname = j.get("job", "")
            steps = j.get("steps", [])
            if steps:
                failed_parts.append(f"{jname} ({', '.join(steps)})")
            elif jname:
                failed_parts.append(jname)
        cause_summary = "; ".join(failed_parts) if failed_parts else "workflow failed"
        attention_text = f"Failing run: {run_name} on {branch_name} ({event_name}) — likely cause: {cause_summary}"
        marker = f"<!-- action:diagnose-run workflow:{run_name} branch:{branch_name} -->"
        attention.append((1, attention_text, marker))

    # Rank order, not PR-list order: conflicts (0) before CI / failing runs (1)
    # before reviews (2) before stale (3), regardless of which arrived first.
    attention_ranked = sorted(attention, key=lambda x: x[0])
    attention = [t[1] for t in attention_ranked]
    attention_with_markers = [(t[1], t[2] if len(t) > 2 else "")
                              for t in attention_ranked]

    enrichment_note = data.get("enrichment_note", "")
    enrichment_total = data.get("enrichment_total", len(prs))
    enrichment_shown = data.get("enrichment_shown", len(prs))
    enrichment_truncated = data.get(
        "enrichment_truncated",
        enrichment_total > data.get("enrichment_cap", ENRICHMENT_CAP))
    if enrichment_truncated and not enrichment_note:
        enrichment_note = (
            f"showing enrichment for {enrichment_shown} of "
            f"{enrichment_total} open PRs "
            f"(most-recently-updated first, "
            f"cap {data.get('enrichment_cap', ENRICHMENT_CAP)})")

    return {
        "repo": REPO_KEY,
        "attention": attention,
        "attention_with_markers": attention_with_markers,
        "prs": pr_infos,
        "prs_err": data["prs_err"],
        "enrichment_note": enrichment_note,
        "enrichment_total": enrichment_total,
        "enrichment_shown": enrichment_shown,
        "enrichment_truncated": enrichment_truncated,
        "reviews_lines": reviews_lines,
        "reviews_err": {str(k): v for k, v in reviews_err.items()},
        "board": board_items, "board_lines": board_lines,
        "board_err": data["board_err"],
        "conflicts": conflicts_list, "conflicts_lines": conflicts_lines,
        "conf_err": data["conf_err"],
        "overlaps": overlaps_list, "overlaps_lines": overlaps_lines,
        "failing_runs": failing_runs, "workflow_lines": workflow_lines,
        "runs_err": data["runs_err"],
    }


def build(data, analysis=None):
    """Return (briefing text, attention items, raw appendix text)."""
    a = analysis if analysis is not None else analyze(data)
    lines = [f"# Briefing {REPO_GH} {date.today().isoformat()}", ""]
    lines.append("## Needs attention")
    if a["attention"]:
        lines.extend(f"- {x}" for x in a["attention"])
    else:
        lines.append("Nothing needs attention.")
    lines.append("")
    lines.append(f"## Open PRs ({len(data['pr_list'])})")
    if a["prs_err"]:
        lines.append(f"Open PR list unavailable: {a['prs_err']}")
    elif a["prs"]:
        lines.extend(p["line"] for p in a["prs"])
        if a.get("enrichment_note"):
            lines.append(f"Note: {a['enrichment_note']}.")
    else:
        lines.append("No open PRs.")
    # analyze() leaves empty sections empty for the API (the app
    # condenses them); the markdown rendering keeps a (none) marker
    # so headings never dangle over a blank gap.
    def _or_none(lines):
        return lines if lines else ["(none)"]

    lines.append("")
    lines.append("## Reviews")
    lines.extend(_or_none(a["reviews_lines"]))
    lines.append("")
    lines.append("## Work board")
    lines.extend(_or_none(a["board_lines"]))
    lines.append("")
    lines.append("## Conflicts")
    lines.extend(_or_none(a["conflicts_lines"]))
    lines.append("")
    lines.append("## Overlaps")
    lines.extend(_or_none(a["overlaps_lines"]))
    lines.append("")
    lines.append("## Workflow health")
    lines.extend(_or_none(a["workflow_lines"]))
    lines.append("")

    raw_parts = []
    for name, key in (("pr-list", "prs_raw"), ("status-board", "board_raw"),
                      ("detect-conflicts", "conf_raw"),
                      ("workflow-runs", "runs_raw")):
        raw = data[key]
        if raw:
            if key == "runs_raw":
                raw = context_filter.extract_ci_error_traces(raw)
            cleaned = context_filter.enforce_context_budget(raw, max_tokens=2000)
            raw_parts.append(f"### {name}\n{cleaned[:RAW_CAP]}")
    if data["reviews_raw_parts"]:
        rev_combined = "\n".join(data["reviews_raw_parts"])
        rev_cleaned = context_filter.enforce_context_budget(rev_combined, max_tokens=2000)
        raw_parts.append(f"### read-pr-reviews\n{rev_cleaned[:RAW_CAP]}")
    raw_text = "\n\n".join(raw_parts)
    return "\n".join(lines), a["attention"], raw_text

def main(argv):
    global REPO_KEY, REPO_GH
    briefing = "--briefing" in argv
    briefing_json = "--briefing-json" in argv
    plan_dir = None
    if "--plan-dir" in argv:
        plan_dir = argv[argv.index("--plan-dir") + 1]
    if "--repo" in argv:
        key = argv[argv.index("--repo") + 1]
        if not NAME_RE.match(key):
            print(f"bad repo name: {key}", file=sys.stderr)
            return 2
        if "--gh" not in argv:
            print("--repo needs --gh (refusing to brief with the "
                  "default gh path)", file=sys.stderr)
            return 2
        REPO_KEY = key
    if "--gh" in argv:
        gh = argv[argv.index("--gh") + 1]
        if not GH_RE.match(gh):
            print(f"bad gh path: {gh}", file=sys.stderr)
            return 2
        REPO_GH = gh
        if "--repo" not in argv:
            REPO_KEY = gh.split("/")[1]
    if not briefing and not briefing_json and not plan_dir:
        print("usage: briefing_summary.py --briefing | --briefing-json | "
              "--plan-dir DIR [--repo NAME --gh OWNER/REPO]",
              file=sys.stderr)
        return 2
    data = fetch()
    analysis = analyze(data)
    if briefing_json:
        print(json.dumps(analysis))
        # Same exit rule as --briefing: 0 if a main source worked.
        return 0 if (data["pr_list"] or data["board"]) else 1
    text, attention, raw_text = build(data, analysis=analysis)
    if briefing:
        print(text)
        # Exit 0 if at least one main source worked; 1 if nothing did.
        return 0 if (data["pr_list"] or data["board"]) else 1
    os.makedirs(plan_dir, exist_ok=True)
    path = os.path.join(plan_dir, f"{date.today().isoformat()}-briefing-plan.md")
    attn_items = analysis.get("attention_with_markers") or [(it, "") for it in attention]
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n\n"):
            f.write("\n")
        f.write("## Next actions\n")
        if attn_items:
            for item, marker in attn_items:
                marker_str = f" {marker}" if marker else ""
                f.write(f"- [ ] {item}{marker_str}\n")
        else:
            f.write("- [ ] No action needed — re-run the briefing "
                    "after the next change\n")
        f.write("- [ ] Re-run the briefing after acting <!-- action:briefing-td -->\n")
        f.write("\n## Raw output\n\n")
        f.write(raw_text)
        f.write("\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

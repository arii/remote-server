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
  td --json gh search-prs --state open
      PRSummary: number, title, author, headRefName, baseRefName,
      isDraft, mergeStateStatus, updatedAt, url. NOTE: no CI state,
      no reviews, no changed files — that is why gh enrichment exists.
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
  td --json agent list-sessions   Jules sessions. Needs JULES_API_KEY
      in the terminal environment; any failure degrades to an
      unavailable note. Sessions are repo-scoped defensively: only
      sessions whose source context names the repo are listed as such;
      others are counted, never claimed.

Attention ranking (first match wins per PR, in this order):
  conflicted > CI failing > changes requested > overlaps > stale (>30 days).
  A changes-requested item now quotes the review excerpt, so the
  action names what to address instead of just who objected.
REVIEW_REQUIRED is deliberately NOT attention: in a solo repo most
open PRs sit at REVIEW_REQUIRED, so flagging them would make every
briefing shout. Drafts are listed, never attention items.
"""

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone

try:
    from buttons import context_filter
except ImportError:
    import context_filter

STALE_DAYS = 30
RAW_CAP = 20_000  # raw appendix cap, chars

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


def run_json(cmd, timeout):
    """Run cmd, return (parsed JSON or None, raw stdout, error str)."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout,
                              text=True)
    except Exception as exc:
        return None, "", str(exc)
    raw = proc.stdout or ""
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        if not err:
            # td sometimes reports the failure on stdout instead.
            out = raw.strip().splitlines()
            err = [out[-1]] if out else []
        return None, raw, (err[-1][:160] if err
                           else f"exit {proc.returncode}")
    try:
        return json.loads(raw), raw, ""
    except Exception:
        return None, raw, "output was not JSON"


def fetch_pr_diff(pr_number: int, max_file_lines: int = 200) -> str:
    """Fetch PR diff via td and pre-filter using context_filter.filter_diff()."""
    data, _, _ = run_json(
        ["td", "--json", "gh", "pr-diff", "--pr", str(pr_number)], 30)
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
    text = " ".join(str(body or "").split())
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


def _session_repo(session):
    """An 'owner/repo', or None, from a Jules session object —
    checked across the field spellings the Jules API and td's
    client use. A display name mentioning this run's repo name
    maps to this run's gh path."""
    if not isinstance(session, dict):
        return None
    candidates = []
    ctx = session.get("sourceContext") or session.get("source_context") or {}
    if isinstance(ctx, dict):
        repo_ctx = ctx.get("githubRepoContext") or ctx.get("github_repo_context") or {}
        if isinstance(repo_ctx, dict):
            candidates.append(repo_ctx)
        candidates.append(ctx)
    src = session.get("source") or {}
    if isinstance(src, dict):
        candidates.append(src.get("githubRepo") or src.get("github_repo") or {})
        candidates.append(src)
    candidates.append(session)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        owner = cand.get("owner")
        repo = cand.get("repo")
        if owner and repo:
            return f"{owner}/{repo}"
        full = cand.get("full_name") or cand.get("fullName")
        if isinstance(full, str) and "/" in full:
            return full
    return None


def _session_title(session):
    title = getf(session, "title", default="") or ""
    if title:
        return _excerpt(title, 100)
    prompt = getf(session, "prompt", default="") or ""
    if prompt:
        first = str(prompt).strip().splitlines()
        if first:
            return _excerpt(first[0], 100)
    name = str(getf(session, "name", default="") or "")
    return name.split("/")[-1] if name else "Jules session"


TERMINAL_SESSION_STATES = {"SUCCEEDED", "COMPLETED", "FAILED",
                           "CANCELLED", "TERMINATED"}


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


def fetch():
    """Gather everything; each source degrades independently."""
    data = {}
    data["prs"], data["prs_raw"], data["prs_err"] = run_json(
        ["td", "--json", "gh", "search-prs", "--state", "open"], 60)
    data["board"], data["board_raw"], data["board_err"] = run_json(
        ["td", "--json", "gh", "status-board"], 60)
    data["conf"], data["conf_raw"], data["conf_err"] = run_json(
        ["td", "--json", "gh", "detect-conflicts", "--all"], 120)
    data["runs"], data["runs_raw"], data["runs_err"] = run_json(
        ["td", "--json", "repo", "workflow-runs"], 60)
    prs = getf(data["prs"], "prs", default=[]) if data["prs"] else []
    data["pr_list"] = prs if isinstance(prs, list) else []
    # gh enrichment, one call per open PR (counts here are small).
    data["gh"] = {}
    for pr in data["pr_list"]:
        num = getf(pr, "number")
        if not isinstance(num, int):
            continue
        view, _, _ = run_json([
            "gh", "pr", "view", str(num), "--json",
            "number,title,headRefName,mergeable,mergeStateStatus,"
            "reviewDecision,statusCheckRollup,updatedAt,author,isDraft,url",
        ], 30)
        if view:
            data["gh"][num] = view
    # Review substance, one td call per open PR (capped: review
    # calls are the slowest part and PR counts here are small).
    data["reviews"] = {}
    data["reviews_err"] = {}
    data["reviews_raw_parts"] = []
    for pr in data["pr_list"][:10]:
        num = getf(pr, "number")
        if not isinstance(num, int):
            continue
        payload, raw, err = run_json(
            ["td", "--json", "gh", "read-pr-reviews", "--pr", str(num)],
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
    # Jules sessions (background agent work). One canonical command path:
    # td --json agent list-sessions.
    data["jules"] = None
    data["jules_err"] = ""
    data["jules_raw"] = ""
    payload, raw, err = run_json(
        ["td", "--json", "agent", "list-sessions"], 60)
    if payload is not None:
        found = _deep_list(payload, "sessions")
        if isinstance(found, list):
            data["jules"] = found
            data["jules_raw"] = raw
        else:
            data["jules_err"] = "list-sessions returned no sessions list"
    else:
        data["jules_err"] = err
    # A td without the agent group (e.g. the pinned
    # cli-v0.14.2) fails with Click's "No such command" — the
    # raw wording reads as a page bug, so say what it means
    # and what fixes it instead.
    if data["jules"] is None and "No such command" in (data["jules_err"] or ""):
        data["jules_err"] = ("this td version can't list Jules sessions — "
                             "rebuild the terminal for a newer td")
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
        shown_any = False
        for pr in prs:
            num = getf(pr, "number")
            if not isinstance(num, int) or num not in reviews:
                continue
            items = reviews[num]
            if not items:
                continue
            shown_any = True
            picked = []
            for want in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED"):
                for user, state, body, _s in items:
                    if state == want:
                        picked.append((user, state, body))
                        break
                if len(picked) >= 2:
                    break
            for user, state, body in picked:
                line = f"- #{num}: @{user} {state}"
                if body:
                    line += f" — '{body}'"
                reviews_lines.append(line)
            states = sorted({state for _u, state, _b, _s in items})
            reviews_lines.append(f"  ({len(items)} review(s): "
                                 f"{', '.join(states).lower() or 'none'})")
        if not shown_any:
            reviews_lines.append("No reviews on open PRs.")
    elif reviews_err:
        first_err = next(iter(reviews_err.values()), "")
        reviews_lines.append(f"Reviews unavailable: {first_err}")
    else:
        reviews_lines.append("No reviews fetched (no open PRs).")
    if reviews_err:
        for num, err in sorted(reviews_err.items()):
            reviews_lines.append(f"Reviews unavailable for #{num}: {err}")

    board = getf(data["board"], "work", default=[]) if data["board"] else []
    board_items = []
    board_lines = []
    if data["board_err"]:
        board_lines.append(f"Work board unavailable: {data['board_err']}")
    elif isinstance(board, list) and board:
        for item in board:
            board_items.append({
                "branch": getf(item, "branch", default="?"),
                "issue": getf(item, "issue", default="?"),
                "status": getf(item, "status", default="?"),
            })
            board_lines.append(f"- {getf(item, 'branch', default='?')} "
                               f"— issue {getf(item, 'issue', default='?')} "
                               f"— {getf(item, 'status', default='?')}")
    else:
        board_lines.append("No work-board items.")

    conflicts_list = [{"number": p["number"], "merge": "dirty"}
                      for p in pr_infos if p["conflicted"]]
    conflicts_lines = []
    if conflicts_list:
        for entry in conflicts_list:
            conflicts_lines.append(f"- PR #{entry['number']} (merge: dirty)")
    else:
        conflicts_lines.append("None detected.")
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
    else:
        overlaps_lines.append("None detected.")

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
        if not raw_failing:
            workflow_lines.append("No failing runs in the recent list.")

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

    sessions = data["jules"]
    jules_items = []
    jules_other = 0
    jules_unscoped = 0
    jules_lines = []
    if sessions is None:
        jules_lines.append(f"Jules sessions unavailable: {data['jules_err']}")
    else:
        matched = []
        seen_session_ids = set()
        for session in sessions:
            if not isinstance(session, dict):
                continue
            sid = getf(session, "id", "sessionId", "session_id", "name")
            if sid:
                if sid in seen_session_ids:
                    continue
                seen_session_ids.add(sid)
            state = str(getf(session, "state", default="") or "").upper()
            if state in TERMINAL_SESSION_STATES:
                continue
            repo = _session_repo(session)
            title = _session_title(session)
            created = str(getf(session, "createTime", "create_time",
                               default="") or "")[:10]
            item = f"- {title} — {state.lower()}"
            if created:
                item += f" — {created}"
            if repo == REPO_GH or (repo and (repo == REPO_KEY or repo.endswith("/" + REPO_KEY))):
                matched.append(item)
            else:
                jules_other += 1
        if matched:
            jules_lines.extend(matched)
        else:
            jules_lines.append(
                f"No active Jules sessions for {REPO_KEY}.")
        if jules_other:
            jules_lines.append(f"({jules_other} active session(s) for other "
                               "repos, not listed.)")
        if jules_unscoped:
            jules_lines.append(f"({jules_unscoped} active session(s) above "
                               "carry no repo info — listed unscoped, not "
                               "claimed.)")
        jules_items = matched

    return {
        "repo": REPO_KEY,
        "attention": attention,
        "attention_with_markers": attention_with_markers,
        "prs": pr_infos,
        "prs_err": data["prs_err"], "reviews_lines": reviews_lines,
        "reviews_err": {str(k): v for k, v in reviews_err.items()},
        "board": board_items, "board_lines": board_lines,
        "board_err": data["board_err"],
        "conflicts": conflicts_list, "conflicts_lines": conflicts_lines,
        "conf_err": data["conf_err"],
        "overlaps": overlaps_list, "overlaps_lines": overlaps_lines,
        "failing_runs": failing_runs, "workflow_lines": workflow_lines,
        "runs_err": data["runs_err"],
        "jules": {"items": jules_items, "other": jules_other,
                  "unscoped": jules_unscoped,
                  "unavailable": data["jules_err"] if sessions is None else None},
        "jules_lines": jules_lines,
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
    else:
        lines.append("No open PRs.")
    lines.append("")
    lines.append("## Reviews")
    lines.extend(a["reviews_lines"])
    lines.append("")
    lines.append("## Work board")
    lines.extend(a["board_lines"])
    lines.append("")
    lines.append("## Conflicts")
    lines.extend(a["conflicts_lines"])
    lines.append("")
    lines.append("## Overlaps")
    lines.extend(a["overlaps_lines"])
    lines.append("")
    lines.append("## Workflow health")
    lines.extend(a["workflow_lines"])
    lines.append("")
    lines.append("## Agent sessions (Jules)")
    lines.extend(a["jules_lines"])
    lines.append("")

    raw_parts = []
    for name, key in (("search-prs", "prs_raw"), ("status-board", "board_raw"),
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
    if data["jules_raw"]:
        jules_cleaned = context_filter.enforce_context_budget(data["jules_raw"], max_tokens=2000)
        raw_parts.append(f"### agent list-sessions\n{jules_cleaned[:RAW_CAP]}")
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

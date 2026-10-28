#!/usr/bin/env python3
"""briefing_summary — readable repo briefing for the S5 buttons page.

Why this lives here, not in td: td has no briefing summarization
that agentic tools can call, and its output modes both fail a human
reader — the CLI defaults to JSON (machine consumption), while human
mode for search-prs only prints "Found N PRs" with no per-PR detail.
So the buttons side parses td's JSON plus `gh` enrichment and prints
a briefing a person can act on from a phone.

Scope: tech-dancer only. The caller (buttons/server.py, via the TD
prefix) runs this with cwd = the tech-dancer checkout and .env
sourced, so `td` and `gh` (authed in the terminal container) work.

Modes:
  --briefing          print the readable briefing to stdout
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
  td --json gh detect-conflicts --all   conflict scan (shape varies;
      parsed defensively, see _conflicted_prs)
  td --json repo workflow-runs   recent runs; failures only shown.

Attention ranking (first match wins per PR, in this order):
  conflicted > CI failing > changes requested > stale (>30 days).
REVIEW_REQUIRED is deliberately NOT attention: in a solo repo most
open PRs sit at REVIEW_REQUIRED, so flagging them would make every
briefing shout. Drafts are listed, never attention items.
"""

import json
import subprocess
import sys
from datetime import date, datetime, timezone

STALE_DAYS = 30
RAW_CAP = 20_000  # raw appendix cap, chars
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
        return None, raw, err[-1] if err else f"exit {proc.returncode}"
    try:
        return json.loads(raw), raw, ""
    except Exception:
        return None, raw, "output was not JSON"


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


def _conflicted_prs(node, out, in_conf=False):
    """Collect {number: files} from a detect-conflicts payload of
    unknown shape. A dict counts when it pairs a PR number (or a
    'prs' list of PR numbers, as td gh detect-conflicts --all emits)
    with a truthy conflict indicator (a files list, hasConflicts) or
    when it sits inside a conflicts list — e.g. {"conflicts": [{"prs":
    [4814, 4826], "files": ["package.json"]}]}. Unknown shapes yield
    nothing rather than a guessed conflict — mergeStateStatus still
    covers it."""
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
                existing = out.setdefault(n, [])
                for f in files:
                    if f not in existing:
                        existing.append(f)
        for key, val in node.items():
            _conflicted_prs(val, out,
                            in_conf or key in ("conflicts", "conflictedFiles",
                                               "conflicted_files"))
    elif isinstance(node, list):
        for item in node:
            _conflicted_prs(item, out, in_conf)


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
    return data


def build(data):
    """Return (briefing text, attention items, raw appendix text)."""
    prs = data["pr_list"]
    gh = data["gh"]
    conflicted = {}
    if data["conf"]:
        _conflicted_prs(data["conf"], conflicted)

    lines = [f"# Briefing {date.today().isoformat()}", ""]
    attention = []
    pr_lines = []
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
        is_conflict = (isinstance(num, int) and num in conflicted) \
            or merge == "dirty"
        review = getf(view, "reviewDecision", "review_decision")
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
        if draft:
            detail += " — draft"
        pr_lines.append(f"- {label} {detail}")
        if draft:
            continue
        if is_conflict:
            attention.append((0, f"{label} — conflicted with base"))
        elif ci == "failing":
            attention.append((1, f"{label} — CI failing"
                             + (f": {', '.join(failing[:3])}" if failing else "")))
        elif review == "CHANGES_REQUESTED":
            attention.append((2, f"{label} — changes requested"))
        elif stale:
            attention.append((3, f"{label} — stale (updated {updated_day})"))
    # Rank order, not PR-list order: conflicts before CI before
    # reviews before stale, regardless of which PR came first.
    attention = [text for _, text in sorted(attention)]

    lines.append("## Needs attention")
    if attention:
        lines.extend(f"- {a}" for a in attention)
    else:
        lines.append("Nothing needs attention.")
    lines.append("")

    lines.append(f"## Open PRs ({len(prs)})")
    if data["prs_err"]:
        lines.append(f"Open PR list unavailable: {data['prs_err']}")
    elif pr_lines:
        lines.extend(pr_lines)
    else:
        lines.append("No open PRs.")
    lines.append("")

    board = getf(data["board"], "work", default=[]) if data["board"] else []
    lines.append("## Work board")
    if data["board_err"]:
        lines.append(f"Work board unavailable: {data['board_err']}")
    elif isinstance(board, list) and board:
        for item in board:
            lines.append(f"- {getf(item, 'branch', default='?')} "
                         f"— issue {getf(item, 'issue', default='?')} "
                         f"— {getf(item, 'status', default='?')}")
    else:
        lines.append("No work-board items.")
    lines.append("")

    lines.append("## Conflicts")
    if data["conf_err"]:
        lines.append(f"Conflict scan unavailable: {data['conf_err']}")
    elif conflicted:
        for num, files in sorted(conflicted.items()):
            lines.append(f"- PR #{num}"
                         + (f": {', '.join(files)}" if files else ""))
    else:
        lines.append("None detected.")
    lines.append("")

    runs = getf(data["runs"], "workflow_runs", "workflowRuns",
                default=[]) if data["runs"] else []
    lines.append("## Workflow health")
    if data["runs_err"]:
        lines.append(f"Workflow run list unavailable: {data['runs_err']}")
    else:
        failing_runs = []
        if isinstance(runs, list):
            for run in runs:
                concl = getf(run, "conclusion")
                status = getf(run, "status")
                if concl == "failure" or (concl is None and status == "failure"):
                    failing_runs.append(run)
        if failing_runs:
            for run in failing_runs[:5]:
                name = getf(run, "name", "workflow", "workflowName",
                            default="?")
                lines.append(f"- failing: {name} on "
                             f"{getf(run, 'branch', 'headBranch', 'head_branch', default='?')} "
                             f"({getf(run, 'event', default='?')})"
                             + (f" — {getf(run, 'htmlUrl', 'html_url', 'url')}"
                                if getf(run, "htmlUrl", "html_url", "url") else ""))
        else:
            lines.append("No failing runs in the recent list.")
    lines.append("")

    raw_parts = []
    for name, key in (("search-prs", "prs_raw"), ("status-board", "board_raw"),
                      ("detect-conflicts", "conf_raw"),
                      ("workflow-runs", "runs_raw")):
        raw = data[key]
        if raw:
            raw_parts.append(f"### {name}\n{raw[:RAW_CAP]}")
    raw_text = "\n\n".join(raw_parts)
    return "\n".join(lines), attention, raw_text


def main(argv):
    briefing = "--briefing" in argv
    plan_dir = None
    if "--plan-dir" in argv:
        plan_dir = argv[argv.index("--plan-dir") + 1]
    if not briefing and not plan_dir:
        print("usage: briefing_summary.py --briefing | --plan-dir DIR",
              file=sys.stderr)
        return 2
    data = fetch()
    text, attention, raw_text = build(data)
    if briefing:
        print(text)
        # Exit 0 if at least one main source worked; 1 if nothing did.
        return 0 if (data["pr_list"] or data["board"]) else 1
    import os
    os.makedirs(plan_dir, exist_ok=True)
    path = os.path.join(plan_dir, f"{date.today().isoformat()}-briefing-plan.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n\n"):
            f.write("\n")
        f.write("## Next actions\n")
        if attention:
            for item in attention:
                f.write(f"- [ ] {item}\n")
        else:
            f.write("- [ ] No action needed — re-run the briefing "
                    "after the next change\n")
        f.write("- [ ] Re-run the briefing after acting\n")
        f.write("\n## Raw output\n\n")
        f.write(raw_text)
        f.write("\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

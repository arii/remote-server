#!/usr/bin/env python3
"""review — local-model PR review: review plans + Ollama/Nebius diff reviews.

Why this lives here, not in td: td's review tooling is
deterministic-only — `td ai review` scores heuristics plus the CI
verdict and always returns ``findings: []``, `td gh audit-pr` runs an
antipattern audit (verified against boomtick_cli 0.19.1) — and the
Gemini reviewer is paused to save credits. This wraps td's own
fetch/post primitives with a local-LLM synthesis path humans can run
pre-push and lanes/auditors can run anytime, at zero marginal cost.

Pipeline:
  1. fetch: `td --json gh pr-diff` (files + diff) and
     `td --json gh pr-view` (title/state, best-effort) — td
     primitives, never raw gh.
  2. plan: per-file entries (risk prompts, test-coverage check,
     scope-vs-title inputs).
  3. synthesize: deep_chat.build_model(prefer="ollama") with a
     prefer="nebius" fallback; s5tools context reads when a local
     checkout exists.
  4. output: findings JSON [{file, line, severity, claim,
     suggestion}] plus a markdown body with the Gemini review
     sections (Summary / Findings / Verdict).
  5. --post only: `td gh post-comment --body` with the markdown
     body (chunked when over the comment size cap).

Modes:
  --dry-run   print the review plan as JSON; no model call, no post.
  --json      print the findings envelope instead of markdown.
  --post      post the markdown body to the PR. Explicit human flag
              only — the default prints and never posts.
  --prefer P  ollama (default, nebius fallback) or nebius (no fallback).

Stdlib-only at import: deep-agent modules (deep_chat, s5tools) import
lazily inside the run path, so --dry-run and the hermetic tests work
under system python without the venv.
"""

import argparse
import json
import os
import re
import subprocess
import sys

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SEVERITIES = ("info", "minor", "major", "critical")
TD_TIMEOUT = 60
BODY_CHUNK_LIMIT = 60000
DIFF_PROMPT_CAP = 24000
CONTEXT_CAP = 6000
MAX_FINDINGS = 50


class ReviewError(Exception):
    """Expected failure (fetch/model/post); main() renders it."""


def valid_repo(repo):
    """True for owner/name strings safe to pass to td."""
    return isinstance(repo, str) and bool(REPO_RE.match(repo))


def _short(exc, limit=200):
    text = str(exc)
    return text if len(text) <= limit else text[:limit] + "…"


def _td_cwd(repo):
    """cwd for td subprocesses: the target checkout when present.

    td discovers project_config.json upward from cwd, but the engine
    runs -m steps from ENGINE_ROOT (/app), which has no config
    (#925). Aiming td at the target checkout keeps ENGINE_ROOT
    module resolution AND checkout config discovery. Falls back to
    None (inherit cwd) when the checkout is absent, so human runs
    from a dev checkout keep working. Mirrors the jobs router td_cwd
    mapping (backend/api/routers/jobs.py::_repo_target) and
    s5tools.checkout_for.
    """
    name = repo.split("/", 1)[1] if "/" in repo else repo
    checkout = ("/srv/remote-server" if name == "remote-server"
                else f"/srv/repos/{name}")
    return checkout if os.path.isdir(checkout) else None


def _td_json(argv, timeout=TD_TIMEOUT, cwd=None):
    """Run td --json; return the merged payload dict or raise ReviewError."""
    try:
        proc = subprocess.run(
            ["td", "--json", *argv],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True, cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        raise ReviewError(f"td {' '.join(argv)} timed out after {timeout}s")
    except OSError as exc:
        raise ReviewError(f"td exec failed: {_short(exc)}")
    try:
        payload = json.loads(proc.stdout or "")
    except Exception:
        detail = (proc.stderr or "").strip().splitlines()
        detail = detail[-1][:160] if detail else f"exit {proc.returncode}"
        raise ReviewError(f"td {' '.join(argv)}: bad JSON ({detail})")
    if not isinstance(payload, dict) or payload.get("status") != "success":
        msg = payload.get("message", "") if isinstance(payload, dict) else ""
        raise ReviewError(f"td {' '.join(argv)}: {msg[:160] or 'failed'}")
    return payload


def fetch_pr_diff(repo, pr):
    """(files, diff_text, truncated) via td's diff primitive."""
    payload = _td_json(["gh", "pr-diff", "--pr", str(pr), "--repo", repo],
                       cwd=_td_cwd(repo))
    files = payload.get("files")
    files = [f for f in files if isinstance(f, dict)] \
        if isinstance(files, list) else []
    return files, str(payload.get("diff_text") or ""), \
        bool(payload.get("is_truncated"))


def fetch_pr_view(repo, pr):
    """PR title/state snapshot via td; {} when unavailable (best-effort)."""
    try:
        return _td_json(["gh", "pr-view", "--pr", str(pr), "--repo", repo],
                        cwd=_td_cwd(repo))
    except ReviewError as exc:
        print(f"review: pr-view unavailable ({exc}); continuing", file=sys.stderr)
        return {}


def _split_diff(diff_text):
    """[(path, chunk)] split on diff --git headers. Defensive: unparseable
    input yields one ("", whole) entry rather than nothing."""
    if not diff_text.strip():
        return []
    parts = re.split(r"(?m)^diff --git a/\S+ b/(\S+)\s*$", diff_text)
    # parts: [preamble, path1, chunk1, path2, chunk2, ...]
    out = []
    for i in range(1, len(parts) - 1, 2):
        out.append((parts[i], parts[i + 1]))
    return out or [("", diff_text)]


def _is_test_path(path):
    name = path.rsplit("/", 1)[-1].lower()
    lower = path.lower()
    return (name.startswith("test_") or name.endswith("_test.py")
            or name.endswith(".test.ts") or name.endswith(".test.tsx")
            or "/test/" in lower or "/tests/" in lower
            or lower.startswith("test/") or lower.startswith("tests/"))


def _risk_prompts(path):
    """Deterministic per-file review prompts: generic trio plus up to
    two prompts matched on the file kind."""
    prompts = [
        "Does each hunk do what the PR title claims, nothing more?",
        "Are errors handled and edge cases covered (empty/None/malformed)?",
        "Is the changed behavior covered by a test in this PR?",
    ]
    lower = path.lower()
    if lower.endswith(".py"):
        prompts.append("Any unhandled exceptions, mutable default args, "
                       "or import-time side effects?")
    elif lower.endswith((".ts", ".tsx", ".js", ".jsx")):
        prompts.append("Any unhandled promise rejections, missing null "
                       "checks, or useEffect dependency bugs?")
    if ".github/workflows/" in lower or lower.endswith((".yml", ".yaml")):
        prompts.append("Does the job fail closed? Are secrets referenced, "
                       "never printed?")
    if lower.endswith(".md"):
        prompts.append("Do the referenced paths, commands, and flags "
                       "still exist?")
    if _is_test_path(path):
        prompts.append("Would these assertions fail without the fix "
                       "(not tautologies)?")
    return prompts[:5]


def build_plan(repo, pr, meta, files, diff_text):
    """Review plan: per-file risk prompts plus the plan-level
    test-coverage and scope-vs-title inputs the model judges."""
    title = str(meta.get("title") or "(unknown)")
    entries = []
    for f in files:
        path = str(f.get("path") or "")
        if not path:
            continue
        entries.append({
            "path": path,
            "status": str(f.get("status") or "modified"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
            "risk_prompts": _risk_prompts(path),
        })
    test_files = sorted({e["path"] for e in entries
                         if _is_test_path(e["path"])})
    uncovered = sorted({e["path"] for e in entries
                        if not _is_test_path(e["path"])})
    top_dirs = sorted({e["path"].split("/", 1)[0] for e in entries
                       if "/" in e["path"]})
    return {
        "repo": repo,
        "pr": pr,
        "title": title,
        "state": str(meta.get("state") or "UNKNOWN"),
        "model": {"preferred": "ollama", "fallback": "nebius"},
        "scope": {"file_count": len(entries), "top_dirs": top_dirs,
                  "title": title},
        "test_coverage": {"tests_changed": bool(test_files),
                          "test_files": test_files,
                          "uncovered_src": uncovered if not test_files else []},
        "diff_truncated": bool(meta.get("diff_truncated", False)),
        "files": entries,
    }


def gather_context(repo, paths, limit=CONTEXT_CAP):
    """Best-effort repo context via s5tools (list_dir/grep_repo).

    Returns "" when there is no local checkout or s5tools is
    unavailable — the review still runs on the diff alone.
    """
    try:
        import s5tools
    except Exception as exc:
        print(f"review: s5tools unavailable ({_short(exc)}); "
              "skipping context reads", file=sys.stderr)
        return ""
    try:
        checkout = s5tools.checkout_for(repo)
    except Exception:
        return ""
    if not os.path.isdir(checkout):
        return ""
    chunks = []
    try:
        chunks.append("top-level:\n" + s5tools.list_dir(checkout, ""))
        for path in paths[:5]:
            base = os.path.basename(path)
            stem = os.path.splitext(base)[0]
            if stem and not stem.startswith("test"):
                hit = s5tools.grep_repo(checkout, stem)
                chunks.append(f"grep {stem}:\n{hit}")
    except Exception as exc:
        print(f"review: context read failed ({_short(exc)})", file=sys.stderr)
    return "\n\n".join(chunks)[:limit]


def _default_model_factory(prefer):
    """(model, spec) via deep_chat.build_model. Venv-only at call time."""
    import deep_chat
    return deep_chat.build_model(prefer=prefer)


def _review_prompt(plan, diff_text, context):
    files = "\n".join(
        f"- {e['path']} ({e['status']}"
        f"{', +' + str(e['additions']) if e['additions'] is not None else ''}"
        f"{', -' + str(e['deletions']) if e['deletions'] is not None else ''})"
        for e in plan["files"][:40])
    cov = plan["test_coverage"]
    cov_line = ("tests changed: " + ", ".join(cov["test_files"])
                if cov["tests_changed"]
                else "NO test files changed in this PR")
    return f"""You are reviewing PR #{plan['pr']} ({plan['repo']}): {plan['title']}.
State: {plan['state']}. Scope: {plan['scope']['file_count']} files \
({', '.join(plan['scope']['top_dirs']) or 'repo root'}). {cov_line}.

Changed files:
{files or '(none listed)'}

Risk prompts per file:
{json.dumps({e['path']: e['risk_prompts'] for e in plan['files'][:40]}, indent=1)}

Repo context (may be empty):
{context or '(no local checkout)'}

Diff (may be truncated):
{diff_text[:DIFF_PROMPT_CAP] or '(empty diff)'}

Reply with JSON ONLY, no prose, matching this schema:
{{"summary": "2-3 sentences: what the PR does and overall shape",
  "findings": [{{"file": "path", "line": 0, "severity": "info|minor|major|critical",
                "claim": "what is wrong", "suggestion": "how to fix"}}],
  "verdict": "Looks good" or "Needs attention" plus one clause why}}
Rules: every finding needs file + claim; line is the new-side line number \
or 0 when it spans the file; severity info < minor < major < critical; \
empty findings [] when there is nothing concrete; verdict "Needs attention" \
when any finding is major or critical."""


def _extract_json(text):
    """Parsed JSON from model text: whole blob, fenced block, or the
    outermost {...}/[...] span. Returns None when nothing parses."""
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",
                      text, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    return None


def normalize_findings(raw, known_paths):
    """Findings into [{file, line, severity, claim, suggestion}].

    Drops entries without a file or claim; unknown severities become
    info; unparseable lines become None. Never raises on odd input.
    """
    items = raw.get("findings") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        path = item.get("file")
        claim = item.get("claim")
        if not isinstance(path, str) or not path.strip():
            continue
        if not isinstance(claim, str) or not claim.strip():
            continue
        line = item.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            line = None
        severity = item.get("severity")
        severity = severity.strip().lower() \
            if isinstance(severity, str) else ""
        if severity not in SEVERITIES:
            severity = "info"
        suggestion = item.get("suggestion")
        suggestion = suggestion.strip() if isinstance(suggestion, str) else ""
        out.append({"file": path.strip(), "line": line,
                    "severity": severity, "claim": claim.strip()[:500],
                    "suggestion": suggestion[:500]})
    return out


def _response_text(resp):
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def synthesize(plan, diff_text, context="", prefer="ollama",
               model_factory=None):
    """(findings, summary, verdict, model_label) from the local model.

    prefer="ollama" tries Ollama then falls back to Nebius;
    prefer="nebius" is a single cloud leg. model_factory(name) is the
    hermetic seam (default: deep_chat.build_model); it returns
    (model, spec) where model.invoke(str) answers.
    """
    factory = model_factory or _default_model_factory
    order = ["ollama", "nebius"] if prefer == "ollama" else ["nebius"]
    model, spec, last_exc, falled_back = None, None, None, False
    for name in order:
        try:
            model, spec = factory(name)
            break
        except Exception as exc:
            last_exc = exc
            falled_back = True
    if model is None:
        raise ReviewError(f"no model available ({'/'.join(order)}): "
                          f"{_short(last_exc)}")
    label = f"{spec.get('provider', '?')}:{spec.get('model', '?')}"
    if falled_back:
        label += " (fallback)"
        print(f"review: ollama unavailable ({_short(last_exc)}); "
              f"using {spec.get('provider')}", file=sys.stderr)
    prompt = _review_prompt(plan, diff_text, context)
    try:
        raw = _extract_json(_response_text(model.invoke(prompt)))
    except Exception as exc:
        raise ReviewError(f"model invoke failed ({label}): {_short(exc)}")
    known = [e["path"] for e in plan["files"]]
    findings = normalize_findings(raw, known)
    summary = raw.get("summary") if isinstance(raw, dict) else None
    summary = summary.strip() if isinstance(summary, str) and summary.strip() \
        else (f"{len(findings)} finding(s) across "
              f"{len({f['file'] for f in findings})} file(s).")
    verdict = raw.get("verdict") if isinstance(raw, dict) else None
    verdict = verdict.strip() if isinstance(verdict, str) and verdict.strip() \
        else ("Needs attention" if any(f["severity"] in ("major", "critical")
                                       for f in findings)
              else "Looks good")
    return findings, summary[:800], verdict[:300], label


def render_markdown(repo, pr, meta, findings, summary, verdict, model_label):
    """Markdown body with the Gemini review sections."""
    title = str(meta.get("title") or "(unknown)")
    lines = [f"# Review: #{pr} — {title}", "",
             "## Summary", summary, "",
             "## Findings"]
    if not findings:
        lines.append("No findings.")
    else:
        for f in findings:
            where = f"`{f['file']}`" + (f":{f['line']}" if f["line"] else "")
            lines.append(f"- **{f['severity']}** {where} — {f['claim']}")
            if f["suggestion"]:
                lines.append(f"  - Suggestion: {f['suggestion']}")
    lines += ["", "## Verdict", verdict, "",
              f"_Local review of {repo}#{pr} via {model_label}; "
              "heuristic plan + local LLM synthesis, not the Gemini reviewer._"]
    return "\n".join(lines) + "\n"


def split_body(body, limit=BODY_CHUNK_LIMIT):
    """Split body into chunks of at most `limit` chars.

    Cuts at the last blank line within each window when there is
    one, else hard-splits. A body that fits returns unchanged as one
    chunk. The chunks partition the input: "".join() of them always
    reconstructs it exactly.
    """
    if len(body) <= limit:
        return [body]
    chunks, start, n = [], 0, len(body)
    while start < n:
        if n - start <= limit:
            chunks.append(body[start:])
            break
        cut = body.rfind("\n\n", start, start + limit - 1)
        if cut == -1:
            chunks.append(body[start:start + limit])
            start += limit
        else:
            chunks.append(body[start:cut + 2])
            start = cut + 2
    return chunks


def post_body(repo, pr, body):
    """Post the markdown body via td's --body flag. Raises ReviewError
    on failure. Called only for an explicit --post.

    --body, not --file: td resolves --file through sanitize_path,
    which rewrites any absolute path outside td's cwd to its bare
    basename, so every temp-file post failed with "File not found"
    (#899). Bodies over BODY_CHUNK_LIMIT (GitHub caps comments at
    65536 chars) post as numbered part-comments instead.
    """
    if not body or not body.strip():
        raise ReviewError("refusing to post an empty body")
    parts = split_body(body)
    total = len(parts)
    for index, part in enumerate(parts, 1):
        chunk = (part if total == 1 else
                 f"{part.rstrip()}\n\n_(review part {index} of {total})_\n")
        argv = ["td", "gh", "post-comment", "--pr", str(pr),
               "--repo", repo, "--body", chunk]
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=TD_TIMEOUT,
                                  text=True, cwd=_td_cwd(repo))
        except subprocess.TimeoutExpired:
            raise ReviewError(f"td post-comment timed out after {TD_TIMEOUT}s")
        except OSError as exc:
            raise ReviewError(f"td exec failed: {_short(exc)}")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            if len(detail) > 2000:
                detail = detail[:2000] + "…"
            raise ReviewError(
                f"td post-comment: {detail or f'exit {proc.returncode}'}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.review",
        description="Local-model PR review: review plan + Ollama/Nebius "
                    "diff review. Prints; posts only with --post.")
    parser.add_argument("--repo", required=True,
                        help="Target repo as owner/name.")
    parser.add_argument("--pr", required=True, type=int,
                        help="PR number to review.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the review plan as JSON; no model, no post.")
    parser.add_argument("--json", action="store_true",
                        help="Print the findings envelope instead of markdown.")
    parser.add_argument("--post", action="store_true",
                        help="Post the markdown body to the PR (explicit "
                             "human flag; default prints only).")
    parser.add_argument("--prefer", default="ollama",
                        choices=["ollama", "nebius"],
                        help="ollama (default, nebius fallback) or nebius.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(
        sys.argv[1:] if argv is None else argv)
    if not valid_repo(args.repo):
        print(f"review: invalid --repo {args.repo!r}: want owner/name",
              file=sys.stderr)
        return 2
    if args.pr <= 0:
        print(f"review: invalid --pr {args.pr}: want a positive integer",
              file=sys.stderr)
        return 2
    if args.dry_run and args.post:
        print("review: --dry-run prints the plan; nothing to --post",
              file=sys.stderr)
        return 2
    try:
        meta = fetch_pr_view(args.repo, args.pr)
        files, diff_text, truncated = fetch_pr_diff(args.repo, args.pr)
    except ReviewError as exc:
        print(f"review: {exc}", file=sys.stderr)
        return 1
    meta["diff_truncated"] = truncated
    plan = build_plan(args.repo, args.pr, meta, files, diff_text)
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    try:
        context = gather_context(
            args.repo, [e["path"] for e in plan["files"]])
        findings, summary, verdict, label = synthesize(
            plan, diff_text, context, prefer=args.prefer)
    except ReviewError as exc:
        print(f"review: {exc}", file=sys.stderr)
        return 1
    body = render_markdown(args.repo, args.pr, meta, findings,
                           summary, verdict, label)
    if args.post:
        try:
            post_body(args.repo, args.pr, body)
        except ReviewError as exc:
            print(f"review: {exc}", file=sys.stderr)
            return 1
        print(body, end="")
        print(f"review: posted to {args.repo}#{args.pr}", file=sys.stderr)
        return 0
    if args.json:
        print(json.dumps({"repo": args.repo, "pr": args.pr,
                          "model": label, "summary": summary,
                          "verdict": verdict, "findings": findings},
                         indent=2))
        return 0
    print(body, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

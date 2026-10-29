#!/usr/bin/env python3
"""buttons — allowlisted action runner for the S5 phone UI.

Single-user service. It has NO authentication of its own: it binds only
to the compose network (no published port) and is reached exclusively
through oauth2-proxy (GitHub OAuth gate) -> caddy. It can ONLY run the
fixed commands in ACTIONS — there is no free-text command input
anywhere, so even a reached page cannot run arbitrary commands.

It mounts the Docker socket, so it is root-equivalent on the host —
the same trust level as the terminal container, which already has it.

Path identity (load-bearing): the host checkout is mounted at the SAME
path inside this container as on the host (/home/ari/remote-server).
docker compose resolves relative paths (., .env) against the project
directory as seen by the CLI. If the container path differed from the
host path, `up` would hand the daemon a nonexistent bind source and
the terminal container would come up with an empty /srv/remote-server.
"""

import base64
import fcntl
import hashlib
import hmac
import json
import os
import pty
import re
import shlex
import struct
import subprocess
import termios
from html import escape
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STACK = "/home/ari/remote-server"
COMPOSE = ["docker", "compose", "-f", f"{STACK}/docker-compose.yml"]
# Run a command inside the terminal container (where td and Pi live).
EXEC = COMPOSE + ["exec", "-T", "terminal", "bash", "-c"]
TD = "set -a && [ -f /srv/remote-server/.env ] && . /srv/remote-server/.env; set +a && cd /srv/repos/tech-dancer && "

# Repos the page's repo-work UI can cover — discovered, not
# hardcoded. The picker, the repo-scoped buttons, the briefing
# view, PR actions, webhook pulls and warming all cover every
# git checkout in ~/repos, plus the stack checkout itself, which
# lives outside the repos dir and stays special. A repo added to
# ~/repos appears on the next page load with no code change and
# no container restart: discovery runs per request, and the
# picker/strip cards are rendered per request from it. Keys are
# directory basenames (the picker and --repo use them); the gh
# path comes from each checkout's git remote origin, so a new
# repo never needs a mapping table. Only the actions listed as
# scoped honour a repo: the rest are stack-wide (Server group)
# or global (Sync & nudge lists Jules sessions across repos),
# so a repo for them is ignored.
REPOS_DIR = os.environ.get("BUTTONS_REPOS_DIR", "/home/ari/repos")
STACK_GH = "arii/remote-server"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
GH_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPO_SCOPED_ACTIONS = {"briefing-td", "warm-context", "preflight",
                       "create-plan"}


def _td_prefix(cwd):
    return ("set -a && [ -f /srv/remote-server/.env ] && "
            f". /srv/remote-server/.env; set +a && cd {cwd} && ")


def _parse_gh_remote(url):
    """owner/repo from a git remote URL, or None. Accepts the
    ssh (git@github.com:owner/repo[.git]), https, and ssh://
    spellings, github.com only — a non-GitHub remote cannot
    serve the gh api paths the briefing and PR actions use."""
    if not url:
        return None
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    m = re.match(r"^(?:git@github\.com:|https://github\.com/|"
                 r"ssh://git@github\.com/)(.+)$", url)
    if not m or not GH_RE.match(m.group(1)):
        return None
    return m.group(1)


def _discover_repos():
    """name -> {gh, td_cwd, host_path}. remote-server is the
    special stack checkout; every other entry is a ~/repos
    checkout with a parseable GitHub origin. Names and gh paths
    are charset-validated because both are interpolated into
    bash -c strings for td/gh calls — a directory name carrying
    shell text must never reach a command line. A checkout that
    fails discovery is skipped with a log line, never mapped to
    another repo's data. A missing repos dir degrades to the
    stack repo alone, not a crash."""
    repos = {"remote-server": {"gh": STACK_GH,
                               "td_cwd": "/srv/remote-server",
                               "host_path": "/home/ari/remote-server"}}
    try:
        entries = sorted(os.listdir(REPOS_DIR))
    except Exception as exc:
        print(f"repo discovery: cannot list {REPOS_DIR}: {exc}",
              flush=True)
        return repos
    for name in entries:
        if name == "remote-server":
            print("repo discovery: skipping repos-dir entry "
                  "remote-server (name reserved for the stack "
                  "checkout)", flush=True)
            continue
        if not NAME_RE.match(name):
            print(f"repo discovery: skipping {name!r} (unsafe name)",
                  flush=True)
            continue
        host_path = os.path.join(REPOS_DIR, name)
        git_path = os.path.join(host_path, ".git")
        if not (os.path.isdir(git_path) or os.path.isfile(git_path)):
            continue
        try:
            # safe.directory is load-bearing here, as for every
            # other git call in this container: buttons runs as
            # root while the checkouts are owned by ari, and git
            # refuses even a config read in a "dubious" repo
            # without it. Without the flag every repo silently
            # failed discovery (empty stdout, no parseable
            # origin) and only the special stack repo listed —
            # the exact symptom this fixes.
            proc = subprocess.run(
                ["git", "-c", f"safe.directory={host_path}",
                 "-C", host_path, "config", "--get",
                 "remote.origin.url"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True)
            if proc.returncode != 0:
                err = (proc.stderr or "").strip().splitlines()
                print(f"repo discovery: skipping {name} (git "
                      f"config failed: {err[-1] if err else 'exit ' + str(proc.returncode)})",
                      flush=True)
                continue
            gh = _parse_gh_remote(proc.stdout.strip())
        except Exception as exc:
            print(f"repo discovery: skipping {name} ({exc})",
                  flush=True)
            continue
        if not gh:
            print(f"repo discovery: skipping {name} (no parseable "
                  "GitHub origin)", flush=True)
            continue
        repos[name] = {"gh": gh, "td_cwd": f"/srv/repos/{name}",
                       "host_path": host_path}
    return repos


def _default_repo(repos):
    if "tech-dancer" in repos:
        return "tech-dancer"
    return sorted(repos)[0]


def _repo_options_html(repos):
    default = _default_repo(repos)
    out = []
    for name in sorted(repos):
        sel = " selected" if name == default else ""
        out.append(f'<option value="{name}"{sel}>{name}</option>')
    return "\n".join(out)


def _repo_strips_html(repos):
    out = []
    for name in sorted(repos):
        gh = repos[name]["gh"]
        if name == "remote-server":
            scope = ("This stack — terminal, buttons, oauth2-proxy. "
                     "The Server buttons below act here.")
        else:
            scope = ("Repo work — briefing, warm context, preflight, "
                     "PR actions. Tap this card to select it.")
        out.append(
            f'<div class="repo" data-repo-pick="{name}">'
            f'<div class="reponame">'
            f'<a href="https://github.com/{gh}">{gh}</a></div>'
            f'<div class="reposcope">{scope}</div>'
            f'<div class="repostatus" data-repo-status="{gh}">'
            "Auto-pull: not yet</div></div>")
    return "\n".join(out)
# HOME must be the host user's home: ${HOME} in the compose file (repos
# mount) interpolates from the CLI environment, and the daemon resolves
# the resulting host path.
ENV = {**os.environ, "HOME": "/home/ari"}

# --- Suggested next actions (Pi) --------------------------------
# Pi suggests, the allowlist disposes, the user approves. The
# suggest endpoint (1) fetches the briefing JSON for the picked
# repo, (2) compresses it to a compact context — attention items
# + one line per PR + failing runs + Jules count, capped, NOT
# the full briefing and NOT APPEND_SYSTEM.md (Pi gets limited,
# relevant context plus a short task instruction, per the
# standing Pi-context steer) — (3) runs Pi in print mode with
# READ-ONLY tools (read,grep,find,ls: Pi can read the checkout
# for more context but cannot run anything), and (4) parses
# Pi's SUGGEST lines through the td-command allowlist below.
# Anything Pi prints outside the allowlist is shown as text,
# never run. Execution is a separate, approved step: each
# suggestion's Run button asks for confirmation, then
# /api/run-td re-validates the command server-side and runs it
# as a normal run (streamed output, ledger entry). There is no
# batch run and no auto-run in v1: one tap per action is the
# approval gate, matching Pi's own read-only boundary (no
# merge/push/post without explicit approval).
def _validate_td_command(text):
    """argv tokens for a suggested td command, or None. Exact
    shapes only: same tokens, PR numbers digits. Anything else —
    extra flags, pipes, semicolons, other verbs — is rejected,
    so Pi output can never smuggle shell text into a run: the
    runner additionally shlex-quotes every token."""
    try:
        tokens = shlex.split(text or "")
    except Exception:
        return None
    if not tokens or tokens[0] != "td":
        return None
    shapes = [
        ["td", "--json", "agent", "fix-ci", "--pr", "N", "--execute"],
        ["td", "--json", "agent", "list-sessions"],
        ["td", "--json", "jules", "list-sessions"],
        ["td", "--json", "agent", "run-feedback-check"],
        ["td", "--json", "preflight"],
        ["td", "context-warm"],
        ["td", "gh", "read-pr-reviews", "--pr", "N"],
        ["td", "gh", "read-pr-comments", "--pr", "N"],
        ["td", "gh", "resolve-conflicts", "--pr", "N"],
    ]
    for shape in shapes:
        if len(tokens) != len(shape):
            continue
        ok = True
        for want, got in zip(shape, tokens):
            if want == "N":
                if not got.isdigit() or int(got) <= 0:
                    ok = False
                    break
            elif want != got:
                ok = False
                break
        if ok:
            return tokens
    return None


def _suggest_context(brief):
    lines = [f"Repo: {brief.get('repo', '?')}"]
    att = brief.get("attention") or []
    lines.append("Needs attention:")
    if att:
        lines += [f"- {a}" for a in att[:10]]
    else:
        lines.append("- Nothing needs attention.")
    prs = brief.get("prs") or []
    lines.append("Open PRs:")
    for pr in prs[:20]:
        if isinstance(pr, dict):
            lines.append(
                f"- #{pr.get('number')} "
                f"{str(pr.get('title') or '?')[:80]} | "
                f"CI {pr.get('ci')} | merge {pr.get('merge')} | "
                f"conflicted {bool(pr.get('conflicted'))} | "
                f"review {pr.get('review')}")
    fr = brief.get("failing_runs") or []
    if fr:
        lines.append("Failing runs:")
        for r in fr[:5]:
            if isinstance(r, dict):
                line = f"- {r.get('name')} on {r.get('branch')}"
                # The failed jobs/steps are what make a failing run
                # actionable: without them Pi sees "CI on main" and
                # has nothing to map to a command.
                failed = r.get("failed") or []
                if failed:
                    parts = []
                    for f in failed:
                        if isinstance(f, dict):
                            s = str(f.get("job") or "?")
                            steps = f.get("steps") or []
                            if steps:
                                s += f" (steps: {', '.join(str(x) for x in steps[:3])})"
                            parts.append(s)
                    if parts:
                        line += f" — failed: {'; '.join(parts[:3])}"
                lines.append(line)
    j = brief.get("jules") or {}
    if isinstance(j, dict):
        lines.append("Active Jules sessions for this repo: "
                     f"{len(j.get('items') or [])}")
    return "\n".join(lines)[:6000]


def _suggest_prompt(repo, context):
    return (
        f"Suggest the next actions for repo {repo}, as td commands.\n"
        "Use ONLY the context below (plus files you can read in "
        "this repo's checkout). Do not run any tools. Do not use "
        "boomtick MCP tools for this task — they are scoped to a "
        "different repo.\n"
        "Output ONLY lines in exactly this form, one per action:\n"
        "SUGGEST: <td command> :: <one-line reason>\n"
        "Allowed td commands (exact shapes, PR numbers only from "
        "the context):\n"
        "td --json agent fix-ci --pr N --execute\n"
        "td gh read-pr-reviews --pr N\n"
        "td gh read-pr-comments --pr N\n"
        "td gh resolve-conflicts --pr N\n"
        "td context-warm\n"
        "td --json preflight\n"
        "td --json agent list-sessions\n"
        "td --json jules list-sessions\n"
        "td --json agent run-feedback-check\n"
        "If the context shows a problem (failing CI, conflicts, "
        "stale PRs) but no allowed command fits it directly, read "
        "the repo checkout (git log, .github/workflows) to pin "
        "down the cause, then suggest the closest diagnostic "
        "command — do not conclude 'none' while problems remain "
        "visible.\n"
        "If genuinely nothing needs doing, output exactly one "
        "line: SUGGEST: none :: <reason>\n"
        "Context:\n" + context)


def _parse_suggestions(raw):
    """Validated suggestions from Pi's stdout. ANSI escapes are
    stripped first (print mode may color). Unparseable lines are
    dropped, never run; the raw tail is returned alongside so
    the page can show what Pi actually said."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw or "")
    suggestions = []
    for line in clean.splitlines():
        line = line.strip()
        if not line.startswith("SUGGEST:"):
            continue
        rest = line[len("SUGGEST:"):].strip()
        if rest.lower().startswith("none"):
            continue
        if "::" in rest:
            cmdpart, reason = rest.split("::", 1)
        else:
            cmdpart, reason = rest, ""
        tokens = _validate_td_command(cmdpart.strip())
        if not tokens:
            continue
        suggestions.append({
            "command": " ".join(tokens),
            "reason": reason.strip()[:200],
            "caveat": ("resolves in a worktree, does not push"
                       if "resolve-conflicts" in tokens else ""),
        })
    return suggestions


ASK_MAX_CHARS = 2000


def _ask_prompt(repo, instruction):
    """Prompt wrapper for the Ask Pi text box. Pi investigates
    read-only; a write the user asked for (create an issue, run a
    td command) comes back as a PROPOSE block, never executed —
    the page shows it with a confirm dialog and the server
    re-validates before running anything."""
    return (
        f"You are helping Ari from his phone. Repo: {repo}. "
        "Be terse, no filler.\n"
        "The user instruction follows. Investigate read-only using "
        "the files in this repo's checkout (do not run any tools).\n"
        "If the instruction asks for a write action, do the "
        "read-only investigation first, then propose the exact "
        "change and STOP — do not execute it. Allowed proposal "
        "kinds: creating a GitHub issue, or running one allowlisted "
        "td command.\n"
        "Emit a proposal ONLY in exactly this form, on its own lines:\n"
        "PROPOSE issue\n"
        "TITLE: <issue title, one line>\n"
        "BODY:\n"
        "<issue body, markdown>\n"
        "END PROPOSE\n"
        "or\n"
        "PROPOSE td\n"
        "COMMAND: <exact td command>\n"
        "END PROPOSE\n"
        "Allowed td commands (exact shapes):\n"
        "td --json agent fix-ci --pr N --execute\n"
        "td gh read-pr-reviews --pr N\n"
        "td gh read-pr-comments --pr N\n"
        "td gh resolve-conflicts --pr N\n"
        "td context-warm\n"
        "td --json preflight\n"
        "td --json agent list-sessions\n"
        "td --json jules list-sessions\n"
        "td --json agent run-feedback-check\n"
        "If no write is needed, just answer the instruction — no "
        "PROPOSE block.\n"
        "Instruction:\n" + instruction)


def _parse_proposal(raw):
    """Strict PROPOSE block parser. Returns a dict or None. ANSI
    escapes are stripped first. Anything not in the exact form is
    ignored — the reply is still shown, just not runnable. A td
    proposal goes through _validate_td_command, the same gate as
    suggestion Run buttons; an issue proposal is length-capped
    here and the title/body are shown verbatim in the confirm
    dialog before anything is created."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw or "")
    m = re.search(r"^PROPOSE\s+(issue|td)\s*$", clean, re.M)
    if not m:
        return None
    kind = m.group(1)
    tail = clean[m.end():]
    end = re.search(r"^END PROPOSE\s*$", tail, re.M)
    if not end:
        return None
    body = tail[:end.start()]
    if kind == "td":
        cm = re.search(r"^COMMAND:\s*(.+)$", body, re.M)
        if not cm:
            return None
        tokens = _validate_td_command(cm.group(1).strip())
        if not tokens:
            return None
        return {"kind": "td", "command": " ".join(tokens)}
    tm = re.search(r"^TITLE:\s*(.+)$", body, re.M)
    if not tm:
        return None
    title = tm.group(1).strip()[:200]
    bm = re.search(r"^BODY:\s*$", body, re.M)
    text = body[bm.end():].strip()[:4000] if bm else ""
    if not title:
        return None
    return {"kind": "issue", "title": title, "body": text}


# GitHub webhook auto-pull (pull-only, never rebuild, never hooks).
# This is the ONE endpoint that is not behind the OAuth gate: GitHub
# cannot pass GitHub OAuth, so oauth2-proxy skips auth for exactly
# POST /buttons/api/webhook (see docker-compose.yml) and this handler
# is gated only by the HMAC signature below. That is why the rules
# are tight: exact path, POST only (enforced by the skip-auth rule),
# HMAC-SHA256 over the raw body with WEBHOOK_SECRET, allowlisted
# repos, main branch only, fast-forward merges only, async execution
# (the webhook answers 202 immediately; the pull runs in a thread).
# No rebuild, no dependency install, no post-merge hook runs here:
# automated pulls pass -c core.hooksPath=/dev/null, so the tracked
# hooks in hooks/ only ever run for interactive host pulls (see
# scripts/setup-hooks.sh). A failed pull (diverged checkout, local
# modifications, missing key) is recorded in webhook_state and the
# page's status — it never forces anything, and the fix is on the
# host shell. If WEBHOOK_SECRET is unset, the endpoint refuses
# everything (503): fail closed, never accept unsigned webhooks.
# Ingress note (verified 2026-10-28): oauth2-proxy is pinned to
# v7.15.2 because CVE-2026-41059 lets 7.5.0-7.15.1 with
# --reverse-proxy + skip-auth rules be bypassed via a spoofed
# X-Forwarded-Uri header. The skip rule is additionally exact
# (method + full path, no wildcards), and even a bypassed skip rule
# still faces the HMAC check here.
WEBHOOK_PATH = "/buttons/api/webhook"
# No webhook allowlist table: the allowlist is the discovered
# repo set (_discover_repos, gh path -> checkout). A repo added
# to ~/repos accepts webhooks with no code change; a repo
# removed from the dir stops accepting them.
WEBHOOK_MAX_BODY = 10_000_000
webhook_state = {}  # repo -> {"at": epoch, "ok": bool, "detail": str}
webhook_state_lock = threading.Lock()
webhook_pull_lock = threading.Lock()  # serializes pulls, not status reads
webhook_pending = set()

# Process start: the buttons container serves server.py from its
# image, not the checkout, so a webhook pull that ADVANCES the
# checkout after this process started means the served page is
# older than the checkout — the stale flag the page highlights.
# A redundant pull ("Already up to date") does not set it. A
# rebuild restarts the process, which clears the flag (and the
# in-memory pull record with it); a restart without a rebuild
# loses the record, so the flag is fail-silent, never fail-loud.
STARTED_AT = time.time()


def _webhook_secret():
    return os.environ.get("WEBHOOK_SECRET", "")


def _verify_signature(secret, body, header):
    if not header or not header.startswith("sha256="):
        return False
    expect = "sha256=" + hmac.new(secret.encode(), body,
                                  hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, header)


def _webhook_warm(old_head, new_head, info):
    """Best-effort post-pull warming for any repo. Runs in the
    terminal container (same transport as the briefing buttons):
    pnpm install only when package.json/pnpm-lock.yaml changed
    across the pull AND the checkout has a package.json (a
    Python-only repo skips the step instead of failing it),
    context-warm always (no --force: it rewrites only when stale
    or missing). .agent-context.json is gitignored, so warming
    never dirties the checkout. Any failure is logged, never
    surfaced as a pull failure. A repo without a td project
    config degrades here the way briefing does: context-warm
    fails visibly in the container log, and nothing else breaks."""
    host_path = info["host_path"]
    try:
        changed = set()
        if old_head and new_head and old_head != new_head:
            diff = subprocess.run(
                ["git", "-c", "safe.directory=" + host_path, "-C",
                 host_path,
                 "diff", "--name-only", old_head, new_head],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60, text=True)
            changed = set((diff.stdout or "").split())
        if changed & {"package.json", "pnpm-lock.yaml"} and \
                os.path.isfile(os.path.join(host_path, "package.json")):
            subprocess.run(
                EXEC + [f"cd {info['td_cwd']} && "
                        "pnpm install --frozen-lockfile"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=300, text=True)
            print("webhook warm: pnpm install done", flush=True)
    except Exception as exc:
        print(f"webhook warm: pnpm install skipped: {exc}", flush=True)
    try:
        subprocess.run(EXEC + [_td_prefix(info["td_cwd"]) + "td context-warm"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=300, text=True)
        print("webhook warm: context-warm done", flush=True)
    except Exception as exc:
        print(f"webhook warm: context-warm skipped: {exc}", flush=True)


def _webhook_worker(repo, info):
    # Serialized: two pushes racing the same checkout must not run
    # fetch+merge concurrently. State/pending updates are separate
    # and brief, so status reads never block on a pull.
    # repo is the GitHub full_name; info is the discovered entry
    # (host_path for git here, td_cwd for warming in terminal).
    path = info["host_path"]
    with webhook_pull_lock:
        git = ["git", "-c", f"safe.directory={path}", "-C", path,
               "-c", "core.hooksPath=/dev/null"]
        old_head = None
        new_head = None
        advanced = False
        try:
            old_head = subprocess.run(
                git + ["rev-parse", "HEAD"], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=30, text=True,
                env=dict(os.environ)).stdout.strip() or None
        except Exception:
            pass
        try:
            # Inherit the container environment: ssh resolves its
            # key via the passwd home (/root), where the read-only
            # .ssh mount lives — not via $HOME, which the button
            # steps override to /home/ari for compose interpolation.
            env = dict(os.environ)
            fetch = subprocess.run(git + ["fetch", "origin"],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=120,
                                   text=True, env=env)
            if fetch.returncode != 0:
                raise RuntimeError(
                    (fetch.stderr or "").strip().splitlines()[-1]
                    if fetch.stderr.strip() else "fetch failed")
            merge = subprocess.run(git + ["merge", "--ff-only",
                                          "origin/main"],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=120,
                                   text=True, env=env)
            if merge.returncode != 0:
                raise RuntimeError(
                    (merge.stderr or "").strip().splitlines()[-1]
                    if merge.stderr.strip() else "merge failed")
            out = (merge.stdout or "").strip().splitlines()
            ok, detail = True, (out[-1] if out else "up to date")[:200]
            try:
                new_head = subprocess.run(
                    git + ["rev-parse", "HEAD"], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=30, text=True,
                    env=env).stdout.strip() or None
            except Exception:
                pass
            # The stale flag must mean the checkout actually moved:
            # a redundant delivery ("Already up to date") changes
            # nothing, so it must not light the rebuild banner.
            advanced = bool(old_head and new_head
                            and old_head != new_head)
            if ok and advanced:
                # Connector pushes drop exec bits (100755 -> 100644),
                # so scripts can arrive non-executable. Restore them
                # on every advancing pull — the working tree is what
                # runs, and this is cheaper than remembering to chmod.
                try:
                    ls = subprocess.run(
                        git + ["ls-files", "-z", "*.sh"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=30, text=True, env=env)
                    files = [f for f in ls.stdout.split("\0")
                             if f.strip()]
                    if files:
                        subprocess.run(
                            ["chmod", "+x"] + files,
                            cwd=path,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30)
                except Exception:
                    pass
        except Exception as exc:
            ok, detail = False, str(exc)[:200]
    with webhook_state_lock:
        webhook_state[repo] = {"at": time.time(), "ok": ok,
                               "detail": detail, "advanced": advanced}
        webhook_pending.discard(repo)
    _ledger_append({"kind": "webhook", "repo": repo, "ok": ok,
                    "detail": detail, "advanced": advanced})
    print(f"webhook pull {repo}: {'ok' if ok else 'FAILED'}: {detail}",
          flush=True)
    # Warming runs AFTER the pull record lands, in its own thread:
    # it must never hold the pull lock (a slow pnpm install would
    # serialize later pushes) and never fail the pull record.
    if ok:
        threading.Thread(target=_webhook_warm,
                         args=(old_head, new_head, info),
                         daemon=True).start()
# Steps run under a PTY (see _run_step): commands then emit the
# ANSI colours they would show in the terminal, and the page renders
# them. A TTY stdout also makes some tools page their output (git
# log -> less), which would hang a run waiting for input nobody can
# send — so pagers are forced off here, not in each command.
RUN_ENV = {**ENV, "TERM": "xterm-256color",
           "GIT_PAGER": "cat", "PAGER": "cat"}
MAX_OUTPUT = 200_000  # keep the tail; model pulls are verbose

GIT = ["git", "-c", f"safe.directory={STACK}", "-C", STACK]

ACTIONS = {
    "health": {
        "label": "System health",
        "group": "Server",
        "sub": "Disk, memory, container states (read-only)",
        "confirm": False,
        "steps": [
            ["df", "-h", "/srv/remote-server", "/home/ari"],
            ["free", "-m"],
            COMPOSE + ["ps", "--format",
                       "table {{.Name}}\t{{.State}}\t{{.Status}}"],
        ],
    },
    "status": {
        "label": "Status",
        "group": "Server",
        "sub": "Running containers, checkout state, terminal image",
        "confirm": False,
        "steps": [
            GIT + ["status", "--short", "--branch"],
            GIT + ["log", "--oneline", "-3"],
            COMPOSE + ["ps"],
            ["docker", "images", "ghcr.io/arii/remote-server-terminal",
             "--format", "{{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}"],
        ],
    },
    # Rebuilds THIS service from the checked-out code — but NOT
    # from inside itself. The old steps ran `docker compose up
    # --build buttons` from the buttons container: compose stops
    # that container to recreate it, killing the compose CLI
    # mid-sequence, so the new container is never created and the
    # page stays down until someone runs `up -d` on the host.
    # That is the "Deploy crashes the site and it never comes
    # back" bug. These steps run in the TERMINAL container
    # instead (EXEC), which stays up across a buttons recreate,
    # so the sequence completes. HOME=/home/ari is load-bearing
    # for the compose calls: the compose file interpolates
    # ${HOME} for the repos mount, and the terminal runs as root
    # (HOME=/root) — without the override the daemon would mount
    # the host's /root/repos. (git keeps the terminal's own HOME
    # so its gitconfig/ssh mounts still resolve; the -c flag
    # covers safe.directory without depending on that gitconfig.)
    # The verify step proves the NEW container serves before the
    # run reports success; on timeout it prints `compose ps`
    # and fails visibly instead of leaving a silently dead page.
    # Confirm first.
    "deploy-buttons": {
        "label": "Deploy buttons (rebuild this page)",
        "group": "Server",
        "sub": "Pull + rebuild this page (drops ~30s)",
        "confirm": True,
        "risk": "danger",
        "steps": [
            EXEC + ["git -c safe.directory=/srv/remote-server -C /srv/remote-server pull --ff-only"],
            EXEC + ["HOME=/home/ari docker compose -f /srv/remote-server/docker-compose.yml up -d --build buttons"],
            EXEC + ["for i in $(seq 1 30); do if curl -sf http://buttons:8000/buttons/api/status >/dev/null; then echo buttons up; exit 0; fi; sleep 2; done; HOME=/home/ari docker compose -f /srv/remote-server/docker-compose.yml ps buttons; exit 1"],
        ],
    },
    "recreate": {
        "label": "Recreate terminal",
        "group": "Server",
        # A plain `up` leaves an unchanged container alone, so .env
        # edits alone never take effect. Recreate forces the
        # restart with the CURRENT image and .env — no pull, no
        # code update. (There used to be a Deploy latest button that
        # pulled the terminal image too; it was cut in the stable
        # state — its git pull is covered by Deploy buttons and its
        # restart by this button, and a terminal image update, when
        # one is ever needed, is a host-shell job: compose pull
        # terminal && compose up -d terminal.)
        "sub": "Restart terminal with current image and .env",
        "confirm": True,
        "risk": "danger",
        "steps": [
            COMPOSE + ["up", "-d", "--force-recreate", "terminal"],
        ],
    },
    "logs": {
        "label": "Logs",
        "group": "Server",
        # Container logs alone do not audit the agent: they show
        # the terminal process, not what Pi did. The second step
        # renders Pi's own transcript (pi-data volume at /root/.pi
        # in the terminal container) readably — newest sessions,
        # then the latest session's prompts, replies and tool
        # calls. The full JSONL stays on disk; pi_logs.py never
        # fails the run, so a missing sessions dir degrades to a
        # note instead of turning Logs red.
        "sub": "Terminal + Ollama logs, latest Pi session",
        "confirm": False,
        "steps": [
            COMPOSE + ["logs", "--tail=100", "terminal", "ollama"],
            EXEC + ["python3 /srv/remote-server/buttons/pi_logs.py"],
        ],
    },
    # Deterministic briefing: the summarizer script (read-only)
    # inside the terminal container. It parses td's JSON plus gh
    # enrichment into a readable briefing — raw td output is NOT
    # shown here, because td defaults to JSON and its human mode
    # drops per-PR detail. The script lives in the checkout, which
    # the terminal container sees at /srv/remote-server (the same
    # mount the TD prefix uses for .env), so a git pull updates it
    # with no container rebuild. tech-dancer has project_config.json,
    # so td passes config validation there. gh is authed in the
    # terminal container; if a gh call fails, that PR's CI reads
    # "unknown" instead of failing the whole briefing.
    "briefing-td": {
        "label": "Refresh briefing",
        "group": "Briefing",
        "sub": "Open PRs, CI, conflicts from td (repo from picker above)",
        "confirm": False,
        "steps": [
            EXEC + [TD + "python3 /srv/remote-server/buttons/briefing_summary.py --briefing"],
        ],
    },
    # Sync & nudge: first list active Jules sessions (td's agent
    # sync is the list-sessions command, not a verb called sync),
    # then run the one-shot feedback check that posts CI failure
    # and review feedback to stalled sessions. Confirm first: the
    # second step writes to live agent sessions. The list step
    # falls back to the jules group (same fallback the briefing's
    # Jules section uses): a group-name difference must not make
    # the whole button fail silently. A missing JULES_API_KEY is
    # NOT a failure: both steps notice and exit 0, so the run
    # stays green with an explanatory note instead of turning the
    # whole banner red for a config step the user hasn't done.
    # Sync and nudge are separate (issue #2): sync is a read-only
    # button; nudge is a job so the session list (the preview of
    # what would be nudged) is visible before the Approve tap on
    # the write step. The nudge action itself stays hidden — there
    # is no blind-nudge button.
    "sync-agents": {
        "label": "Sync agent sessions",
        "group": "Briefing",
        "sub": "List Jules sessions (read-only)",
        "confirm": False,
        "steps": [
            EXEC + [TD + "if [ -z \"$JULES_API_KEY\" ]; then echo "
                    "\"notice: JULES_API_KEY is unset — add it to .env "
                    "to enable Jules session sync\"; else "
                    "td --json agent list-sessions || "
                    "td --json jules list-sessions; fi"],
        ],
    },
    "nudge-agents": {
        "label": "Nudge agents (job step)",
        "group": "Briefing",
        "sub": "Post feedback to stalled Jules sessions",
        "confirm": False,
        "hidden": True,
        "steps": [
            EXEC + [TD + "if [ -z \"$JULES_API_KEY\" ]; then echo "
                    "\"notice: JULES_API_KEY is unset — skipping "
                    "feedback check\"; else "
                    "td --json agent run-feedback-check; fi"],
        ],
    },
    # Warm context: regenerate .agent-context.json only if stale
    # or missing (context-warm without --force), so td/agy/pi start
    # with a fresh index. The file is gitignored (untracked), so
    # warming never dirties the checkout or blocks a fast-forward.
    "warm-context": {
        "label": "Warm repo context",
        "group": "Briefing",
        "sub": "Regenerate .agent-context.json if stale",
        "confirm": False,
        "steps": [
            EXEC + [TD + "td context-warm"],
        ],
    },
    # Preflight: doctor + typecheck + lint + overlap detection +
    # merge simulation, in one deterministic run. td's raw JSON is
    # 30KB+ of nested lint reports — unreadable on a phone — so the
    # step pipes through preflight_digest.py, which prints one
    # OK/FAIL line per check plus a summary (raw output is kept
    # only when the JSON won't parse). pipefail keeps td's own
    # non-zero exit failing the step; the digest exits 1 when any
    # check fails.
    "preflight": {
        "label": "Preflight check",
        "group": "Briefing",
        "sub": "doctor + typecheck + lint + overlap + merge simulation (compact digest)",
        "confirm": False,
        "steps": [
            EXEC + [TD + "set -o pipefail; td --json preflight | "
                    "python3 /srv/remote-server/buttons/preflight_digest.py"],
        ],
    },
    # Restored 2026-10-29: requested Oct 27 (ad85c046), lost in a
    # refactor. Saves the deterministic briefing as a date-stamped
    # plan file in the repo checkout (.agents/plans/), untracked
    # until committed. Modernized: the old td status-board /
    # search-prs / detect-conflicts chain is gone; the briefing
    # summarizer is the source now.
    "create-plan": {
        "label": "Create plan from briefing",
        "group": "Briefing",
        "sub": "Save briefing to .agents/plans/<date>-briefing-plan.md (repo from picker)",
        "confirm": True,
        "steps": [
            EXEC + [TD + "mkdir -p .agents/plans && "
                    "python3 /srv/remote-server/buttons/briefing_summary.py "
                    "--briefing > .agents/plans/$(date +%F)-briefing-plan.md "
                    "2>&1; "
                    "echo \"wrote .agents/plans/$(date +%F)-briefing-plan.md\"; "
                    "git status --short .agents/plans/"],
        ],
    },
    # Hidden: no button renders for this (see _buttons_html).
    # Suggestion Run buttons post to /api/run-td instead, which
    # attaches the validated argv to the run record; run_action
    # builds this action's single step from it. Steps here stay
    # empty so a bare /api/run naming run-td fails visibly in
    # run_action instead of running an empty step.
    "run-td": {
        "label": "Run suggested td command",
        "group": "Briefing",
        "sub": "Runs one validated td command from the suggestions below.",
        "confirm": False,
        "hidden": True,
        "steps": [],
    },
}

# Action buttons are rendered into the HTML server-side, not built
# by JavaScript. Lesson from 2026-10-28: when the buttons were
# JS-generated, any failure in the page script (parse or early
# runtime) left the page with NO action buttons at all, while the
# static parts (Clear output, Briefing heading) still rendered —
# so the page looked half-alive and gave no clue what broke. Now
# the buttons exist in the raw HTML whether or not any script
# runs; JavaScript only attaches handlers to them. A second,
# separate script block holds the Briefing code, so a Briefing
# failure cannot take the core buttons down with it.
# Buttons are grouped (Server / Briefing) with a one-line subtitle
# under each label, so the page itself says what a button does and
# when to use it — the docs are not where anyone looks mid-task.
# data-label carries the bare label for the confirm dialog; the
# button's textContent now includes the subtitle too.
# Group headings carry the repo each group acts on, because the
# old bare headings left that ambiguous: Server = the
# remote-server stack, Briefing = the repo from the picker above.
GROUP_TITLES = {
    "Server": "Server · arii/remote-server",
    "Briefing": "Briefing · repo from the picker above",
}


def _buttons_html():
    # Server is a closed <details> drawer: the phone screen belongs
    # to repo work, and Status/Logs/Recreate are escape hatches,
    # one tap away. Briefing stays open. Buttons with risk=danger
    # (deploy-buttons, recreate) get an amber left border: they
    # destroy or recreate containers, unlike the read-only actions.
    parts = []
    for group in ("Server", "Briefing"):
        btns = []
        for name, spec in ACTIONS.items():
            if spec["group"] != group or spec.get("hidden"):
                continue
            btns.append(
                f'<button data-action="{escape(name)}" '
                f'data-confirm="{"1" if spec["confirm"] else "0"}" '
                f'class="{"danger" if spec.get("risk") == "danger" else ""}" '
                f'data-label="{escape(spec["label"])}">'
                f'<span class="blabel">{escape(spec["label"])}</span>'
                f'<span class="bsub">{escape(spec["sub"])}</span></button>')
        if group == "Server":
            parts.append(
                f'<details><summary class="grouphead">'
                f'{GROUP_TITLES[group]}</summary>{"".join(btns)}</details>')
        else:
            parts.append(
                f'<details open><summary class="grouphead">'
                f'{GROUP_TITLES[group]}</summary>{"".join(btns)}</details>')
    return "".join(parts)


BUTTONS_HTML = _buttons_html()

runs = {}
runs_lock = threading.Lock()
active_id = None


# --- Audit ledger (durable, append-only JSONL) --------------------
# The ledger lives on a named volume (buttons-data:/var/lib/buttons),
# never in a checkout, so pulls and rebuilds cannot dirty or erase
# it. One line per action: run completions, webhook pulls,
# pr-action taps (including refused ones), and process starts.
# Briefing reads are NOT logged: reads are not actions, and
# logging them would bury the actions. A failed append never
# fails the action it records — the ledger is audit, not gate.
LEDGER_PATH = os.environ.get("BUTTONS_LEDGER_PATH",
                             "/var/lib/buttons/ledger.jsonl")
LEDGER_MAX_BYTES = 2_000_000
LEDGER_PRUNE_LINES = 2000
ledger_lock = threading.Lock()
LEDGER_KINDS = ("run", "webhook", "pr-action", "system", "job")


def _ledger_append(entry):
    """Append one JSON line to the ledger. Never raises."""
    try:
        line = json.dumps({"ts": time.time(), **entry},
                          sort_keys=True) + "\n"
        with ledger_lock:
            d = os.path.dirname(LEDGER_PATH)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(LEDGER_PATH) \
                    and os.path.getsize(LEDGER_PATH) > LEDGER_MAX_BYTES:
                with open(LEDGER_PATH) as f:
                    lines = f.readlines()
                with open(LEDGER_PATH, "w") as f:
                    f.writelines(lines[-LEDGER_PRUNE_LINES:])
            with open(LEDGER_PATH, "a") as f:
                f.write(line)
    except Exception as exc:  # ledger failure must not fail actions
        print(f"ledger append failed: {exc}", flush=True)


def _ledger_read(kind="all", limit=50):
    """Newest-first entries, kind-filtered. [] on any failure."""
    try:
        with open(LEDGER_PATH) as f:
            lines = f.readlines()
    except Exception:
        return []
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue  # skip unparseable lines, keep the rest
        if isinstance(e, dict) and (kind == "all" or e.get("kind") == kind):
            entries.append(e)
    return entries[-limit:][::-1]


def _ledger_clear():
    """Truncate the ledger, then leave a tombstone entry so the
    clear itself stays on the record. Without the tombstone,
    clearing would erase evidence of its own act. The append
    runs outside the truncate lock: _ledger_append takes the
    same lock, and holding it across the call would deadlock.
    Returns True on success."""
    try:
        with ledger_lock:
            d = os.path.dirname(LEDGER_PATH)
            if d:
                os.makedirs(d, exist_ok=True)
            open(LEDGER_PATH, "w").close()
    except Exception as exc:
        print(f"ledger clear failed: {exc}", flush=True)
        return False
    _ledger_append({"kind": "system", "event": "ledger cleared"})
    return True


# --- Agent jobs (multi-step tasks with approval gates) ---------------
# A job = goal + plan checklist + approvals + audit. Jobs only run
# the same allowlisted actions the buttons already offer (ACTIONS
# entries and pr-actions); they add orchestration, not capability.
# Every plan step either runs itself or waits for an explicit
# Approve/Decline tap — the gate that makes multi-step tasks safe
# from the phone. The gate guards WRITES, not reads: a step marked
# "write" acts outside the box (dispatches an agent, spends money)
# and waits for a tap; read-only steps run on their own as soon as
# the previous step finishes. Tapping "Start job" is the approval
# for the plan itself. v1 is local-only and deterministic: no model
# calls inside jobs (Pi judgment stays in the Suggestions flow).
# On terminal state (done/failed/declined) the job writes an audit
# JSON file plus a ledger entry: goal, plan, every approval with
# timestamp, run ids, outcome.
JOBS_PATH = os.environ.get("BUTTONS_JOBS_PATH",
                           "/var/lib/buttons/jobs.json")
JOBS_AUDIT_DIR = os.environ.get("BUTTONS_JOBS_AUDIT_DIR",
                                "/var/lib/buttons/job-audits")
jobs_lock = threading.Lock()
jobs = {}  # id -> job dict; persisted to JOBS_PATH on mutation

JOB_TEMPLATES = {
    "triage": {
        "goal": "Triage {repo}: refresh briefing, warm context, preflight",
        "steps": [
            {"label": "Refresh briefing", "kind": "action",
             "action": "briefing-td", "write": False},
            {"label": "Warm repo context", "kind": "action",
             "action": "warm-context", "write": False},
            {"label": "Preflight check", "kind": "action",
             "action": "preflight", "write": False},
        ],
    },
    "fix-ci": {
        "goal": "Fix CI on PR #{pr} ({repo}) via Jules",
        "needs_pr": True,
        "steps": [
            {"label": "Dispatch Jules fix-ci on PR #{pr}",
             "kind": "pr-action", "action": "fix-ci", "write": True},
        ],
    },
    # Sequential issue fixing: one approval per issue. Approving
    # labels the issue 'jules', which starts a Jules session on it
    # (Jules's GitHub integration). The Project tab's Jules list
    # shows each session's progress before the next approval.
    "fix-issues": {
        "goal": "Fix issues via Jules, sequential: {issues} ({repo})",
        "needs_issues": True,
        "steps": [],
    },
    # Nudge is a job, not a button: the read-only session list
    # (the preview of what would be nudged) runs first, and the
    # write step waits for Approve. No blind-nudge path.
    "nudge-agents": {
        "goal": "Nudge stalled Jules sessions ({repo})",
        "steps": [
            {"label": "List Jules sessions (nudge preview)",
             "kind": "action", "action": "sync-agents",
             "write": False},
            {"label": "Post feedback to stalled sessions",
             "kind": "action", "action": "nudge-agents",
             "write": True},
        ],
    },
}


def _label_issue_jules(repo, issue, repos):
    """Label a GitHub issue 'jules' — Jules's GitHub integration
    starts a session on it. Strict validation, shlex-quoted,
    ledgered by the caller. Never raises ValueError silently."""
    if repo not in repos or not isinstance(issue, int) \
            or isinstance(issue, bool) or issue <= 0:
        raise ValueError("bad repo or issue")
    gh = repos[repo]["gh"]
    _td_run("gh issue edit " + shlex.quote(str(issue)) +
            " --repo " + shlex.quote(gh) +
            " --add-label " + shlex.quote("jules"),
            timeout=60, prefix=_td_prefix(repos[repo]["td_cwd"]))


def _jobs_load():
    """Load persisted jobs; fail interrupted ones. Never raises."""
    try:
        with open(JOBS_PATH) as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for jid, job in data.items():
        if not isinstance(job, dict):
            continue
        if job.get("state") == "active":
            for st in job.get("plan", []):
                if st.get("state") == "running":
                    st["state"] = "failed"
                    st["note"] = "server restarted mid-step"
            job["state"] = "terminal"
            job["outcome"] = "failed"
            job["ended_at"] = time.time()
        jobs[jid] = job


def _jobs_save():
    """Persist the jobs dict. Never raises."""
    try:
        d = os.path.dirname(JOBS_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = JOBS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(jobs, f)
        os.replace(tmp, JOBS_PATH)
    except Exception as exc:
        print(f"jobs save failed: {exc}", flush=True)


def _job_audit(job):
    """Write the completion audit file + ledger entry. Never raises."""
    try:
        os.makedirs(JOBS_AUDIT_DIR, exist_ok=True)
        audit = {
            "id": job["id"], "template": job["template"],
            "goal": job["goal"], "repo": job["repo"],
            "pr": job.get("pr"), "created_at": job["created_at"],
            "ended_at": job.get("ended_at"),
            "outcome": job.get("outcome"),
            "steps": job["plan"], "approvals": job.get("approvals", []),
        }
        with open(os.path.join(JOBS_AUDIT_DIR,
                               "audit-" + job["id"] + ".json"),
                  "w") as f:
            json.dump(audit, f, indent=2, sort_keys=True)
    except Exception as exc:
        print(f"job audit failed: {exc}", flush=True)
    _ledger_append({"kind": "job", "job": job["id"],
                    "template": job["template"], "repo": job["repo"],
                    "outcome": job.get("outcome")})


def _job_end(job, outcome):
    """Terminal state: stamp, audit, persist. jobs_lock held."""
    job["state"] = "terminal"
    job["outcome"] = outcome
    job["ended_at"] = time.time()
    _job_audit(job)
    _jobs_save()


def _job_after_step(job):
    """End the job if the plan reached a terminal state.
    jobs_lock held."""
    states = [s["state"] for s in job["plan"]]
    if all(s == "done" for s in states):
        _job_end(job, "done")
    elif any(s == "failed" for s in states):
        _job_end(job, "failed")


def _job_advance(job):
    """Run the next pending read-only step, if any. Write steps wait
    for an explicit Approve tap. jobs_lock NOT held on entry."""
    while True:
        with jobs_lock:
            if job["state"] != "active":
                return
            idx = next((i for i, s in enumerate(job["plan"])
                        if s["state"] == "pending"), None)
            if idx is None:
                return
            if any(s["state"] != "done" for s in job["plan"][:idx]):
                return
            st = job["plan"][idx]
            if st.get("write"):
                return  # approval gate: waits for a tap
            st["state"] = "running"
            st["auto"] = True
            kind, action = st["kind"], st["action"]
        if kind == "pr-action":
            # Synchronous: runs inline, then advance to the next step.
            try:
                _pr_action(job["pr"], action, job["repo"],
                           _discover_repos())
                ok, err = True, None
            except ValueError as exc:
                ok, err = False, str(exc)
            except Exception as exc:
                ok, err = False, str(exc)[:500]
            with jobs_lock:
                st["state"] = "done" if ok else "failed"
                st["finished_at"] = time.time()
                st["exit_code"] = 0 if ok else 1
                st["note"] = None if ok else err
                _job_after_step(job)
                _jobs_save()
            _ledger_append({"kind": "job", "job": job["id"], "step": idx,
                            "event": "step auto-" + ("done" if ok else "failed"),
                            "ok": ok})
        else:
            # Async action: start the run; the client polls it and
            # reports step-done, which advances the plan again.
            run_id, err = _start_run(action, job["repo"])
            with jobs_lock:
                if err:
                    st["state"] = "pending"
                    st["auto"] = False
                    st["note"] = err
                    _jobs_save()
                    return
                st["run_id"] = run_id
                _jobs_save()
            _ledger_append({"kind": "job", "job": job["id"], "step": idx,
                            "event": "step auto-started", "run": run_id})
            return


def _job_create(template, repo, pr=None, issues=None):
    """Create a job from a template. Returns (job, error)."""
    spec = JOB_TEMPLATES.get(template)
    if spec is None:
        return None, "unknown template"
    repos = _discover_repos()
    if repo not in repos:
        return None, "unknown repo"
    if spec.get("needs_pr"):
        if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
            return None, "bad pr number"
    else:
        pr = None
    if spec.get("needs_issues"):
        if not isinstance(issues, list) or not issues \
                or len(issues) > 10 \
                or any(not isinstance(n, int) or isinstance(n, bool)
                       or n <= 0 for n in issues):
            return None, "bad issues list"
        issues = sorted(set(issues))
    else:
        issues = None
    issues_str = ", ".join("#" + str(n) for n in issues) \
        if issues else ""
    goal = spec["goal"].format(repo=repo, pr=pr, issues=issues_str)
    plan = []
    if spec.get("needs_issues"):
        for n in issues:
            plan.append({
                "label": f"Dispatch Jules on #{n} (label 'jules')",
                "kind": "label-issue", "issue": n,
                "write": True, "state": "pending",
                "approved_at": None, "auto": False, "run_id": None,
                "finished_at": None, "exit_code": None, "note": None,
            })
    else:
        for s in spec["steps"]:
            plan.append({
                "label": s["label"].format(repo=repo, pr=pr),
                "kind": s["kind"], "action": s.get("action"),
                "write": bool(s.get("write", False)),
                "state": "pending", "approved_at": None, "auto": False,
                "run_id": None, "finished_at": None,
                "exit_code": None, "note": None,
            })
    jid = uuid.uuid4().hex[:12]
    now = time.time()
    job = {"id": jid, "template": template, "goal": goal,
           "repo": repo, "pr": pr, "issues": issues, "plan": plan,
           "approvals": [], "state": "active", "outcome": None,
           "created_at": now, "ended_at": None}
    with jobs_lock:
        jobs[jid] = job
        _jobs_save()
    _ledger_append({"kind": "job", "job": jid, "template": template,
                    "repo": repo, "event": "created"})
    _job_advance(job)  # read-only steps start on their own
    return job, None


def _job_public(job):
    """Summary shape for the job list (no step internals)."""
    return {"id": job["id"], "template": job["template"],
            "goal": job["goal"], "repo": job["repo"],
            "state": job["state"], "outcome": job.get("outcome"),
            "created_at": job["created_at"],
            "steps": [{"label": s["label"], "state": s["state"],
                       "run_id": s.get("run_id")}
                      for s in job["plan"]]}


def _append(run, text):
    run["output"] += text
    run["last_output_at"] = time.time()
    if len(run["output"]) > MAX_OUTPUT:
        run["output"] = run["output"][-MAX_OUTPUT:]


def _public(run):
    """What the API returns for one run. Progress is server-computed
    so the page shows it even between polls, and pass/fail comes only
    from the exit code — never from parsing output. Steps run without
    a PTY, so commands emit no ANSI colours to parse anyway."""
    now = time.time()
    done = run["done"]
    elapsed = (run["finished_at"] - run["started_at"]) if done else now - run["started_at"]
    return {
        "id": run["id"], "action": run["action"], "repo": run.get("repo"),
        "done": done,
        "exit_code": run["exit_code"], "output": run["output"],
        "step_index": run["step_index"], "step_total": run["step_total"],
        "current_step": run["current_step"],
        "started_at": run["started_at"], "elapsed_seconds": elapsed,
        "last_output_at": run["last_output_at"],
        "seconds_since_output": 0 if done else now - run["last_output_at"],
    }


def _run_step(step, append):
    """Run one step under a PTY so its output carries the ANSI
    colours a terminal would show (git status, compose logs, td).
    append(chunk) is called with decoded text as it arrives, so the
    page streams. Returns the exit code. stdin is /dev/null: nothing
    here can answer a prompt, and a prompt must fail fast, not hang
    the run. The parent closes its slave fd right after spawn so
    EOF on the master means the child (and anything it spawned
    holding the slave) is gone."""
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 40, 120, 0, 0))
    except Exception:
        pass
    proc = subprocess.Popen(step, stdin=subprocess.DEVNULL,
                            stdout=slave, stderr=slave,
                            cwd=STACK, env=RUN_ENV)
    os.close(slave)
    try:
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            append(chunk.decode("utf-8", "replace"))
    finally:
        os.close(master)
    return proc.wait()


def _finish_run(run_id, action, repo, run):
    # The assignment below needs the global: without it, the
    # `active_id = None` reset would make every read in this
    # function local and raise UnboundLocalError, leaving the
    # run active forever (every later run 409s).
    global active_id
    print(f"run {run_id} ({action}) finished: exit={run['exit_code']}",
          flush=True)
    _ledger_append({"kind": "run", "action": action, "repo": repo,
                    "exit": run["exit_code"],
                    "output": run["output"][-2000:]})
    with runs_lock:
        if active_id == run_id:
            active_id = None


def run_action(run_id, action, repo="tech-dancer"):
    global active_id
    run = runs[run_id]
    repos = _discover_repos()
    if (action in REPO_SCOPED_ACTIONS or action == "run-td") \
            and repo not in repos:
        # The repo vanished between validation and run start (or
        # was never valid): fail visibly rather than running the
        # steps against tech-dancer's checkout by default.
        with runs_lock:
            _append(run, f"error: unknown repo {repo}\n")
            run["exit_code"] = 1
            run["done"] = True
            run["finished_at"] = time.time()
        _finish_run(run_id, action, repo, run)
        return
    steps = ACTIONS[action]["steps"]
    if action == "run-td":
        # The command arrives attached to the run record (from
        # /api/run-td, which validated it); a bare /api/run
        # naming run-td attaches nothing and must fail visibly
        # rather than running an empty step. Tokens are
        # re-validated here and shlex-quoted, so even a
        # tampered record cannot smuggle shell text in.
        tokens = run.get("td_argv")
        if not tokens or _validate_td_command(" ".join(tokens)) != tokens:
            with runs_lock:
                _append(run, "error: run-td needs a validated td "
                             "command attached\n")
                run["exit_code"] = 1
                run["done"] = True
                run["finished_at"] = time.time()
            _finish_run(run_id, action, repo, run)
            return
        prefix = _td_prefix(repos[repo]["td_cwd"])
        steps = [EXEC + [prefix + " ".join(shlex.quote(t)
                                         for t in tokens)]]
    elif repo in repos and action in REPO_SCOPED_ACTIONS \
            and repo != "tech-dancer":
        # Repo-scoped steps run td in the repo checkout: rewrite
        # the TD prefix (the step's last element) to that repo.
        # The step shape is fixed (EXEC + [TD + cmd]), so the
        # rewrite is exact, never string surgery on free text.
        prefix = _td_prefix(repos[repo]["td_cwd"])
        rewritten = []
        for step in steps:
            if len(step) == len(EXEC) + 1 and step[:len(EXEC)] == EXEC \
                    and step[-1].startswith(TD):
                cmd = step[-1][len(TD):]
                if action in ("briefing-td", "create-plan"):
                    # The cwd alone does not pick the repo: the
                    # summarizer's gh api paths need --repo and
                    # --gh (it no longer carries a repo table).
                    # Insert into the python invocation (not the
                    # end: create-plan's command continues after it).
                    cmd = cmd.replace(
                        "briefing_summary.py --briefing",
                        f"briefing_summary.py --briefing --repo {repo} "
                        f"--gh {repos[repo]['gh']}", 1)
                rewritten.append(EXEC + [prefix + cmd])
            else:
                rewritten.append(step)
        steps = rewritten
    for i, step in enumerate(steps, 1):
        with runs_lock:
            run["step_index"] = i
            run["step_total"] = len(steps)
            run["current_step"] = " ".join(step)
            _append(run, "$ " + " ".join(step) + "\n")
        chunks = []

        def _cb(text, run=run, chunks=chunks):
            chunks.append(text)
            with runs_lock:
                _append(run, text)

        try:
            code = _run_step(step, _cb)
        except Exception as exc:  # e.g. docker binary missing
            with runs_lock:
                _append(run, f"error: {exc}\n")
            # The error line IS diagnosable output, so it must
            # suppress the no-output note below (chunks only
            # tracks step output, not this handler's line).
            chunks.append("error")
            code = 1
        if code != 0:
            with runs_lock:
                # A step that fails without printing anything is
                # the hardest failure to diagnose from a phone:
                # the pane shows the command and then nothing.
                # Say so explicitly, with the re-run hint, instead
                # of leaving a blank gap that reads like a
                # rendering bug. (stderr is already merged into
                # the PTY stream, so empty output means the tool
                # itself exited silently — e.g. a missing key or
                # a group name neither spelling covers.)
                if not "".join(chunks).strip():
                    _append(run, "note: the step printed nothing "
                            "before failing — re-run this command "
                            "in the terminal to see the error "
                            "(check JULES_API_KEY and the td "
                            "agent/jules group names first)\n")
                _append(run, f"step failed with exit code {code}; aborting\n")
                run["exit_code"] = code
                run["done"] = True
                run["finished_at"] = time.time()
            break
    else:
        with runs_lock:
            _append(run, "finished: exit 0\n")
            run["exit_code"] = 0
            run["done"] = True
            run["finished_at"] = time.time()
    _finish_run(run_id, action, repo, run)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S5 Control Panel</title>
<style>
:root {
  --bg-app: #0b1120;
  --bg-card: #131d31;
  --bg-card-hover: #1e293b;
  --border: #24344d;
  --text-main: #f1f5f9;
  --text-muted: #94a3b8;
  --primary: #3b82f6;
  --primary-hover: #2563eb;
  --danger: #ef4444;
  --danger-bg: #450a0a;
  --warning: #f59e0b;
  --success: #10b981;
}
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
  margin: 0; padding: 12px 16px 140px; background: var(--bg-app); color: var(--text-main); }
h1 { font-size: 1.2em; margin: 0.3em 0; }
button { display: block; width: 100%; margin: 0.5em 0; padding: 0.9em 1em; font-size: 1.05em;
  background: var(--bg-card); color: var(--text-main); border: 1px solid var(--border);
  border-radius: 10px; text-align: left; cursor: pointer; }
button:hover { background: var(--bg-card-hover); border-color: var(--primary); }
/* danger = destroys or recreates containers (deploy, recreate) */
button.danger { border-left: 4px solid var(--warning); }
button:disabled { opacity: 0.4; }
/* primary / danger / ghost action hierarchy */
button.btn-primary { display: inline-block; width: auto; background: var(--primary);
  border-color: var(--primary); color: #fff; padding: 0.6em 1em; font-size: 0.95em; }
button.btn-primary:hover { background: var(--primary-hover); border-color: var(--primary-hover); }
button.btn-danger { display: inline-block; width: auto; background: var(--danger);
  border-color: var(--danger); color: #fff; padding: 0.6em 1em; font-size: 0.95em; }
button.btn-ghost { display: inline-block; width: auto; background: transparent;
  border: 1px solid var(--border); color: var(--text-main); padding: 0.6em 1em; font-size: 0.95em; }
button.btn-ghost:hover { border-color: var(--primary); background: var(--bg-card); }
#stickyHead { position: sticky; top: 0; z-index: 20; background: var(--bg-app); }
#pickerBar { background: var(--bg-app); padding: 0.5em 0;
  border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 0.5em; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: #555; flex: none; }
.dot.ok { background: var(--success); }
.dot.fail { background: var(--danger); }
.dot.running { background: var(--warning); }
.blabel { display: block; font-weight: 600; }
.bsub { display: block; font-size: 0.85em; color: var(--text-muted); font-weight: normal;
  margin-top: 0.25em; }
.grouphead { font-weight: bold; margin: 1em 0 0.2em; color: var(--text-muted); }
summary.grouphead { cursor: pointer; }
.repo { border: 1px solid var(--border); border-radius: 10px; padding: 0.5em;
  margin: 0.5em 0; background: var(--bg-card); }
.repo[data-repo-pick] { cursor: pointer; transition: border-color 0.15s ease, background-color 0.15s ease; }
.repo[data-repo-pick]:hover { border-color: var(--primary); }
.forrepo { font-size: 0.6em; font-weight: normal; color: var(--text-muted); }
.repo.selected { border-color: var(--primary); background: #1a2744; box-shadow: 0 0 0 1px var(--primary); }
.issuepick { max-height: 12em; overflow-y: auto; margin: 0.5em 0;
  border: 1px solid var(--border); border-radius: 8px; padding: 0.5em; }
.issuepick label { display: block; padding: 0.3em 0; cursor: pointer; }
.issuepick input { margin-right: 0.5em; }
.reponame { font-weight: bold; }
.reponame a { color: inherit; text-decoration: underline; }
.reposcope { font-size: 0.9em; color: var(--text-muted); }
.repostatus { font-size: 0.9em; color: var(--text-muted); }
.planexcerpt { max-height: 30vh; }
.planact button { display: inline-block; width: auto; margin: 0.5em 0.5em 0.5em 0;
  padding: 0.5em 0.8em; font-size: 0.9em; }
.planact a { margin-left: 0.5em; }
#jsdiag { color: #ffd75e; border: 1px solid #8a6d1b; background: #3a2f0a;
  padding: 0.5em; border-radius: 8px; margin: 0.5em 0; }
#liveBar { font-size: 0.9em; color: var(--text-muted); margin: 0.5em 0; }
.spinner { display: inline-block; width: 12px; height: 12px;
  margin-right: 0.4em; vertical-align: -2px; border: 2px solid #555;
  border-top-color: #eee; border-radius: 50%;
  animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
#state { margin: 0.5em 0.8em; font-weight: bold; padding: 0.5em; border-radius: 8px;
  white-space: pre-line; border: 1px solid var(--border); }
#state.ok { color: #7ed07e; background: #12351a; border-color: #2e7d32; }
#state.fail { color: #ff8a8a; background: var(--danger-bg); border-color: var(--danger); }
#state.running { color: #ffd75e; background: #3a2f0a; border-color: #8a6d1b; }
pre { white-space: pre-wrap; word-break: break-all; background: #050811; padding: 0.5em;
  border-radius: 8px; max-height: 60vh; overflow: auto; border: 1px solid var(--border); }
pre.ok { border-color: #2e7d32; }
pre.fail { border-color: var(--danger); }
pre.running { border-color: #8a6d1b; }
h2 { font-size: 1.1em; margin-top: 1.5em; }
/* PR cards: badge + status dot + title, chips, action strip */
.prcard { border: 1px solid var(--border); border-radius: 10px; padding: 0.75em;
  margin: 0.5em 0; background: var(--bg-card); }
.prhead { display: flex; align-items: baseline; gap: 0.5em; margin-bottom: 0.4em; }
.prnum { font-weight: bold; background: var(--bg-card-hover); border: 1px solid var(--border);
  border-radius: 6px; padding: 0.1em 0.45em; font-size: 0.9em; white-space: nowrap; }
.prcard .prtitle { font-weight: bold; }
.prcard .prtitle a { color: var(--text-main); }
.chips { display: flex; flex-wrap: wrap; gap: 0.35em; margin: 0.4em 0; }
.chip { font-size: 0.8em; color: var(--text-muted); background: var(--bg-card-hover);
  border: 1px solid var(--border); border-radius: 20px; padding: 0.15em 0.6em;
  white-space: nowrap; }
.chip-ok { color: #7ed07e; border-color: #2e7d32; }
.chip-fail { color: #ff8a8a; border-color: var(--danger); }
.chip-warn { color: #ffd75e; border-color: #8a6d1b; }
.prcard .prmeta { font-size: 0.9em; color: var(--text-muted); }
.planrow { display: flex; gap: 0.5em; align-items: center; flex-wrap: wrap;
  margin: 0.4em 0; }
.planrow button { width: auto; margin: 0; }
.planrow input, .planrow select { font-size: 0.9em; }
#issues .prcard, #julesSessions .prcard { margin: 0.4em 0; }
#issues details { margin-top: 0.4em; }
#issues input { margin-right: 0.4em; }
.prcard .prresult { font-size: 0.9em; margin-top: 0.4em; }
.practions { margin-top: 0.5em; display: flex; gap: 0.5em; flex-wrap: wrap; }
.practions button { margin: 0; }
#staleBar { border: 1px solid var(--warning); border-radius: 8px; padding: 0.5em; margin: 0.5em 0; }
button.needs-update { border-color: var(--warning); box-shadow: 0 0 0 2px rgba(245,158,11,0.4); }
#briefingErr { color: #ff8a8a; }
#askBox { width: 100%; box-sizing: border-box; background: var(--bg-card);
  color: var(--text-main); border: 1px solid var(--border); border-radius: 10px;
  padding: 0.6em; font: inherit; margin: 0.4em 0; resize: vertical; }
#askBox:focus { border-color: var(--primary); outline: none; }
.proposal { border: 1px solid var(--primary); border-radius: 10px; padding: 0.75em;
  margin: 0.5em 0; background: var(--bg-card); }
.proposal .ptitle { font-weight: bold; margin-bottom: 0.4em; }
.proposal pre { max-height: 12em; overflow: auto; }
#askErr, #suggestErr { color: #ff8a8a; }
a { color: #8bf; }
/* segmented tab bar */
#tabbar { display: flex; gap: 0.4em; background: var(--bg-app); padding: 0.5em 0;
  border-bottom: 1px solid var(--border); }
#tabbar .tab { flex: 1; width: auto; margin: 0; padding: 0.6em 0.2em; font-size: 0.9em;
  text-align: center; }
#tabbar .tab.active { border-color: var(--primary); background: var(--bg-card-hover);
  font-weight: bold; }
@media (max-width: 520px) {
  #tabbar .tab { font-size: 0.72em; padding: 0.6em 0.1em; }
}
/* slide-up output drawer */
#outputDrawer { position: fixed; left: 0; right: 0; bottom: 0; z-index: 1000;
  background: #050811; border-top: 1px solid var(--border);
  box-shadow: 0 -4px 16px rgba(0,0,0,0.6);
  transform: translateY(105%); transition: transform 0.25s ease-out; }
#outputDrawer.open { transform: none; }
#drawerHead { display: flex; align-items: center; justify-content: space-between;
  padding: 0.4em 0.8em; border-bottom: 1px solid var(--border); font-weight: bold; }
#drawerHead .dbactions { display: flex; gap: 0.5em; }
#drawerHead button { width: auto; margin: 0; padding: 0.7em 1em; font-size: 0.85em; }
#piSessionList:empty { display: none; }
.pirow { display: flex; gap: 0.5em; align-items: center; margin: 0.4em 0; }
.pirow button:first-child { flex: 1; text-align: left; }
.pirowdel { flex: 0 0 auto; width: auto; padding: 0.4em 0.7em; }
#outputDrawer pre { border: none; border-radius: 0; max-height: 45vh; margin: 0; }
/* confirm modal (replaces window.confirm) */
#confirmModal { position: fixed; inset: 0; z-index: 2000; display: none;
  align-items: center; justify-content: center; background: rgba(0,0,0,0.6); padding: 1em; }
#confirmModal.open { display: flex; }
.modal-card { background: var(--bg-card); border: 1px solid var(--border);
  border-radius: 12px; padding: 1.2em; max-width: 26em; width: 100%; }
#confirmTitle { font-weight: bold; font-size: 1.1em; margin-bottom: 0.5em; }
#confirmBody { color: var(--text-muted); margin-bottom: 1em; white-space: pre-line; }
.modal-actions { display: flex; gap: 0.5em; justify-content: flex-end; }
.modal-actions button { margin: 0; }
/* status-first attention section */
#attention { border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-card); padding: 0.75em; margin: 0.5em 0; }
#attention ul { margin: 0.4em 0; padding-left: 1.2em; }
#attention li { margin: 0.25em 0; font-size: 0.92em; }
#attention .attn-head { font-weight: bold; margin-bottom: 0.2em; }
/* jobs */
.jobcard { border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-card); padding: 0.75em; margin: 0.5em 0; }
.jobcard .jobgoal { font-weight: bold; margin-bottom: 0.4em; }
.jobcard .jobmeta { font-size: 0.85em; color: var(--text-muted); margin-bottom: 0.4em; }
.jobstep { display: flex; align-items: center; gap: 0.5em; padding: 0.35em 0;
  border-top: 1px solid var(--border); font-size: 0.92em; }
.jobstep .st { font-size: 0.78em; border-radius: 20px; padding: 0.15em 0.6em;
  border: 1px solid var(--border); color: var(--text-muted); white-space: nowrap; }
.jobstep .st.done { color: #7ed07e; border-color: #2e7d32; }
.jobstep .st.failed { color: #ff8a8a; border-color: var(--danger); }
.jobstep .st.running { color: #ffd75e; border-color: #8a6d1b; }
.jobstep .st.approved { color: #8bf; border-color: var(--primary); }
.jobstep .st.declined { color: var(--text-muted); }
.jobstep .sactions { margin-left: auto; display: flex; gap: 0.4em; }
.jobstep .sactions button { width: auto; margin: 0; padding: 0.35em 0.7em; font-size: 0.85em; }
.jobtmpl { border: 1px solid var(--border); border-radius: 10px; padding: 0.6em;
  margin: 0.4em 0; background: var(--bg-card); }
.jobtmpl .blabel { font-weight: 600; }
.jobtmpl .bsub { font-size: 0.85em; color: var(--text-muted); margin: 0.2em 0 0.4em; }
.jobtmpl button { width: auto; margin: 0.2em 0 0; }
</style>
</head>
<body>
<h1>S5 Control Panel</h1>
<p><a href="/">Terminal</a> &middot; fixed actions only, no free-text commands.
Tap a repo card below to select it for the buttons and briefing.</p>
<div id="repos">
<!--REPO_STRIPS-->
</div>
<div id="liveBar"><span id="liveSpinner" class="spinner" style="display:none"></span><span id="liveUpdated"></span><span id="livePending"></span></div>
<div id="jsdiag">JavaScript is not running on this page — if this line stays
visible, hard-refresh and check the browser console. The buttons
below are still visible, but they need JavaScript to start a run.</div>
<div id="stickyHead">
<div id="pickerBar"><span id="serverDot" class="dot" title="server status"></span><span>Active repo: <strong id="selectedRepoName">...</strong></span><select id="briefingRepo" style="display:none">
<!--REPO_OPTIONS-->
</select></div>
<div id="tabbar" role="tablist">
<button class="tab active" data-tab="actions" role="tab">⚡ Actions</button>
<button class="tab" data-tab="briefing" role="tab">📋 Briefing</button>
<button class="tab" data-tab="pi" role="tab">🤖 Pi agent</button>
<button class="tab" data-tab="project" role="tab">📊 Project</button>
<button class="tab" data-tab="jobs" role="tab">🧰 Jobs</button><button class="tab" data-tab="ledger" role="tab">📜 Ledger</button>
</div>
</div>
<section class="tabpanel" id="tab-actions">
<div id="attention"><div id="staleBar" style="display:none">Stack code updated since this page was built — tap Deploy buttons below to rebuild.</div><div id="attentionBody"><span class="spinner"></span> Loading briefing…</div></div>
<div id="buttons"><!--BUTTONS--></div>
</section>
<section class="tabpanel" id="tab-briefing" style="display:none">
<h2>Briefing</h2>
<p>Live briefing — open PRs, CI, conflicts, reviews, Jules
sessions. Runs on load; the Refresh briefing button re-runs it
from td. Tap a repo card above to select the repo for the cards,
their action buttons, and the repo buttons.</p>
<div id="briefing"></div>
<div id="briefingErr"></div>
</section>
<section class="tabpanel" id="tab-pi" style="display:none">
<h2>Suggested next actions</h2>
<details><summary>How this works</summary>
<p>Pi reads the briefing context and suggests td commands, then
runs only what you approve: each suggestion has its own Run
button with a confirm dialog. Pi runs read-only here;
suggestions are validated against a fixed td-command list,
anything else is shown as text, never run. Asking Pi takes
minutes on the local model.</p>
</details>
<button id="suggestBtn">Suggest next actions</button>
<div id="suggestions"></div>
<div id="suggestErr"></div>

<h2>Ask Pi</h2>
<details><summary>How this works</summary>
<p>Open-ended instructions for this repo, in your own words —
e.g. "draft an issue for the flaky briefing refresh" or "where
does the webhook worker live?". Pi investigates read-only and
replies here. If you asked for a write (like creating an issue),
Pi proposes the exact change and nothing runs until you confirm
it. Takes minutes on the local model.</p>
</details>
<textarea id="askBox" rows="3" placeholder="Ask Pi anything about this repo…"></textarea>
<button id="askBtn">Ask Pi</button>
<div id="askOut"></div>
<div id="askErr"></div>

<h2>Pi sessions</h2>
<details><summary>How this works</summary>
<p>Browse Pi transcripts on disk (pi-data volume). List shows the
newest sessions with date, repo, and first prompt — tap one to
render it readably. The full JSONL stays on disk. Clearing
deletes all transcripts.</p>
</details>
<button id="listPiSessions">List sessions</button>
<button id="clearPiSessions">Clear Pi sessions</button>
<div id="piSessionList"></div>
<pre id="piSessionOut"></pre>
<div id="piSessionErr"></div>
</section>
<section class="tabpanel" id="tab-project" style="display:none">
<h2>Issues <span class="forrepo" data-forrepo></span></h2>
<details><summary>How this works</summary>
<p>Open issues for the picked repo — status, assignees, labels.
Tap one to expand, assign, or open it in GitHub.</p>
</details>
<button id="refreshIssues">Refresh issues</button>
<div id="issues"></div>
<div id="issuesErr"></div>
<h3>New issue</h3>
<input id="newIssueTitle" placeholder="Title" maxlength="200" style="width:100%;box-sizing:border-box">
<textarea id="newIssueBody" rows="3" placeholder="Body (optional)"></textarea>
<button id="createIssueBtn">Create issue</button>
<div id="newIssueErr"></div>
<h2>Jules sessions <span class="forrepo" data-forrepo></span></h2>
<details><summary>How this works</summary>
<p>What Jules is doing — state, what it's working on, link.
Needs JULES_API_KEY in .env.</p>
</details>
<button id="refreshJules">Refresh Jules sessions</button>
<div id="julesSessions"></div>
<div id="julesErr"></div>
<h2>Plans <span class="forrepo" data-forrepo></span></h2>
<details><summary>How this works</summary>
<p>Deterministic td planning guides. They read repo state and
write a markdown plan file; nothing executes or pushes.</p>
</details>
<div class="planrow"><button id="planIssueAuditBtn">Audit open issues</button>
<button id="planWorkflowAuditBtn">Audit workflows</button></div>
<div class="planrow"><label>Review plan for PR
<select id="planPrSelect"><option value="">—</option></select></label>
<button id="planReviewBtn">Generate</button></div>
<div class="planrow"><span>Aggregate PRs</span>
<select id="planAggPrs" multiple size="4" style="min-width:9em"><option value="">—</option></select>
<label>into <input id="planAggTarget" value="main" size="12" maxlength="64"></label>
<button id="planAggBtn">Generate</button></div>
<div id="planOut"></div>
<div id="planErr"></div>
</section>
<section class="tabpanel" id="tab-jobs" style="display:none">
<h2>Jobs</h2>
<details><summary>How this works</summary>
<p>Multi-step tasks with a visible plan. Read-only steps run on
their own once the job starts; only steps that act outside the box
wait for your Approve or Decline tap. Output streams to the drawer;
declining ends the job. Every job writes an audit record — goal,
plan, approvals with timestamps, run ids, outcome — to the Ledger
tab and a JSON file on the buttons volume. Jobs only run the same
allowlisted actions the buttons already offer; nothing new becomes
writable.</p>
</details>
<div id="jobNew"></div>
<div id="jobErr"></div>
<h2>Active and past jobs</h2>
<div id="jobList"></div>
<div id="jobListErr"></div>
</section>
<section class="tabpanel" id="tab-ledger" style="display:none">
<details open><summary class="grouphead">Ledger</summary>
<details><summary>How this works</summary>
<p>Durable audit log — every run, webhook pull, and PR-action tap,
newest first. Survives container restarts (named volume, not the
checkout). Briefing reads are not logged. Clearing deletes all
entries but leaves a 'ledger cleared' record.</p>
</details>
<label>Kind: <select id="ledgerKind">
<option value="all">All</option>
<option value="run">Runs</option>
<option value="webhook">Webhooks</option>
<option value="pr-action">PR actions</option>
<option value="job">Jobs</option>
<option value="system">System</option>
</select></label>
<div id="ledger"></div>
<button id="refreshLedger">Refresh ledger</button>
<button id="clearLedger">Clear ledger</button>
<div id="ledgerErr"></div>
</details>
</section>
<div id="outputDrawer">
<div id="drawerHead"><span>Output</span><span class="dbactions"><button id="bugBtn" title="File the failed run as a GitHub issue">Report bug</button><button id="clearBtn">Clear output</button><button id="drawerClose">Close</button></span></div>
<div id="state"></div>
<pre id="out"></pre>
</div>
<div id="confirmModal"><div class="modal-card">
<div id="confirmTitle"></div>
<div id="confirmBody"></div>
<div class="modal-actions"><button id="confirmCancel" class="btn-ghost">Cancel</button><button id="confirmOk" class="btn-primary">Confirm</button></div>
</div></div>
<script>
// Core script (script block 1). The buttons above are rendered
// server-side; this only attaches handlers to them, so a failure
// here cannot make the buttons vanish — and the diagnostic line
// above stays visible if this block never runs at all.
document.getElementById("jsdiag").style.display = "none";
const btnDiv = document.getElementById("buttons");
const stateEl = document.getElementById("state");
const outEl = document.getElementById("out");
let timer = null;
// True only for a run this page-load started (not a re-attached
// run from a reload): its completion opens the output drawer,
// because on a phone the drawer is how progress stays visible
// across tabs.
let userStartedRun = false;

// Repo cards are tap-to-select: clicking a card (outside its
// GitHub link) sets the picker and refreshes the briefing. The
// picker remains as the explicit control.
function markSelectedRepo() {
  const picker = document.getElementById("briefingRepo");
  const cur = picker ? picker.value : "";
  const nameEl = document.getElementById("selectedRepoName");
  if (nameEl && cur) nameEl.textContent = cur;
  document.querySelectorAll("[data-repo-pick]").forEach(el => {
    el.classList.toggle("selected",
      el.getAttribute("data-repo-pick") === cur);
  });
}
document.querySelectorAll("[data-repo-pick]").forEach(el => {
  el.addEventListener("click", e => {
    if (e.target.closest("a")) return;
    const picker = document.getElementById("briefingRepo");
    if (!picker) return;
    picker.value = el.getAttribute("data-repo-pick");
    markSelectedRepo();
    if (typeof refreshBriefing === "function") refreshBriefing();
  });
});
// Project tab: repo context lives where the work happens. The
// section headers name the repo, and switching the picker
// re-renders the tab (like the briefing does) so a stale repo
// is never a silent empty list.
function refreshProject() {
  const repo = projectRepo();
  document.querySelectorAll("[data-forrepo]").forEach(el => {
    el.textContent = "— " + repo;
  });
  if (typeof refreshIssues === "function") refreshIssues();
  if (typeof refreshJulesSessions === "function") refreshJulesSessions();
}
const _pickerEl = document.getElementById("briefingRepo");
if (_pickerEl) _pickerEl.addEventListener("change", markSelectedRepo);
if (_pickerEl) _pickerEl.addEventListener("change", () => {
  if (typeof refreshProject === "function") refreshProject();
});
markSelectedRepo();

// Tab bar: one visible panel at a time. The output drawer is
// fixed-position, so it stays reachable from every tab.
document.querySelectorAll("#tabbar .tab").forEach(t => {
  t.onclick = () => {
    document.querySelectorAll("#tabbar .tab").forEach(x =>
      x.classList.toggle("active", x === t));
    document.querySelectorAll(".tabpanel").forEach(p => {
      p.style.display = (p.id === "tab-" + t.dataset.tab) ? "" : "none";
    });
    // Opening the Project tab re-renders it for the current
    // picker repo, so the sections never show a stale repo's data.
    if (t.dataset.tab === "project" &&
        typeof refreshProject === "function") refreshProject();
  };
});

// Slide-up output drawer (replaces the inline <pre> below the
// buttons): opens on run start and on new output, so progress is
// visible without scrolling, on any tab.
const drawerEl = document.getElementById("outputDrawer");
function openDrawer() { if (drawerEl) drawerEl.classList.add("open"); }
function closeDrawer() { if (drawerEl) drawerEl.classList.remove("open"); }
const drawerCloseBtn = document.getElementById("drawerClose");
if (drawerCloseBtn) drawerCloseBtn.onclick = closeDrawer;

// Confirm modal (replaces window.confirm): non-blocking, carries
// a title + body, danger styling for destructive actions.
// Returns a Promise<boolean>.
function confirmDialog(title, body, danger) {
  return new Promise(resolve => {
    const modal = document.getElementById("confirmModal");
    const ok = document.getElementById("confirmOk");
    const cancel = document.getElementById("confirmCancel");
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmBody").textContent = body;
    ok.className = danger ? "btn-danger" : "btn-primary";
    const done = v => {
      modal.classList.remove("open");
      ok.onclick = null; cancel.onclick = null; modal.onclick = null;
      resolve(v);
    };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
    modal.onclick = e => { if (e.target === modal) done(false); };
    modal.classList.add("open");
  });
}

// Safe JSON fetch: never throws on an empty or non-JSON body.
// Returns {ok, status, data}; data is the parsed JSON, or
// {error: <text>} when the body is not JSON, or {error: ...}
// on network failure. Call sites read data.error with
// textContent only — never innerHTML — so an error string can
// never inject markup.
// Map raw infrastructure errors to one-line plain-language
// messages with a next action. The raw detail stays in the
// ledger; the phone screen gets the actionable version.
function friendlyError(msg) {
  msg = String(msg || "");
  if (/\[Errno 2\] No such file or directory: 'docker'/.test(msg))
    return "terminal container not reachable — is the stack " +
      "running? Check the Terminal tab.";
  if (/\[Errno 2\] No such file or directory/.test(msg))
    return "a required file or checkout is missing on the server — " +
      "check the terminal container.";
  if (/JULES_API_KEY/.test(msg))
    return "JULES_API_KEY is not set — add it to .env to enable " +
      "Jules features.";
  return msg;
}
async function fetchJson(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    return {ok: false, status: 0, data: {error:
      "cannot reach the buttons server — check your connection, " +
      "or it may be rebuilding (retry in ~30s)"}};
  }
  let text;
  try {
    text = await res.text();
  } catch (e) {
    return {ok: res.ok, status: res.status,
      data: {error: "unreadable response: " + e}};
  }
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (e) {
    data = {error: text.slice(0, 200) || ("HTTP " + res.status)};
  }
  if (!res.ok && res.status === 502 && !data.error) {
    // 502 comes from the proxy in front, never from our API:
    // the buttons container is down or rebuilding.
    data.error = "buttons server unreachable (502) — it may be " +
      "rebuilding; retry in ~30s";
  }
  if (!res.ok && data.error) data.error = friendlyError(data.error);
  return {ok: res.ok, status: res.status, data: data};
}

async function startRun(name, label, confirmFlag, dangerFlag) {
  userStartedRun = true;
  if (confirmFlag && !(await confirmDialog("Run action", label + " — run it?", !!dangerFlag))) return;
  openDrawer();
  // Repo-scoped buttons follow the picker above; the rest are
  // stack-wide or global and ignore it.
  const body = {action: name};
  if (name === "briefing-td" || name === "warm-context" || name === "preflight") {
    const picker = document.getElementById("briefingRepo");
    if (picker) body.repo = picker.value;
  }
  const res = await fetchJson("/buttons/api/run", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  if (!res.ok) {
    stateEl.textContent = "error: " + (res.data.error || ("HTTP " + res.status));
    return;
  }
  poll(res.data.id, name);
}

btnDiv.querySelectorAll("button").forEach(b => {
  b.onclick = () => startRun(b.dataset.action, b.dataset.label,
    b.dataset.confirm === "1", b.classList.contains("danger"));
});

function setBusy(busy) {
  btnDiv.querySelectorAll("button").forEach(b => b.disabled = busy);
}

function fmt(s) {
  s = Math.max(0, Math.floor(s));
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
}

// ANSI colour rendering for the output pane. Steps run under a
// PTY, so their output carries SGR colour codes; without this the
// pane would show raw escape sequences. Pass/fail colour (the pane
// border) still comes only from the exit code — this only renders
// the command's own colours.
const ANSI_FG = ["#888", "#ff8a8a", "#7ed07e", "#ffd75e", "#8bf", "#e0a0ff", "#7ee0e0", "#eee"];
const ANSI_FG_BRIGHT = ["#aaa", "#ffb0b0", "#a5e8a5", "#ffe88a", "#b0d0ff", "#f0c0ff", "#a5f0f0", "#fff"];
function ansi256(n) {
  if (n < 8) return ANSI_FG[n];
  if (n < 16) return ANSI_FG_BRIGHT[n - 8];
  if (n < 232) {
    n -= 16;
    const v = x => x === 0 ? 0 : 55 + 40 * x;
    return "rgb(" + v(Math.floor(n / 36)) + "," + v(Math.floor(n % 36 / 6)) + "," + v(n % 6) + ")";
  }
  const g = 8 + 10 * (n - 232);
  return "rgb(" + g + "," + g + "," + g + ")";
}
function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function ansiLineToHtml(line) {
  // Strip non-SGR escapes (cursor moves, titles, charsets), then
  // split on SGR sequences and rebuild as styled spans.
  line = line.replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-9;?]*[A-Za-z]/g, m => m.endsWith("m") ? m : "")
    .replace(/\x1b\([A-Za-z0-9]/g, "").replace(/\x07/g, "");
  const parts = line.split(/(\x1b\[[0-9;]*m)/g);
  let bold = false, fg = null, bg = null;
  let out = "", buf = "";
  function flush() {
    if (!buf) return;
    let style = "";
    if (fg) style += "color:" + fg + ";";
    if (bg) style += "background-color:" + bg + ";";
    if (bold) style += "font-weight:bold;";
    out += style ? '<span style="' + style + '">' + escHtml(buf) + "</span>" : escHtml(buf);
    buf = "";
  }
  for (const part of parts) {
    const m = part.match(/^\x1b\[([0-9;]*)m$/);
    if (!m) { buf += part; continue; }
    flush();
    const codes = m[1] === "" ? [0] : m[1].split(";").map(x => parseInt(x, 10) || 0);
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) { bold = false; fg = null; bg = null; }
      else if (c === 1) bold = true;
      else if (c === 22) bold = false;
      else if (c >= 30 && c <= 37) fg = ANSI_FG[c - 30];
      else if (c === 39) fg = null;
      else if (c >= 40 && c <= 47) bg = ANSI_FG[c - 40];
      else if (c === 49) bg = null;
      else if (c >= 90 && c <= 97) fg = ANSI_FG_BRIGHT[c - 90];
      else if (c >= 100 && c <= 107) bg = ANSI_FG_BRIGHT[c - 100];
      else if ((c === 38 || c === 48) && codes[i + 1] === 5) {
        const col = ansi256(codes[i + 2] || 0);
        if (c === 38) fg = col; else bg = col;
        i += 2;
      } else if ((c === 38 || c === 48) && codes[i + 1] === 2) {
        const col = "rgb(" + (codes[i + 2] || 0) + "," + (codes[i + 3] || 0) + "," + (codes[i + 4] || 0) + ")";
        if (c === 38) fg = col; else bg = col;
        i += 4;
      }
    }
  }
  flush();
  return out;
}
function ansiToHtml(text) {
  // The PTY translates \n to \r\n (ONLCR), so a raw \r\n pair is a
  // normal line ending, not a redraw. Collapse those first; a lone \r
  // still overwrites the current line (progress output), so a line
  // built with \r shows only its final state, not every redraw.
  text = text.replace(/\r\n/g, "\n");
  const lines = [];
  let cur = "";
  for (const ch of text) {
    if (ch === "\n") { lines.push(cur); cur = ""; }
    else if (ch === "\r") { cur = ""; }
    else cur += ch;
  }
  lines.push(cur);
  return lines.map(ansiLineToHtml).join("\n");
}
let lastRenderedOutput = null;

// Last error line for the failure banner, read from the output
// pane itself: the pane already holds the run's output with ANSI
// stripped (ansiToHtml), so its text is the plain tail — no
// second strip pass. The banner used to show the last 3 raw
// lines, but the tail usually ends with our own "step failed"
// trailer and the echoed "$ ..." command line, neither of which
// says anything about the cause — and the pane sits below the
// fold on a phone, so the cause was never visible. Noise is
// filtered first, then the most error-like line wins: a JSON
// "message"/"error" field, a traceback, or an Error/failed line.
// Falls back to the raw last line when nothing looks like an
// error. One line, truncated: a banner is a pointer, not a log.
function lastErrorLines() {
  const lines = outEl.textContent.split("\n").map(l => {
    const i = l.lastIndexOf("\r");
    return (i >= 0 ? l.slice(i + 1) : l).trim();
  }).filter(l => l && !l.startsWith("$ ") &&
    !/^step failed with exit code \d+; aborting$/.test(l));
  if (!lines.length) return "";
  const isErr = l =>
    /"(message|error)"\s*:|traceback|error|failed|exception/i.test(l);
  let pick = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (isErr(lines[i])) { pick = i; break; }
  }
  const line = pick >= 0 ? lines[pick] : lines[lines.length - 1];
  return "\n" + line.slice(0, 220);
}

// Status colour comes from the exit code only, never from output
// text: steps run without a PTY, so there is no terminal colour to
// read. A silent-but-running step is distinguished from a stuck one
// by "last output … ago".
function renderState(run) {
  const now = Date.now() / 1000;
  const elapsed = run.done ? run.elapsed_seconds : now - run.started_at;
  const since = run.done ? 0 : now - run.last_output_at;
  const step = "step " + run.step_index + "/" + run.step_total;
  let cls, text;
  if (run.done && run.exit_code === 0) {
    cls = "ok";
    text = "done ✓ — " + step + " — " + fmt(elapsed);
  } else if (run.done) {
    cls = "fail";
    text = "failed (exit " + run.exit_code + ") — " + step +
      " — " + fmt(elapsed) + lastErrorLines();
  } else {
    cls = "running";
    text = "running: " + run.action + " — " + step + " — " + fmt(elapsed) +
      " elapsed, last output " + fmt(since) + " ago\n" +
      (run.current_step || "").slice(0, 160);
  }
  stateEl.className = cls;
  outEl.className = cls;
  stateEl.textContent = text;
  const bugBtn = document.getElementById("bugBtn");
  // Always visible so the feature is discoverable; disabled
  // until there is a failed run to report.
  if (bugBtn) bugBtn.disabled = !(run.done && run.exit_code !== 0);
}

let currentId = null;
let lastRun = null;

// Shared "server unreachable" end state for the poll loop: used
// both when fetches fail and when they succeed with a body that
// is not JSON (a proxy or error page mid-deploy, never our API).
function lostConnection() {
  clearInterval(timer);
  stateEl.textContent = "connection lost — the buttons server may " +
    "be rebuilding itself; refresh this page in ~30s";
  stateEl.className = "fail";
  outEl.className = "fail";
  setBusy(false);
}

async function poll(id, action) {
  currentId = id;
  outEl.innerHTML = "";
  lastRenderedOutput = null;
  stateEl.textContent = "";
  stateEl.className = "";
  outEl.className = "";
  setBusy(true);
  clearInterval(timer);
  let failures = 0;
  timer = setInterval(async () => {
    let r;
    try {
      r = await fetch("/buttons/api/run/" + id);
    } catch (e) {
      r = null;
    }
    if (!r || !r.ok) {
      // Deploy buttons recreates its own container, so the run
      // record dies with the old container: the new container
      // answers 404 for the old id, or the fetch fails while the
      // page is down. That is a deploy in progress, not a run
      // failure — stop polling the dead run and poll the status
      // endpoint instead; when it answers, the new container is
      // serving, so reload into it. After 120s with no answer,
      // say so with the terminal command that shows the real
      // state, instead of leaving a stale "running" line.
      if (action === "deploy-buttons") {
        clearInterval(timer);
        stateEl.textContent = "Deploying — waiting for the new page to come up…";
        stateEl.className = "";
        let tries = 0;
        const recover = setInterval(async () => {
          tries++;
          try {
            const s = await fetch("/buttons/api/status");
            if (s.ok) { clearInterval(recover); location.reload(); return; }
          } catch (e) {}
          if (tries >= 40) {
            clearInterval(recover);
            stateEl.textContent = "Buttons has not come back after 120s — " +
              "from the terminal run: docker compose ps buttons";
            stateEl.className = "fail";
            setBusy(false);
          }
        }, 3000);
        return;
      }
      // Any other action: the buttons server itself is
      // unreachable. Say so and stop polling instead of spinning
      // forever with disabled buttons and a stale "running" line.
      failures++;
      if (failures >= 5) lostConnection();
      return;
    }
    failures = 0;
    let run = null;
    try { run = await r.json(); } catch (e) { run = null; }
    if (!run) {
      failures++;
      if (failures >= 5) lostConnection();
      return;
    }
    lastRun = run;
    // Re-render only on change: re-parsing a 200KB tail every poll
    // would churn on a phone for no visible difference.
    if (run.output !== lastRenderedOutput) {
      outEl.innerHTML = ansiToHtml(run.output);
      lastRenderedOutput = run.output;
    }
    outEl.scrollTop = outEl.scrollHeight;
    renderState(run);
    if (run.done) {
      clearInterval(timer);
      setBusy(false);
      // The ledger is append-only server-side; re-render it so a
      // finished run shows up without a manual Refresh tap.
      if (typeof refreshLedger === "function") refreshLedger();
      if (userStartedRun) {
        userStartedRun = false;
        openDrawer();
      }
      // A finished briefing-td run means the cards are stale:
      // refresh them so tapping Refresh briefing visibly updates
      // the view instead of leaving the reader to guess where
      // output went.
      if (run.action === "briefing-td" &&
          typeof refreshBriefing === "function") refreshBriefing();
      // Any finished run is a new ledger line.
      if (typeof refreshLedger === "function") refreshLedger();
      // A finished job step advances the job's plan.
      if (typeof jobStepDone === "function") jobStepDone(run);
    }
  }, 2000);
}

// Ticker between polls so elapsed / last-output age keeps moving
// even on a long silent step (model pulls, Pi briefings).
setInterval(() => { if (lastRun && !lastRun.done) renderState(lastRun); }, 1000);

// Clear output: clears the stored output server-side, so the next
// poll does not refill the pane with old lines. A running run keeps
// its progress line; only new lines appear after the clear. This
// button is outside #buttons on purpose, so it is never disabled
// while a run is in progress.
document.getElementById("clearBtn").onclick = async () => {
  const id = currentId || (lastRun && lastRun.id);
  if (id) {
    await fetch("/buttons/api/clear", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: id})});
  }
  outEl.innerHTML = "";
  lastRenderedOutput = null;
  if (!lastRun || lastRun.done) {
    stateEl.textContent = "output cleared";
    stateEl.className = "";
    outEl.className = "";
  }
};

// Report bug: file the failed run as a GitHub issue in the run's
// repo. The title/body are built from the run record (action,
// repo, exit code, extracted error line, output tail) and shown
// in a confirm dialog first; creation goes through the same
// validated /buttons/api/create-issue endpoint as Ask Pi
// proposals, so it is ledgered the same way.
async function reportBug() {
  const run = lastRun;
  if (!run || !run.done || run.exit_code === 0) return;
  const picker = document.getElementById("briefingRepo");
  const repo = run.repo || (picker ? picker.value : "remote-server");
  const errLine = (lastErrorLines() || "").trim() || "(no error line captured)";
  const tail = (run.output || "").slice(-2500);
  const title = ("[buttons] " + run.action + " failed on " + repo +
    " (exit " + run.exit_code + ")").slice(0, 200);
  const body = ("Run `" + run.action + "` on `" + repo +
    "` failed with exit " + run.exit_code + ".\n\nError:\n" +
    errLine + "\n\nOutput tail:\n```\n" + tail + "\n```").slice(0, 4000);
  if (!(await confirmDialog("Report bug as GitHub issue",
      "File this in " + repo + "?\n\n" + title + "\n\n" +
      errLine.slice(0, 300), false))) return;
  const btn = document.getElementById("bugBtn");
  if (btn) btn.disabled = true;
  try {
    const res = await fetchJson("/buttons/api/create-issue", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({repo: repo, title: title, body: body})});
    if (!res.ok) {
      await confirmDialog("Failed",
        "Could not create issue: " + (res.data.error || res.status), false);
      return;
    }
    await confirmDialog("Issue created", res.data.url || "created", false);
  } finally {
    if (btn) btn.disabled = !(run.done && run.exit_code !== 0);
  }
}
document.getElementById("bugBtn").onclick = reportBug;

</script>
<script>
// Briefing view (script block 2, separate from the core block
// above, so a Briefing failure cannot take the core buttons down).
// Renders the structured briefing from /buttons/api/briefing as
// PR cards. Auto-runs on load and after a Refresh briefing run;
// changing the picker above re-fetches.
const briefingEl = document.getElementById("briefing");
const briefingErrEl = document.getElementById("briefingErr");
// Per-PR actions: one tap per card, gated both client-side
// (which buttons render) and server-side (fresh state re-checked
// at tap time — the card may be minutes old). Merge is squash +
// delete-branch; Update is the GitHub update-branch API, BEHIND
// only; Fix CI dispatches a Jules session, it is not instant.
function prActionButton(pr, action, label, confirmText, cls, danger) {
  const b = document.createElement("button");
  b.textContent = label;
  if (cls) b.className = cls;
  b.onclick = async () => {
    if (!(await confirmDialog(label, confirmText, !!danger))) return;
    b.disabled = true;
    const card = b.closest(".prcard");
    const res = card ? card.querySelector(".prresult") : null;
    try {
      const picker = document.getElementById("briefingRepo");
      const body = {pr_number: pr.number, action: action,
        repo: picker ? picker.value : "tech-dancer"};
      const resp = await fetchJson("/buttons/api/pr-action", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)});
      const data = resp.data || {};
      if (!resp.ok) { if (res) res.textContent = "Failed: " + (data.error || ("HTTP " + resp.status)); b.disabled = false; return; }
      if (res) res.textContent = "Done. Refresh the briefing to see the new state.";
      if (typeof refreshLedger === "function") refreshLedger();
    } catch (e) {
      if (res) res.textContent = "Failed: " + e;
      b.disabled = false;
    }
  };
  return b;
}
function renderBriefing(b) {
  briefingErrEl.textContent = "";
  briefingEl.textContent = "";
  if (b.prs_err) briefingErrEl.textContent = "Open PR list unavailable: " + b.prs_err;
  const att = document.createElement("div");
  const attHead = document.createElement("b");
  attHead.textContent = "Needs attention";
  att.appendChild(attHead);
  if (b.attention && b.attention.length) {
    const ul = document.createElement("ul");
    b.attention.forEach(a => { const li = document.createElement("li"); li.textContent = a; ul.appendChild(li); });
    att.appendChild(ul);
  } else {
    const p = document.createElement("p"); p.textContent = "Nothing needs attention."; att.appendChild(p);
  }
  briefingEl.appendChild(att);
  (b.prs || []).forEach(pr => {
    const card = document.createElement("div");
    card.className = "prcard";
    const head = document.createElement("div");
    head.className = "prhead";
    const num = document.createElement("span");
    num.className = "prnum";
    num.textContent = "#" + pr.number;
    const cdot = document.createElement("span");
    cdot.className = "dot " + (pr.ci === "passing" ? "ok" : pr.ci === "failing" ? "fail" : "");
    cdot.title = "CI: " + pr.ci;
    const title = document.createElement("span");
    title.className = "prtitle";
    if (pr.url) {
      const link = document.createElement("a");
      link.href = pr.url; link.textContent = pr.title;
      title.appendChild(link);
    } else {
      title.textContent = pr.title;
    }
    head.appendChild(num); head.appendChild(cdot); head.appendChild(title);
    card.appendChild(head);
    const chips = document.createElement("div");
    chips.className = "chips";
    const chip = (t, cls) => {
      const s = document.createElement("span");
      s.className = "chip" + (cls ? " " + cls : "");
      s.textContent = t;
      chips.appendChild(s);
    };
    chip(pr.branch);
    chip("CI " + pr.ci, pr.ci === "passing" ? "chip-ok" : pr.ci === "failing" ? "chip-fail" : "");
    chip("merge " + pr.merge, pr.merge === "clean" ? "chip-ok" : pr.merge === "behind" ? "chip-warn" : "");
    if (pr.conflicted) chip("CONFLICTED", "chip-fail");
    if (pr.draft) chip("draft");
    if (pr.stale) chip("stale", "chip-warn");
    chip(pr.updated);
    card.appendChild(chips);
    if (pr.failing && pr.failing.length) {
      const fl = document.createElement("div");
      fl.className = "prmeta";
      fl.textContent = "failing: " + pr.failing.map(f => shorten(String(f), 160)).join("; ");
      card.appendChild(fl);
    }
    if (pr.verdicts && pr.verdicts.length) {
      const rev = document.createElement("div"); rev.className = "prmeta";
      rev.textContent = "Reviews: " + pr.verdicts.map(v => "@" + v.user + " " + v.state).join("; ");
      card.appendChild(rev);
    }
    (pr.reviews_picked || []).forEach(r => {
      const ex = document.createElement("div"); ex.className = "prmeta";
      ex.textContent = "@" + r.user + " " + r.state + (r.body ? " — '" + r.body + "'" : "");
      card.appendChild(ex);
    });
    const actions = document.createElement("div");
    actions.className = "practions";
    actions.setAttribute("data-pr", pr.number);
    if (pr.ci === "passing" && pr.merge === "clean" && !pr.draft)
      actions.appendChild(prActionButton(pr, "merge", "Merge (squash)",
        "Merge PR #" + pr.number + " (squash, delete branch)?", "btn-primary", true));
    if (pr.merge === "behind" && !pr.draft)
      actions.appendChild(prActionButton(pr, "update", "Update branch",
        "Update PR #" + pr.number + " branch to the base branch?", "btn-ghost", false));
    if (pr.ci === "failing" && !pr.draft)
      actions.appendChild(prActionButton(pr, "fix-ci", "Fix CI with Jules",
        "Dispatch a Jules session to fix CI on PR #" + pr.number + "? This creates an agent session, it is not instant.",
        "btn-primary", false));
    card.appendChild(actions);
    const result = document.createElement("div");
    result.className = "prresult";
    card.appendChild(result);
    briefingEl.appendChild(card);
  });
  // Long job/step strings (a Dependabot run dumps dozens of
  // package names) blow out the card height: cap one segment
  // and the whole line.
  function shorten(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  if (b.failing_runs && b.failing_runs.length) {
    const w = document.createElement("div");
    const wHead = document.createElement("b"); wHead.textContent = "Failing runs"; w.appendChild(wHead);
    b.failing_runs.forEach(run => {
      const p = document.createElement("p");
      const failed = (run.failed || []).map(f =>
        shorten(f.job + (f.steps && f.steps.length ? " (" + f.steps.join(", ") + ")" : ""), 160)
      ).join("; ");
      p.textContent = shorten(
        run.name + " on " + run.branch + " (" + run.event + ")" +
        (failed ? " — failed: " + failed : ""), 600);
      w.appendChild(p);
    });
    briefingEl.appendChild(w);
  }
  const j = b.jules || {};
  const js = document.createElement("div");
  const jHead = document.createElement("b"); jHead.textContent = "Agent sessions (Jules)"; js.appendChild(jHead);
  if (j.unavailable) {
    const p = document.createElement("p"); p.textContent = "Jules sessions unavailable: " + j.unavailable; js.appendChild(p);
  } else if (j.items && j.items.length) {
    j.items.forEach(t => { const p = document.createElement("p"); p.textContent = t.replace(/^- /, ""); js.appendChild(p); });
  } else {
    const p = document.createElement("p"); p.textContent = "No active Jules sessions for " + (b.repo || "this repo") + "."; js.appendChild(p);
  }
  if (j.other) { const p = document.createElement("p"); p.textContent = "(" + j.other + " active session(s) for other repos, not listed.)"; js.appendChild(p); }
  briefingEl.appendChild(js);
}
async function refreshBriefing() {
  const picker = document.getElementById("briefingRepo");
  const repo = picker ? picker.value : "tech-dancer";
  briefingEl.innerHTML = '<span class="spinner"></span> Loading briefing…';
  const res = await fetchJson("/buttons/api/briefing?repo=" + encodeURIComponent(repo));
  if (!res.ok) {
    briefingEl.textContent = "";
    briefingErrEl.textContent = "briefing unavailable: " + (res.data.error || ("HTTP " + res.status));
    const att = document.getElementById("attentionBody");
    if (att) {
      att.textContent = "";
      const span = document.createElement("span");
      span.textContent = "Briefing unavailable — ";
      att.appendChild(span);
      const rb = document.createElement("button");
      rb.textContent = "Retry";
      rb.style.width = "auto";
      rb.onclick = () => refreshBriefing();
      att.appendChild(rb);
    }
    return;
  }
  briefingErrEl.textContent = "";
  renderBriefing(res.data);
  briefingPrs = res.data.pr_list || [];
  if (typeof refreshJobPrSelect === "function") refreshJobPrSelect();
  if (typeof renderPlanPrSelects === "function") renderPlanPrSelects();
  if (typeof renderAttention === "function") renderAttention(res.data);
}

// Status-first: the Actions tab leads with what needs attention —
// failing runs and attention items from the latest briefing. Runs
// whenever the briefing refreshes, so the top of the default tab
// always reflects the freshest state.
function renderAttention(b) {
  const el = document.getElementById("attentionBody");
  if (!el) return;
  el.textContent = "";
  const items = [];
  (b.failing_runs || []).forEach(run => {
    items.push("Failing run: " + run.name + " on " + run.branch);
  });
  (b.attention || []).forEach(a => items.push(a));
  if (!items.length) {
    el.textContent = "All clear — nothing needs attention.";
    return;
  }
  const head = document.createElement("div");
  head.className = "attn-head";
  head.textContent = "Needs attention";
  el.appendChild(head);
  const ul = document.createElement("ul");
  items.slice(0, 8).forEach(t => {
    const li = document.createElement("li");
    li.textContent = t;
    ul.appendChild(li);
  });
  el.appendChild(ul);
}

// Agent jobs: multi-step tasks with a visible plan. Read-only
// steps run on their own; only write steps wait for an Approve /
// Decline tap. Approving a write step runs it through the existing
// run machinery (output in the drawer); declining ends the job.
// Every terminal job writes an audit record.
const JOB_TEMPLATES = {
  "triage": {label: "Triage repo",
    desc: "Briefing → warm context → preflight. Runs on its own."},
  "fix-ci": {label: "Fix CI on a PR",
    desc: "Dispatch a Jules session to fix a failing PR.", needsPr: true},
  "fix-issues": {label: "Fix issues via Jules",
    desc: "Pick issues; each gets one Approve tap, which labels it " +
      "'jules' and starts a Jules session. Sequential.",
    needsIssues: true},
  "nudge-agents": {label: "Nudge agents",
    desc: "Preview the session list, then Approve to post feedback " +
      "to stalled sessions."},
};
let pendingJobStep = null; // {job, step, runId} while a step run polls
let briefingPrs = []; // open PRs from the latest briefing, for the PR dropdown
// Open-PR dropdown for jobs that need one (fix-ci): rebuilt from the
// latest briefing's PR list as "#number — title", not a blind number
// field. Rebuilt whenever the briefing refreshes (repo switch incl.).
function refreshJobPrSelect(sel) {
  sel = sel || document.getElementById("jobPrSelect");
  if (!sel) return;
  sel.textContent = "";
  if (!briefingPrs.length) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "No open PRs";
    sel.appendChild(o);
    return;
  }
  briefingPrs.forEach(pr => {
    const o = document.createElement("option");
    o.value = pr.number;
    o.textContent = "#" + pr.number + " — " + (pr.title || "");
    sel.appendChild(o);
  });
}
function jobErr(t) {
  const e = document.getElementById("jobErr");
  if (e) e.textContent = t;
}
function renderJobNew() {
  const el = document.getElementById("jobNew");
  if (!el) return;
  el.textContent = "";
  Object.entries(JOB_TEMPLATES).forEach(([name, t]) => {
    const d = document.createElement("div");
    d.className = "jobtmpl";
    const l = document.createElement("div");
    l.className = "blabel"; l.textContent = t.label;
    const s = document.createElement("div");
    s.className = "bsub"; s.textContent = t.desc;
    d.appendChild(l); d.appendChild(s);
    let prSelect = null;
    if (t.needsPr) {
      prSelect = document.createElement("select");
      prSelect.id = "jobPrSelect";
      prSelect.style.marginRight = "0.5em";
      prSelect.style.maxWidth = "100%";
      d.appendChild(prSelect);
      // Populate directly: the card is still detached here, so a
      // getElementById lookup would find nothing and leave the
      // select empty (the "No open PRs" option included).
      refreshJobPrSelect(prSelect);
    }
    // Issue checkboxes for jobs that need them (fix-issues):
    // fetched from the open-issues endpoint for the picked repo.
    let issueBox = null;
    if (t.needsIssues) {
      issueBox = document.createElement("div");
      issueBox.className = "issuepick";
      issueBox.textContent = "Loading issues…";
      d.appendChild(issueBox);
      const picker = document.getElementById("briefingRepo");
      const repo = picker ? picker.value : "tech-dancer";
      fetchJson("/buttons/api/issues?repo=" + encodeURIComponent(repo))
        .then(res => {
          issueBox.textContent = "";
          const issues = (res.data && res.data.issues) || [];
          if (!res.ok || !issues.length) {
            issueBox.textContent = res.ok ? "No open issues."
              : "Issues unavailable: " + (res.data.error || res.status);
            return;
          }
          issues.forEach(i => {
            const lab = document.createElement("label");
            const cb = document.createElement("input");
            cb.type = "checkbox"; cb.value = i.number;
            lab.appendChild(cb);
            lab.appendChild(document.createTextNode(
              " #" + i.number + " — " + (i.title || "")));
            issueBox.appendChild(lab);
          });
        });
    }
    const b = document.createElement("button");
    b.textContent = "Start job";
    b.onclick = async () => {
      const picker = document.getElementById("briefingRepo");
      const body = {template: name, repo: picker ? picker.value : "tech-dancer"};
      if (t.needsPr) {
        const n = parseInt(prSelect.value, 10);
        if (!(n > 0)) { jobErr("Pick an open PR."); return; }
        body.pr = n;
      }
      if (t.needsIssues) {
        const nums = [...issueBox.querySelectorAll("input:checked")]
          .map(cb => parseInt(cb.value, 10)).filter(n => n > 0);
        if (!nums.length) { jobErr("Pick at least one issue."); return; }
        body.issues = nums;
      }
      const res = await fetchJson("/buttons/api/jobs", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)});
      if (!res.ok) {
        jobErr("Could not start job: " + (res.data.error || res.status));
        return;
      }
      jobErr("");
      refreshJobs();
    };
    d.appendChild(b);
    el.appendChild(d);
  });
}
async function refreshJobs() {
  const listEl = document.getElementById("jobList");
  const errEl = document.getElementById("jobListErr");
  if (!listEl) return;
  const res = await fetchJson("/buttons/api/jobs");
  if (!res.ok) {
    if (errEl) errEl.textContent = "jobs unavailable: " + (res.data.error || res.status);
    return;
  }
  if (errEl) errEl.textContent = "";
  listEl.textContent = "";
  const jobs = res.data.jobs || [];
  if (!jobs.length) {
    listEl.textContent = "No jobs yet. Start one above.";
    return;
  }
  jobs.forEach(j => listEl.appendChild(renderJobCard(j)));
  // Read-only steps start on the server without a tap. If one is
  // running and we aren't polling its run yet, pick it up so its
  // output streams to the drawer and step-done gets reported.
  if (!pendingJobStep) {
    outer:
    for (const j of jobs) {
      for (let i = 0; i < j.steps.length; i++) {
        const s = j.steps[i];
        if (s.state === "running" && s.run_id) {
          pendingJobStep = {job: j.id, step: i, runId: s.run_id};
          openDrawer();
          if (typeof poll === "function") poll(s.run_id, "job-step");
          break outer;
        }
      }
    }
  }
}
function renderJobCard(j) {
  const card = document.createElement("div");
  card.className = "jobcard";
  const g = document.createElement("div");
  g.className = "jobgoal";
  g.textContent = j.goal;
  card.appendChild(g);
  const m = document.createElement("div");
  m.className = "jobmeta";
  const done = j.steps.filter(s => s.state === "done").length;
  m.textContent = j.repo + " · " + done + "/" + j.steps.length + " steps" +
    (j.state === "terminal" ? " · " + j.outcome : " · active");
  card.appendChild(m);
  j.steps.forEach((s, i) => {
    const row = document.createElement("div");
    row.className = "jobstep";
    const badge = document.createElement("span");
    badge.className = "st " + s.state;
    badge.textContent = s.state;
    const lab = document.createElement("span");
    lab.textContent = s.label;
    row.appendChild(badge);
    row.appendChild(lab);
    if (j.state === "active" && s.state === "pending" &&
        j.steps.slice(0, i).every(p => p.state === "done")) {
      const acts = document.createElement("span");
      acts.className = "sactions";
      const ok = document.createElement("button");
      ok.textContent = "Approve";
      ok.onclick = () => jobApprove(j.id, i);
      const no = document.createElement("button");
      no.textContent = "Decline";
      no.className = "btn-ghost";
      no.onclick = () => jobDecline(j.id, i);
      acts.appendChild(ok); acts.appendChild(no);
      row.appendChild(acts);
    }
    card.appendChild(row);
  });
  return card;
}
async function jobApprove(id, step) {
  jobErr("");
  const res = await fetchJson("/buttons/api/jobs/" + id + "/approve",
    {method: "POST", headers: {"Content-Type": "application/json"},
     body: JSON.stringify({step})});
  if (!res.ok) {
    jobErr("Approve failed: " + (res.data.error || res.status));
    return;
  }
  if (res.data.run_id) {
    pendingJobStep = {job: id, step: step, runId: res.data.run_id};
    openDrawer();
    if (typeof poll === "function") poll(res.data.run_id, "job-step");
  }
  refreshJobs();
}
async function jobDecline(id, step) {
  const ok = await confirmDialog("Decline step",
    "Decline this step and end the job? The audit record is still written.",
    false);
  if (!ok) return;
  const res = await fetchJson("/buttons/api/jobs/" + id + "/decline",
    {method: "POST", headers: {"Content-Type": "application/json"},
     body: JSON.stringify({step})});
  if (!res.ok) {
    jobErr("Decline failed: " + (res.data.error || res.status));
    return;
  }
  refreshJobs();
  if (typeof refreshLedger === "function") refreshLedger();
}
// Called from the poll loop when a run finishes: if it was a job
// step, report completion so the server advances the plan.
async function jobStepDone(run) {
  if (!pendingJobStep || run.id !== pendingJobStep.runId) return;
  const sj = pendingJobStep;
  pendingJobStep = null;
  const res = await fetchJson("/buttons/api/jobs/" + sj.job + "/step-done",
    {method: "POST", headers: {"Content-Type": "application/json"},
     body: JSON.stringify({step: sj.step, run_id: sj.runId})});
  if (!res.ok) jobErr("Step completion not recorded: " + (res.data.error || res.status));
  refreshJobs();
  if (typeof refreshLedger === "function") refreshLedger();
}
renderJobNew();
refreshJobs();
const briefingRepoPicker = document.getElementById("briefingRepo");
if (briefingRepoPicker) briefingRepoPicker.onchange = refreshBriefing;
refreshBriefing();

// Suggested next actions: Pi suggests td commands from the
// briefing context; each suggestion runs only on its own
// confirm tap, through /api/run-td (which re-validates).
const suggestBtn = document.getElementById("suggestBtn");
const suggestionsEl = document.getElementById("suggestions");
const suggestErrEl = document.getElementById("suggestErr");
if (suggestBtn) suggestBtn.onclick = async () => {
  const picker = document.getElementById("briefingRepo");
  const repo = picker ? picker.value : "tech-dancer";
  suggestErrEl.textContent = "";
  suggestionsEl.textContent = "Asking Pi (local model, may take minutes)…";
  try {
    const r = await fetch("/buttons/api/suggest", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({repo: repo})});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { suggestionsEl.textContent = ""; suggestErrEl.textContent = "suggest failed: " + (data.error || r.status); return; }
    suggestionsEl.textContent = "";
    if (!data.suggestions || !data.suggestions.length) {
      suggestionsEl.textContent = "Pi suggested nothing runnable.";
      if (data.raw) {
        const pre = document.createElement("pre");
        pre.textContent = data.raw;
        suggestionsEl.appendChild(pre);
      }
      return;
    }
    data.suggestions.forEach(s => {
      const div = document.createElement("div");
      div.className = "prcard";
      const reason = document.createElement("div");
      reason.textContent = s.reason || s.command;
      div.appendChild(reason);
      const code = document.createElement("div");
      const c = document.createElement("code");
      c.textContent = s.command;
      code.appendChild(c);
      div.appendChild(code);
      if (s.caveat) {
        const cav = document.createElement("div");
        cav.className = "prmeta";
        cav.textContent = s.caveat;
        div.appendChild(cav);
      }
      const b = document.createElement("button");
      b.textContent = "Run";
      b.onclick = async () => {
        if (!(await confirmDialog("Run suggested command", "Run: " + s.command + "?", false))) return;
        b.disabled = true;
        const rr = await fetch("/buttons/api/run-td", {method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({repo: repo, command: s.command})});
        const dd = await rr.json().catch(() => ({}));
        if (!rr.ok) { suggestErrEl.textContent = "run failed: " + (dd.error || rr.status); b.disabled = false; return; }
        userStartedRun = true;
        poll(dd.id, "run-td");
      };
      div.appendChild(b);
      suggestionsEl.appendChild(div);
    });
  } catch (e) {
    suggestionsEl.textContent = "";
    suggestErrEl.textContent = "suggest failed: " + e;
  }
};

// Ask Pi: open-ended instruction text box. Pi investigates
// read-only and replies inline; a write it was asked for comes
// back as a PROPOSE block (parsed server-side). td proposals run
// through /api/run-td like suggestion Run buttons; issue
// proposals go through /api/create-issue after a confirm dialog
// showing the exact title and body.
const askBtn = document.getElementById("askBtn");
const askBox = document.getElementById("askBox");
const askOut = document.getElementById("askOut");
const askErr = document.getElementById("askErr");
if (askBtn) askBtn.onclick = async () => {
  const picker = document.getElementById("briefingRepo");
  const repo = picker ? picker.value : "tech-dancer";
  const instruction = askBox.value.trim();
  if (!instruction) { askErr.textContent = "Type an instruction first."; return; }
  askErr.textContent = "";
  askOut.textContent = "Asking Pi (local model, may take minutes)…";
  askBtn.disabled = true;
  try {
    const r = await fetch("/buttons/api/ask", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({repo: repo, instruction: instruction})});
    const data = await r.json().catch(() => ({}));
    askBtn.disabled = false;
    if (!r.ok) { askOut.textContent = ""; askErr.textContent = "ask failed: " + (data.error || r.status); return; }
    askOut.textContent = "";
    const pre = document.createElement("pre");
    pre.textContent = data.reply || "(Pi said nothing)";
    askOut.appendChild(pre);
    const p = data.proposal;
    if (!p) return;
    const div = document.createElement("div");
    div.className = "proposal";
    const title = document.createElement("div");
    title.className = "ptitle";
    const b = document.createElement("button");
    if (p.kind === "issue") {
      title.textContent = "Proposed issue: " + p.title;
      div.appendChild(title);
      const bodyPre = document.createElement("pre");
      bodyPre.textContent = p.body || "(no body)";
      div.appendChild(bodyPre);
      b.textContent = "Create issue";
      b.onclick = async () => {
        if (!(await confirmDialog("Create GitHub issue",
            "Create this issue in " + repo + "?\n\n" + p.title, false))) return;
        b.disabled = true;
        const rr = await fetch("/buttons/api/create-issue", {method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({repo: repo, title: p.title, body: p.body})});
        const dd = await rr.json().catch(() => ({}));
        if (!rr.ok) { askErr.textContent = "create failed: " + (dd.error || rr.status); b.disabled = false; return; }
        title.textContent = "Issue created: " + (dd.url || "ok");
        b.remove();
      };
    } else {
      title.textContent = "Proposed command:";
      div.appendChild(title);
      const code = document.createElement("code");
      code.textContent = p.command;
      div.appendChild(code);
      b.textContent = "Run";
      b.onclick = async () => {
        if (!(await confirmDialog("Run proposed command", "Run: " + p.command + "?", false))) return;
        b.disabled = true;
        const rr = await fetch("/buttons/api/run-td", {method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({repo: repo, command: p.command})});
        const dd = await rr.json().catch(() => ({}));
        if (!rr.ok) { askErr.textContent = "run failed: " + (dd.error || rr.status); b.disabled = false; return; }
        userStartedRun = true;
        poll(dd.id, "run-td");
      };
    }
    div.appendChild(b);
    askOut.appendChild(div);
  } catch (e) {
    askBtn.disabled = false;
    askOut.textContent = "";
    askErr.textContent = "ask failed: " + e;
  }
};

// Ledger view. Entry data is rendered with textContent only —
// never innerHTML — so a log line can never inject markup.
// Output tails hide in <details> so the list stays scannable.
const ledgerEl = document.getElementById("ledger");
const ledgerErrEl = document.getElementById("ledgerErr");
const ledgerKindEl = document.getElementById("ledgerKind");
function ledgerSummary(e) {
  if (e.kind === "run") return "run " + e.action + (e.repo ? " (" + e.repo + ")" : "") + " — exit " + e.exit;
  if (e.kind === "webhook") return "webhook " + e.repo + " — " + (e.ok ? "pulled" : "FAILED") + (e.advanced ? " (advanced)" : "") + ": " + (e.detail || "");
  if (e.kind === "pr-action") return "pr-action " + e.action + (e.pr ? " #" + e.pr : "") + (e.repo ? " (" + e.repo + ")" : "") + " — " + (e.ok ? "ok" : "refused/failed") + (e.note ? " — " + e.note : "");
  if (e.kind === "system") return "system: " + (e.event || "");
  if (e.kind === "job") {
    const label = e.template || (e.job || "").slice(0, 8);
    const step = (typeof e.step === "number") ? " · step " + (e.step + 1) : "";
    const what = e.event || (e.outcome ? "ended: " + e.outcome : "");
    return "job " + label + (e.repo ? " (" + e.repo + ")" : "") + step +
      (what ? " — " + what : "");
  }
  return "unknown entry";
}
async function refreshLedger() {
  if (!ledgerEl) return;
  const kind = ledgerKindEl ? ledgerKindEl.value : "all";
  ledgerEl.textContent = "Loading ledger…";
  try {
    const res = await fetchJson("/buttons/api/ledger?kind=" + encodeURIComponent(kind) + "&limit=50");
    if (!res.ok) { ledgerEl.textContent = ""; ledgerErrEl.textContent = "ledger unavailable: " + (res.data.error || ("HTTP " + res.status)); return; }
    const data = res.data || {};
    ledgerErrEl.textContent = "";
    ledgerEl.textContent = "";
    const entries = data.entries || [];
    if (!entries.length) { ledgerEl.textContent = "No ledger entries yet."; return; }
    entries.forEach(e => {
      const row = document.createElement("div");
      const head = document.createElement("div");
      head.textContent = new Date(e.ts * 1000).toLocaleString() + " — " + ledgerSummary(e);
      row.appendChild(head);
      const tail = e.output || e.error || "";
      if (tail) {
        const det = document.createElement("details");
        const sum = document.createElement("summary");
        sum.textContent = e.error ? "error" : "output tail";
        det.appendChild(sum);
        const pre = document.createElement("pre");
        pre.textContent = tail;
        det.appendChild(pre);
        row.appendChild(det);
      }
      ledgerEl.appendChild(row);
    });
  } catch (e) {
    ledgerEl.textContent = "";
    ledgerErrEl.textContent = "ledger unavailable: " + e;
  }
}
const refreshLedgerBtn = document.getElementById("refreshLedger");
if (refreshLedgerBtn) refreshLedgerBtn.onclick = refreshLedger;
const clearLedgerBtn = document.getElementById("clearLedger");
if (clearLedgerBtn) clearLedgerBtn.onclick = async () => {
  if (!(await confirmDialog("Clear ledger", "All entries will be deleted except a 'ledger cleared' record. This cannot be undone.", true))) return;
  const r = await fetch("/buttons/api/ledger/clear", {method: "POST"});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { ledgerErrEl.textContent = "ledger clear failed: " + (data.error || r.status); return; }
  refreshLedger();
};
if (ledgerKindEl) ledgerKindEl.onchange = refreshLedger;
refreshLedger();

// Pi session browser. List renders each session file as a
// button; tapping one renders that session readably below.
const piSessionListEl = document.getElementById("piSessionList");
const piSessionOutEl = document.getElementById("piSessionOut");
const piSessionErrEl = document.getElementById("piSessionErr");
async function showSession(file) {
  piSessionOutEl.textContent = "Loading session…";
  try {
    const res = await fetchJson("/buttons/api/pi-session?session=" + encodeURIComponent(file));
    const data = res.data || {};
    if (!res.ok) { piSessionOutEl.textContent = ""; piSessionErrEl.textContent = "session unavailable: " + (data.error || ("HTTP " + res.status)); return; }
    piSessionErrEl.textContent = "";
    piSessionOutEl.textContent = data.output;
  } catch (e) {
    piSessionOutEl.textContent = "";
    piSessionErrEl.textContent = "session unavailable: " + e;
  }
}
const clearPiSessionsBtn = document.getElementById("clearPiSessions");
if (clearPiSessionsBtn) clearPiSessionsBtn.onclick = async () => {
  if (!(await confirmDialog("Clear Pi sessions", "This deletes the transcripts and cannot be undone.", true))) return;
  const r = await fetch("/buttons/api/pi-sessions/clear", {method: "POST"});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { piSessionErrEl.textContent = "clear failed: " + (data.error || r.status); return; }
  piSessionListEl.textContent = "";
  piSessionOutEl.textContent = "";
  piSessionErrEl.textContent = data.output || "Pi sessions cleared.";
};
const listPiSessionsBtn = document.getElementById("listPiSessions");
if (listPiSessionsBtn) listPiSessionsBtn.onclick = async () => {
  piSessionListEl.textContent = "Loading sessions…";
  try {
    const res = await fetchJson("/buttons/api/pi-sessions");
    const data = res.data || {};
    if (!res.ok) { piSessionListEl.textContent = ""; piSessionErrEl.textContent = "sessions unavailable: " + (data.error || ("HTTP " + res.status)); return; }
    piSessionErrEl.textContent = "";
    piSessionListEl.textContent = "";
    const sessions = data.sessions || [];
    if (!sessions.length) { piSessionListEl.textContent = "No Pi sessions found."; return; }
    sessions.forEach(s => {
      const row = document.createElement("div");
      row.className = "pirow";
      const b = document.createElement("button");
      const when = s.mtime ? new Date(s.mtime * 1000).toLocaleString() : "";
      const repo = s.cwd ? s.cwd.split("/").filter(Boolean).pop() : "";
      // Never show the raw filename in the label: date, repo,
      // first prompt, or "untitled session".
      b.textContent = [when, repo, (s.title || "untitled session")]
        .filter(Boolean).join(" · ");
      b.onclick = () => showSession(s.file);
      const del = document.createElement("button");
      del.textContent = "✕";
      del.className = "pirowdel";
      del.title = "Delete this session";
      del.onclick = async (e) => {
        e.stopPropagation();
        if (!(await confirmDialog("Delete Pi session",
            "Delete this transcript? It cannot be undone.", true)))
          return;
        const r = await fetchJson("/buttons/api/pi-sessions/delete",
          {method: "POST", headers: {"Content-Type": "application/json"},
           body: JSON.stringify({session: s.file})});
        if (!r.ok) {
          piSessionErrEl.textContent = "delete failed: " +
            (r.data.error || r.status);
          return;
        }
        piSessionErrEl.textContent = r.data.output || "Deleted.";
        row.remove();
      };
      row.appendChild(b); row.appendChild(del);
      piSessionListEl.appendChild(row);
    });
  } catch (e) {
    piSessionListEl.textContent = "";
    piSessionErrEl.textContent = "sessions unavailable: " + e;
  }
};

// Project tab: issues, Jules sessions, td planning guides.
// Reads the repo from the picker at tap time, like the other tabs.
function projectRepo() {
  const p = document.getElementById("briefingRepo");
  return p ? p.value : "remote-server";
}
function renderPlanPrSelects() {
  const single = document.getElementById("planPrSelect");
  if (single) {
    single.innerHTML = '<option value="">—</option>';
    (briefingPrs || []).forEach(pr => {
      const o = document.createElement("option");
      o.value = pr.number;
      o.textContent = "#" + pr.number + " — " +
        String(pr.title || "").slice(0, 60);
      single.appendChild(o);
    });
  }
  const multi = document.getElementById("planAggPrs");
  if (multi) {
    multi.innerHTML = "";
    (briefingPrs || []).forEach(pr => {
      const o = document.createElement("option");
      o.value = pr.number;
      o.textContent = "#" + pr.number + " — " +
        String(pr.title || "").slice(0, 40);
      multi.appendChild(o);
    });
  }
}
async function refreshIssues() {
  const el = document.getElementById("issues");
  const err = document.getElementById("issuesErr");
  if (!el) return;
  el.innerHTML = '<span class="spinner"></span> Loading issues…';
  err.textContent = "";
  const res = await fetchJson("/buttons/api/issues?repo=" +
    encodeURIComponent(projectRepo()));
  const data = res.data || {};
  if (!res.ok) {
    el.innerHTML = "";
    err.textContent = "issues unavailable: " + (data.error || res.status);
    return;
  }
  el.innerHTML = "";
  const issues = data.issues || [];
  if (!issues.length) {
    el.textContent = "No open issues on " + projectRepo() + ".";
    return;
  }
  issues.forEach(iss => {
    const card = document.createElement("div");
    card.className = "prcard";
    const title = document.createElement("div");
    title.className = "prtitle";
    const a = document.createElement("a");
    a.href = iss.url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "#" + iss.number + " " + iss.title;
    title.appendChild(a);
    card.appendChild(title);
    const meta = document.createElement("div");
    meta.className = "prmeta";
    const who = (iss.assignees || []).map(x => x.login).join(", ") ||
      "unassigned";
    const labs = (iss.labels || []).map(x => x.name).join(", ");
    let when = "";
    try { when = new Date(iss.updatedAt).toLocaleString(); } catch (e) {}
    meta.textContent = who + (labs ? " · " + labs : "") +
      (when ? " · updated " + when : "");
    card.appendChild(meta);
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "Details / assign";
    det.appendChild(sum);
    const pre = document.createElement("pre");
    pre.textContent = iss.body || "(no body)";
    det.appendChild(pre);
    const row = document.createElement("div");
    const inp = document.createElement("input");
    inp.placeholder = "assignee username";
    inp.size = 18;
    inp.maxLength = 39;
    const btn = document.createElement("button");
    btn.textContent = "Assign";
    btn.onclick = async () => {
      const name = inp.value.trim();
      if (!name) return;
      if (!(await confirmDialog("Assign issue",
          "Assign #" + iss.number + " to " + name + "?", false))) return;
      btn.disabled = true;
      const r = await fetchJson("/buttons/api/assign-issue", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({repo: projectRepo(), issue: iss.number,
                              assignee: name})});
      btn.disabled = false;
      if (!r.ok) {
        err.textContent = "assign failed: " +
          (((r.data || {}).error) || r.status);
        return;
      }
      refreshIssues();
    };
    row.appendChild(inp);
    row.appendChild(btn);
    det.appendChild(row);
    card.appendChild(det);
    el.appendChild(card);
  });
}
async function createProjectIssue() {
  const tEl = document.getElementById("newIssueTitle");
  const bEl = document.getElementById("newIssueBody");
  const err = document.getElementById("newIssueErr");
  const title = (tEl.value || "").trim();
  const repo = projectRepo();
  if (!title) { err.textContent = "Title is required."; return; }
  err.textContent = "";
  if (!(await confirmDialog("Create GitHub issue",
      "Create this issue in " + repo + "?\n\n" + title, false))) return;
  const btn = document.getElementById("createIssueBtn");
  btn.disabled = true;
  try {
    const res = await fetchJson("/buttons/api/create-issue", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({repo: repo, title: title,
                            body: bEl.value || ""})});
    const data = res.data || {};
    if (!res.ok) {
      err.textContent = "create failed: " + (data.error || res.status);
      return;
    }
    tEl.value = "";
    bEl.value = "";
    await confirmDialog("Issue created", data.url || "created", false);
    refreshIssues();
  } finally {
    btn.disabled = false;
  }
}
async function refreshJulesSessions() {
  const el = document.getElementById("julesSessions");
  const err = document.getElementById("julesErr");
  if (!el) return;
  el.innerHTML = '<span class="spinner"></span> Loading Jules sessions…';
  err.textContent = "";
  const res = await fetchJson("/buttons/api/jules-sessions?repo=" +
    encodeURIComponent(projectRepo()));
  const data = res.data || {};
  if (!res.ok) {
    el.innerHTML = "";
    err.textContent = "jules unavailable: " + (data.error || res.status);
    return;
  }
  el.innerHTML = "";
  if (data.notice) {
    const n = document.createElement("p");
    n.textContent = data.notice;
    el.appendChild(n);
  }
  const ss = data.sessions || [];
  if (!ss.length) {
    el.appendChild(document.createTextNode("No Jules sessions."));
    return;
  }
  ss.forEach(s => {
    const card = document.createElement("div");
    card.className = "prcard";
    const title = document.createElement("div");
    title.className = "prtitle";
    if (s.url) {
      const a = document.createElement("a");
      a.href = s.url; a.target = "_blank"; a.rel = "noopener";
      a.textContent = s.title || s.id;
      title.appendChild(a);
    } else {
      title.textContent = s.title || s.id;
    }
    card.appendChild(title);
    const meta = document.createElement("div");
    meta.className = "prmeta";
    let when = "";
    try { when = new Date(s.updated).toLocaleString(); } catch (e) {}
    meta.textContent = (s.state || "?") +
      (s.source ? " · " + s.source : "") +
      (when ? " · updated " + when : "");
    card.appendChild(meta);
    if (s.pr_url) {
      const pr = document.createElement("div");
      const a = document.createElement("a");
      a.href = s.pr_url; a.target = "_blank"; a.rel = "noopener";
      a.textContent = "PR: " + s.pr_url.split("/").pop();
      pr.appendChild(a);
      card.appendChild(pr);
    }
    el.appendChild(card);
  });
}
async function runPlan(plan, extra) {
  const out = document.getElementById("planOut");
  const err = document.getElementById("planErr");
  out.innerHTML = '<span class="spinner"></span> Generating plan…';
  err.textContent = "";
  const res = await fetchJson("/buttons/api/run-plan", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(Object.assign({repo: projectRepo(), plan: plan},
                                      extra || {}))});
  const data = res.data || {};
  if (!res.ok) {
    out.innerHTML = "";
    err.textContent = "plan failed: " + (data.error || res.status);
    return;
  }
  out.innerHTML = "";
  if (data.plan_path) {
    const p = document.createElement("p");
    p.textContent = "Plan written to: " + data.plan_path;
    out.appendChild(p);
  }
  const pre = document.createElement("pre");
  pre.textContent = data.output || "(empty)";
  out.appendChild(pre);
}
(function wireProjectTab() {
  const ri = document.getElementById("refreshIssues");
  if (ri) ri.onclick = refreshIssues;
  const ci = document.getElementById("createIssueBtn");
  if (ci) ci.onclick = createProjectIssue;
  const rj = document.getElementById("refreshJules");
  if (rj) rj.onclick = refreshJulesSessions;
  const pia = document.getElementById("planIssueAuditBtn");
  if (pia) pia.onclick = () => runPlan("issue-audit");
  const pwa = document.getElementById("planWorkflowAuditBtn");
  if (pwa) pwa.onclick = () => runPlan("workflow-audit");
  const prb = document.getElementById("planReviewBtn");
  if (prb) prb.onclick = () => {
    const pr = document.getElementById("planPrSelect").value;
    if (!pr) {
      document.getElementById("planErr").textContent =
        "Pick a PR first.";
      return;
    }
    runPlan("review", {pr: pr});
  };
  const pab = document.getElementById("planAggBtn");
  if (pab) pab.onclick = () => {
    const sel = document.getElementById("planAggPrs");
    const prs = Array.from(sel.selectedOptions).map(o => o.value);
    const target =
      (document.getElementById("planAggTarget").value || "").trim() ||
      "main";
    if (!prs.length) {
      document.getElementById("planErr").textContent =
        "Select at least one PR.";
      return;
    }
    runPlan("aggregation", {prs: prs, target: target});
  };
})();

// Live updates: re-fetch status every 10s (skipped while the tab
// is hidden) so webhook auto-pulls show up without a reload. The
// spinner shows while a refresh is in flight or a pull is pending;
// the timestamp shows when the page last refreshed. Run
// re-attachment still happens only once, on load.
let liveFetching = false;
let liveAttachedRun = null;
async function refreshStatus() {
  if (liveFetching || document.hidden) return;
  liveFetching = true;
  const spinner = document.getElementById("liveSpinner");
  if (spinner) spinner.style.display = "inline-block";
  try {
    const s = await (await fetch("/buttons/api/status")).json();
    const dot = document.getElementById("serverDot");
    if (dot) dot.className = "dot ok";
    if (s.active && liveAttachedRun !== s.active) {
      liveAttachedRun = s.active;
      poll(s.active);
    }
    const upd = document.getElementById("liveUpdated");
    const pend = document.getElementById("livePending");
    const w = s.webhooks || {};
    const pending = s.webhooks_pending || [];
    // Per-repo pull state lives on the repo cards, so the page
    // always shows which repo each result belongs to.
    document.querySelectorAll("[data-repo-status]").forEach(card => {
      const repo = card.getAttribute("data-repo-status");
      if (pending.includes(repo)) {
        card.textContent = "Auto-pull: pulling…";
      } else if (w[repo]) {
        const st = w[repo];
        card.textContent = "Auto-pull: " +
          (st.ok ? "pulled ✓ " : "pull failed ✗ ") +
          new Date(st.at * 1000).toLocaleString();
      }
    });
    // Stale page: a remote-server pull landed after this container
    // started, so the served page predates the checkout. Show the
    // banner and highlight the rebuild button — the pull alone
    // never rebuilds, and without this the page silently looks
    // current while serving old code.
    const staleBar = document.getElementById("staleBar");
    const depBtn = btnDiv.querySelector('button[data-action="deploy-buttons"]');
    if (s.stale_buttons) {
      if (staleBar) staleBar.style.display = "block";
      if (depBtn) depBtn.classList.add("needs-update");
    } else {
      if (staleBar) staleBar.style.display = "none";
      if (depBtn) depBtn.classList.remove("needs-update");
    }
    if (upd) upd.textContent = "Updated " + new Date().toLocaleTimeString();
    if (pend) pend.textContent = pending.length
      ? " · Pulling " + pending.join(", ") + "…" : "";
    if (spinner) spinner.style.display = pending.length ? "inline-block" : "none";
  } catch (e) {
    if (spinner) spinner.style.display = "none";
    const dot = document.getElementById("serverDot");
    if (dot) dot.className = "dot fail";
  }
  liveFetching = false;
}
refreshStatus();
setInterval(refreshStatus, 10000);
</script>
</body>
</html>
"""

HTML = HTML.replace("<!--BUTTONS-->", BUTTONS_HTML)


def _td_run(cmd, timeout=120, prefix=None):
    """Run one td/gh command in the terminal container; return
    stdout. Raises RuntimeError with the last stderr line on
    failure. The command is a fixed string built from validated
    values only — never from free text."""
    proc = subprocess.run(EXEC + [(prefix or TD) + cmd],
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout,
                          text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip().splitlines()[-1]
                           if proc.stderr.strip() else "command failed")
    return proc.stdout


def _ci_label(rollup):
    """CI label from a gh statusCheckRollup list. Mirrors
    briefing_summary.ci_summary (kept local: server.py cannot
    import the summarizer script)."""
    if rollup is None:
        return "unknown"
    if not rollup:
        return "no checks"
    failing = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
    for check in rollup:
        if isinstance(check, dict) and check.get("conclusion") in failing:
            return "failing"
    for check in rollup:
        if isinstance(check, dict) and check.get("status") != "COMPLETED":
            return "pending"
    return "passing"


def _pr_view(pr_number, repo, repos):
    """Fresh (CI label, merge state) for gating, at tap time."""
    view = json.loads(_td_run(
        f"gh pr view {pr_number} --json statusCheckRollup,mergeStateStatus",
        timeout=60, prefix=_td_prefix(repos[repo]["td_cwd"])))
    return (_ci_label(view.get("statusCheckRollup")),
            str(view.get("mergeStateStatus") or "?").lower())


def _pr_action(pr_number, action, repo, repos):
    """Per-PR actions from the Briefing view. Every action re-checks
    fresh state at tap time — the card may be minutes old. Merge is
    gated on CI passing + merge clean; update only on BEHIND (a
    DIRTY branch needs conflict resolution in the terminal, not a
    one-tap button — td's resolve-conflicts resolves in a worktree
    without pushing, so it cannot update the PR branch from here);
    fix-ci requires CI failing and dispatches a Jules session via
    `fix-ci --execute` (fix-ci defaults to dry-run, so the flag is
    load-bearing). ValueError = gate refused (400)."""
    prefix = _td_prefix(repos[repo]["td_cwd"])
    if action == "merge":
        ci, merge = _pr_view(pr_number, repo, repos)
        if ci != "passing" or merge != "clean":
            raise ValueError(f"not mergeable: CI {ci}, merge {merge}")
        out = _td_run(f"gh pr merge {pr_number} --squash --delete-branch",
                      prefix=prefix)
        return {"ok": True, "output": out[-2000:]}
    if action == "update":
        _ci, merge = _pr_view(pr_number, repo, repos)
        if merge != "behind":
            raise ValueError(f"cannot update: merge state is {merge} "
                             "(only behind branches update this way)")
        out = _td_run(f"gh api repos/{repos[repo]['gh']}/pulls/{pr_number}/"
                      "update-branch --method PUT", prefix=prefix)
        return {"ok": True, "output": out[-2000:]}
    ci, _merge = _pr_view(pr_number, repo, repos)
    if ci != "failing":
        raise ValueError(f"CI is {ci}, not failing — nothing to fix")
    out = _td_run(f"td --json agent fix-ci --pr {pr_number} --execute",
                  prefix=prefix)
    return {"ok": True, "output": out[-2000:]}


def _start_run(action, repo, td_argv=None):
    """Create and start a run for an allowlisted action. Returns
    (run_id, error). Single-flight: a second concurrent run gets
    the 'already in progress' error, same as the API."""
    global active_id
    if action not in ACTIONS:
        return None, "unknown action"
    with runs_lock:
        if active_id is not None:
            return None, "a run is already in progress"
        run_id = uuid.uuid4().hex[:12]
        now = time.time()
        run = {"id": run_id, "action": action, "repo": repo, "done": False,
               "exit_code": None, "output": "",
               "seq": len(runs), "started_at": now,
               "finished_at": None, "last_output_at": now,
               "step_index": 0, "step_total": 1
               if action == "run-td" else len(ACTIONS[action]["steps"]),
               "current_step": ""}
        if td_argv is not None:
            run["td_argv"] = td_argv
        runs[run_id] = run
        active_id = run_id
    print(f"run {run_id} ({action}) started", flush=True)
    threading.Thread(target=run_action, args=(run_id, action, repo),
                     daemon=True).start()
    return run_id, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep docker logs clean; runs log their own lines

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_webhook(self, body):
        secret = _webhook_secret()
        if not secret:
            self._json({"error": "webhook not configured "
                                 "(WEBHOOK_SECRET unset)"}, 503)
            return
        if len(body) > WEBHOOK_MAX_BODY:
            self._json({"error": "payload too large"}, 413)
            return
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(secret, body, sig):
            self._json({"error": "bad signature"}, 401)
            return
        event = self.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            if event == "ping":
                payload = {}
            else:
                self._json({"error": "bad JSON"}, 400)
                return
        repo = (payload.get("repository") or {}).get("full_name")
        # The allowlist is the discovered set, by GitHub path:
        # a repo added to ~/repos accepts webhooks immediately,
        # a removed one stops. Discovery runs per delivery, so a
        # push for a repo cloned after container start still
        # pulls.
        by_gh = {info["gh"]: info
                 for info in _discover_repos().values()}
        if event == "ping":
            print(f"webhook ping {repo or '?'}: pong", flush=True)
            if repo in by_gh:
                with webhook_state_lock:
                    if repo not in webhook_pending:
                        webhook_pending.add(repo)
                        threading.Thread(target=_webhook_worker,
                                         args=(repo, by_gh[repo]),
                                         daemon=True).start()
            self._json({"ok": True, "msg": "pong"})
            return
        if event != "push":
            self._json({"ignored": event})
            return
        if repo not in by_gh:
            self._json({"ignored": repo})
            return
        if payload.get("ref") != "refs/heads/main":
            self._json({"skipped": "not main"})
            return
        with webhook_state_lock:
            if repo in webhook_pending:
                self._json({"queued": True, "coalesced": True})
                return
            webhook_pending.add(repo)
        threading.Thread(target=_webhook_worker,
                         args=(repo, by_gh[repo]),
                         daemon=True).start()
        self._json({"queued": True, "repo": repo}, 202)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/buttons", "/buttons/"):
            # Repo strips and picker options render per request
            # from discovery, so a repo added to ~/repos shows up
            # without a container rebuild.
            repos = _discover_repos()
            body = HTML.replace(
                "<!--REPO_STRIPS-->",
                _repo_strips_html(repos)).replace(
                "<!--REPO_OPTIONS-->",
                _repo_options_html(repos)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/buttons/api/status":
            with runs_lock:
                recent = [{k: v for k, v in _public(r).items() if k != "output"}
                          for r in sorted(runs.values(), key=lambda r: r["seq"])[-10:]]
            with webhook_state_lock:
                hooks = dict(webhook_state)
                pending = sorted(webhook_pending)
            rs = hooks.get(STACK_GH, {})
            stale = bool(rs.get("ok") and rs.get("advanced")
                         and rs.get("at", 0) > STARTED_AT)
            self._json({"active": active_id, "runs": recent,
                        "webhooks": hooks, "webhooks_pending": pending,
                        "stale_buttons": stale})
        elif path == "/buttons/api/briefing":
            # Structured briefing for the live Briefing view: the
            # summarizer runs in the terminal container (same
            # transport as the briefing button) and its JSON is
            # passed through. Slow (td + gh calls), so the page
            # shows a spinner while it loads. ?repo= picks the
            # repo (discovered set; default tech-dancer).
            repos = _discover_repos()
            repo = parse_qs(urlparse(self.path).query).get(
                "repo", [_default_repo(repos)])[0]
            if repo not in repos:
                self._json({"error": "unknown repo"}, 400)
                return
            try:
                proc = subprocess.run(
                    EXEC + [_td_prefix(repos[repo]["td_cwd"])
                            + "python3 /srv/remote-server/buttons/"
                            "briefing_summary.py --briefing-json "
                            f"--repo {repo} --gh {repos[repo]['gh']}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=180, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (proc.stderr or "").strip().splitlines()[-1]
                        if proc.stderr.strip() else "briefing failed")
                self._json(json.loads(proc.stdout))
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
        elif path == "/buttons/api/ledger":
            # Durable audit log: newest-first, kind-filtered.
            # kind is allowlisted (plus "all"); limit is clamped.
            qs = parse_qs(urlparse(self.path).query)
            kind = qs.get("kind", ["all"])[0]
            if kind != "all" and kind not in LEDGER_KINDS:
                self._json({"error": "unknown kind"}, 400)
                return
            try:
                limit = int(qs.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            limit = max(1, min(200, limit))
            self._json({"entries": _ledger_read(kind, limit)})
        elif path == "/buttons/api/pi-sessions":
            # List Pi session files (newest first) as JSON. No TD
            # prefix: pi_logs reads /root/.pi directly, it needs
            # no repo cwd and no .env.
            try:
                proc = subprocess.run(
                    EXEC + ["python3 /srv/remote-server/buttons/"
                            "pi_logs.py --list-json"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=60, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (proc.stderr or "").strip().splitlines()[-1]
                        if proc.stderr.strip() else "pi sessions failed")
                self._json(json.loads(proc.stdout))
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
        elif path == "/buttons/api/pi-session":
            # Render one Pi session by filename query. The query is
            # interpolated into a bash -c string, so it must match
            # a strict allowlist — anything else is rejected, never
            # quoted-and-hoped. Session filenames are
            # <timestamp>_<uuid>.jsonl, which this pattern covers.
            qs = parse_qs(urlparse(self.path).query)
            query = qs.get("session", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", query):
                self._json({"error": "bad session query"}, 400)
                return
            try:
                proc = subprocess.run(
                    EXEC + ["python3 /srv/remote-server/buttons/"
                            f"pi_logs.py --session {query}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=60, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (proc.stderr or "").strip().splitlines()[-1]
                        if proc.stderr.strip() else "pi session failed")
                self._json({"output": proc.stdout})
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
        elif path.startswith("/buttons/api/run/"):
            run_id = path.rsplit("/", 1)[-1]
            with runs_lock:
                run = runs.get(run_id)
                if run is None:
                    self._json({"error": "unknown run"}, 404)
                else:
                    self._json(_public(run))
        elif path == "/buttons/api/jobs":
            with jobs_lock:
                out = [_job_public(j) for j in jobs.values()]
            out.sort(key=lambda j: j["created_at"], reverse=True)
            self._json({"jobs": out})
        elif path.startswith("/buttons/api/jobs/"):
            jid = path[len("/buttons/api/jobs/"):].split("/")[0]
            with jobs_lock:
                job = jobs.get(jid)
                self._json({"job": job} if job is not None
                           else {"error": "unknown job"},
                           200 if job is not None else 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        global active_id
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if path == WEBHOOK_PATH:
            self._handle_webhook(body)
            return
        if path == "/buttons/api/clear":
            # Clears stored output only — no command runs, no run is
            # cancelled. With no id, clears the active or newest run.
            try:
                run_id = json.loads(body).get("id") if body else None
            except Exception:
                run_id = None
            with runs_lock:
                run = runs.get(run_id) if run_id else None
                if run is None:
                    run = (runs[active_id] if active_id in runs else None) or \
                        (max(runs.values(), key=lambda r: r["seq"]) if runs else None)
                if run is None:
                    self._json({"error": "no run to clear"}, 404)
                else:
                    run["output"] = ""
                    run["last_output_at"] = time.time()
                    self._json({"ok": True, "id": run["id"]})
            return
        if path == "/buttons/api/ledger/clear":
            # Destructive and deliberate: truncates the audit log.
            # The tombstone entry (see _ledger_clear) is the record
            # that a clear happened.
            if _ledger_clear():
                self._json({"ok": True})
            else:
                self._json({"error": "ledger clear failed"}, 500)
            return
        if path == "/buttons/api/pi-sessions/clear":
            # Bulk-delete Pi session transcripts in the terminal
            # container. File discovery lives in pi_logs.py (the
            # same _list_files as the browser), never in the page,
            # and --clear takes no argument, so nothing from the
            # request reaches a shell string.
            try:
                proc = subprocess.run(
                    EXEC + ["python3 /srv/remote-server/buttons/"
                            "pi_logs.py --clear"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=120, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (proc.stderr or "").strip().splitlines()[-1]
                        if proc.stderr.strip() else "pi clear failed")
                self._json({"ok": True, "output": proc.stdout[-2000:]})
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path == "/buttons/api/pi-sessions/delete":
            # Delete one session transcript. Strict validation:
            # basename charset only, must end .jsonl, no path
            # separators — nothing from the request reaches a
            # shell string; pi_logs.py --delete QUERY matches on
            # basename substring and refuses ambiguous queries.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            session = str(data.get("session", ""))
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.jsonl",
                                session):
                self._json({"error": "bad session name"}, 400)
                return
            try:
                proc = subprocess.run(
                    EXEC + ["python3 /srv/remote-server/buttons/"
                            "pi_logs.py --delete " + shlex.quote(session)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=120, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (proc.stderr or "").strip().splitlines()[-1]
                        if proc.stderr.strip() else "pi delete failed")
                _ledger_append({"kind": "system", "action": "pi-session-delete",
                                "session": session[:120],
                                "output": proc.stdout[-500:]})
                self._json({"ok": True, "output": proc.stdout[-2000:]})
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
            return
            # Strict validation: integer PR number only (bools are
            # ints in Python, so exclude them), allowlisted action.
            # The number is then safe to interpolate: it cannot
            # carry shell text.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            pr_number, action = data.get("pr_number"), data.get("action")
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            if not isinstance(pr_number, int) or isinstance(pr_number, bool) \
                    or pr_number <= 0 \
                    or action not in ("merge", "update", "fix-ci") \
                    or repo not in repos:
                self._json({"error": "bad pr_number, action or repo"}, 400)
                return
            try:
                result = _pr_action(pr_number, action, repo, repos)
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "pr": pr_number, "action": action,
                                "ok": True,
                                "output": result["output"][-2000:]})
                self._json(result)
            except ValueError as exc:
                # A refused tap is audit-worthy too: the gate fired.
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "pr": pr_number, "action": action,
                                "ok": False, "error": str(exc)[:500]})
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "pr": pr_number, "action": action,
                                "ok": False, "error": str(exc)[:500]})
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path == "/buttons/api/suggest":
            # Pi suggests (see the section above). Reads only:
            # suggestions are not ledger entries, exactly like
            # briefing reads. Slow by design: briefing fetch plus
            # a local-model Pi run can take minutes.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            if repo not in repos:
                self._json({"error": "unknown repo"}, 400)
                return
            info = repos[repo]
            try:
                proc = subprocess.run(
                    EXEC + [_td_prefix(info["td_cwd"])
                            + "python3 /srv/remote-server/buttons/"
                            "briefing_summary.py --briefing-json "
                            f"--repo {repo} --gh {info['gh']}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=180, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (proc.stderr or "").strip().splitlines()[-1]
                        if proc.stderr.strip() else "briefing failed")
                brief = json.loads(proc.stdout)
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
                return
            prompt = _suggest_prompt(repo, _suggest_context(brief))
            b64 = base64.b64encode(prompt.encode()).decode()
            # The prompt travels base64-encoded: its alphabet is
            # shell-safe, so briefing titles carrying quotes or
            # $ cannot break out of the pi argument.
            cmd = (_td_prefix(info["td_cwd"]) +
                   'pi -p --tools read,grep,find,ls "$(echo ' + b64 +
                   ' | base64 -d)"')
            try:
                proc = subprocess.run(
                    EXEC + [cmd],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=600, text=True)
                raw = proc.stdout or ""
            except Exception as exc:
                self._json({"error": f"Pi failed: {exc}"[:500]}, 500)
                return
            self._json({"repo": repo,
                        "suggestions": _parse_suggestions(raw),
                        "raw": raw[-2000:]})
            return
        if path == "/buttons/api/ask":
            # Ask Pi: open-ended instruction from the text box.
            # Pi investigates read-only and replies; a write the
            # user asked for comes back as a PROPOSE block, parsed
            # strictly by _parse_proposal. Nothing is executed
            # here — proposals run only after a confirm tap, via
            # /buttons/api/run-td (td commands, re-validated) or
            # /buttons/api/create-issue. Like suggest: reads only,
            # not a ledger entry; slow by design on the local model.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            instruction = data.get("instruction", "")
            if repo not in repos or not isinstance(instruction, str) \
                    or not instruction.strip() \
                    or len(instruction) > ASK_MAX_CHARS:
                self._json({"error": "bad repo or instruction"}, 400)
                return
            info = repos[repo]
            prompt = _ask_prompt(repo, instruction.strip())
            b64 = base64.b64encode(prompt.encode()).decode()
            # Base64 again: the instruction is free text, so it
            # must never touch the shell unencoded.
            cmd = (_td_prefix(info["td_cwd"]) +
                   'pi -p --tools read,grep,find,ls "$(echo ' + b64 +
                   ' | base64 -d)"')
            try:
                proc = subprocess.run(
                    EXEC + [cmd],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=600, text=True)
                raw = proc.stdout or ""
            except Exception as exc:
                self._json({"error": f"Pi failed: {exc}"[:500]}, 500)
                return
            self._json({"repo": repo,
                        "reply": raw[-6000:],
                        "proposal": _parse_proposal(raw)})
            return
        if path == "/buttons/api/create-issue":
            # Create the GitHub issue Pi proposed, after the user's
            # confirm tap. Strict validation: the repo must be
            # known (gh path comes from discovery, never the
            # client), title/body are length-capped strings, and
            # both are shlex-quoted into the gh argv — free text
            # never reaches a shell unquoted. The confirm dialog
            # shows the exact title and body first.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            title, body_text = data.get("title"), data.get("body", "")
            if repo not in repos or not isinstance(title, str) \
                    or not title.strip() or len(title) > 200 \
                    or not isinstance(body_text, str) \
                    or len(body_text) > 4000:
                self._json({"error": "bad repo, title or body"}, 400)
                return
            gh = repos[repo]["gh"]
            cmd = ("gh issue create --repo " + shlex.quote(gh) +
                   " --title " + shlex.quote(title.strip()) +
                   " --body " + shlex.quote(body_text.strip()))
            try:
                out = _td_run(cmd, timeout=120,
                              prefix=_td_prefix(repos[repo]["td_cwd"]))
                url = out.strip().splitlines()
                url = url[-1] if url else ""
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "action": "create-issue", "ok": True,
                                "note": url[:200]})
                self._json({"ok": True, "url": url[:500]})
            except Exception as exc:
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "action": "create-issue", "ok": False,
                                "error": str(exc)[:500]})
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path == "/buttons/api/issues":
            # Open issues for the picked repo: number, title,
            # assignees, labels, updated, url, body (truncated).
            # Read-only; gh list with a fixed --json field set.
            qs = parse_qs(urlparse(self.path).query)
            repos = _discover_repos()
            repo = qs.get("repo", [_default_repo(repos)])[0]
            if repo not in repos:
                self._json({"error": "unknown repo"}, 400)
                return
            gh = repos[repo]["gh"]
            try:
                out = _td_run(
                    "gh issue list --repo " + shlex.quote(gh) +
                    " --state open --limit 50 --json "
                    "number,title,state,assignees,labels,updatedAt,"
                    "url,body",
                    timeout=60,
                    prefix=_td_prefix(repos[repo]["td_cwd"]))
                issues = json.loads(out)
                for i in issues:
                    if isinstance(i.get("body"), str) \
                            and len(i["body"]) > 600:
                        i["body"] = i["body"][:600] + "…"
                self._json({"repo": repo, "issues": issues})
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path == "/buttons/api/assign-issue":
            # Assign an issue to a user. Strict validation: repo
            # from discovery, issue digits, assignee GitHub-username
            # charset; everything shlex-quoted into the gh argv.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            issue = str(data.get("issue", ""))
            assignee = str(data.get("assignee", "")).strip()
            if repo not in repos or not issue.isdigit() \
                    or not re.fullmatch(r"[A-Za-z0-9-]{1,39}",
                                        assignee):
                self._json({"error": "bad repo, issue or assignee"},
                           400)
                return
            gh = repos[repo]["gh"]
            try:
                _td_run("gh issue edit " + shlex.quote(issue) +
                        " --repo " + shlex.quote(gh) +
                        " --add-assignee " + shlex.quote(assignee),
                        timeout=60,
                        prefix=_td_prefix(repos[repo]["td_cwd"]))
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "action": "assign-issue", "ok": True,
                                "note": f"#{issue} -> {assignee}"[:200]})
                self._json({"ok": True})
            except Exception as exc:
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "action": "assign-issue", "ok": False,
                                "error": str(exc)[:500]})
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path == "/buttons/api/jules-sessions":
            # Jules sessions for progress visibility: id, title,
            # state, updated, url, source repo, linked PR. Read-only.
            # Graceful when JULES_API_KEY is unset — the notice
            # pattern from the sync-nudge button.
            qs = parse_qs(urlparse(self.path).query)
            repos = _discover_repos()
            repo = qs.get("repo", [_default_repo(repos)])[0]
            if repo not in repos:
                self._json({"error": "unknown repo"}, 400)
                return
            info = repos[repo]
            cmd = ("if [ -z \"$JULES_API_KEY\" ]; then "
                   "echo '{\"sessions\": [], "
                   "\"notice\": \"JULES_API_KEY is unset — add it to "
                   ".env to see Jules sessions\"}'; else "
                   "td --json jules list-sessions; fi")
            try:
                out = _td_run(cmd, timeout=120,
                              prefix=_td_prefix(info["td_cwd"]))
                data = json.loads(out)
                sessions = []
                for s in data.get("sessions", [])[:30]:
                    pr_url = ""
                    for o in s.get("outputs", []) or []:
                        pr = o.get("pullRequest") or {}
                        if pr.get("url"):
                            pr_url = pr["url"]
                            break
                    sessions.append({
                        "id": s.get("id"), "title": s.get("title"),
                        "state": s.get("state"),
                        "updated": s.get("updateTime"),
                        "url": s.get("url"),
                        "source": ((s.get("sourceContext") or {})
                                   .get("source", "")
                                   .split("/")[-1]),
                        "pr_url": pr_url,
                    })
                self._json({"repo": repo, "sessions": sessions,
                            "notice": data.get("notice", "")})
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path == "/buttons/api/run-plan":
            # Run one allowlisted td planning guide. Plans are
            # deterministic: they read repo state and write a
            # markdown plan file; nothing executes or pushes.
            # Strict validation: plan name from the allowlist, PR
            # numbers digits, target branch charset; every token
            # shlex-quoted into the argv.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            plan = data.get("plan")
            if repo not in repos or plan not in (
                    "issue-audit", "workflow-audit", "review",
                    "aggregation"):
                self._json({"error": "bad repo or plan"}, 400)
                return
            argv = ["td", "--json", "agent"]
            if plan == "issue-audit":
                argv += ["plan-issue-audit", "--all-open"]
            elif plan == "workflow-audit":
                argv += ["plan-workflow-audit"]
            elif plan == "review":
                pr = str(data.get("pr", ""))
                if not pr.isdigit() or int(pr) <= 0:
                    self._json({"error": "bad pr"}, 400)
                    return
                argv += ["plan-review", "--pr", pr]
            else:  # aggregation
                prs = data.get("prs") or []
                target = str(data.get("target", "")).strip()
                if not prs or len(prs) > 10 or not all(
                        str(p).isdigit() and int(p) > 0
                        for p in prs) \
                        or not re.fullmatch(r"[A-Za-z0-9._/-]{1,64}",
                                            target):
                    self._json({"error": "bad prs or target"}, 400)
                    return
                argv += ["plan-aggregation"]
                for p in prs:
                    argv += ["--pr", str(int(p))]
                argv += ["--target", target]
            cmd = " ".join(shlex.quote(t) for t in argv)
            try:
                out = _td_run(cmd, timeout=300,
                              prefix=_td_prefix(repos[repo]["td_cwd"]))
                plan_path = ""
                try:
                    plan_path = json.loads(out).get("plan_path",
                                                    "") or ""
                except Exception:
                    pass
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "action": "plan-" + plan, "ok": True,
                                "note": str(plan_path)[:200]})
                self._json({"ok": True, "output": out[-4000:],
                            "plan_path": plan_path})
            except Exception as exc:
                _ledger_append({"kind": "pr-action", "repo": repo,
                                "action": "plan-" + plan, "ok": False,
                                "error": str(exc)[:500]})
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path == "/buttons/api/run-td":
            # Run one suggested td command. The command is
            # re-validated here (never trust the client) and
            # attached to the run record; run_action builds the
            # step from it. One run at a time, like every run.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            tokens = _validate_td_command(data.get("command", ""))
            if repo not in repos or not tokens:
                self._json({"error": "bad repo or command"}, 400)
                return
            run_id, err = _start_run("run-td", repo, td_argv=tokens)
            if err:
                self._json({"error": err}, 409)
                return
            self._json({"id": run_id})
            return
        # --- Agent jobs ---
        if path == "/buttons/api/jobs":
            # POST: create a job from a template.
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            repos = _discover_repos()
            repo = data.get("repo", _default_repo(repos))
            pr = data.get("pr")
            issues = data.get("issues")
            job, err = _job_create(data.get("template"), repo, pr,
                                   issues)
            if err:
                self._json({"error": err}, 400)
                return
            self._json({"job": job})
            return
        if path.startswith("/buttons/api/jobs/"):
            # /buttons/api/jobs/<id>/{approve|decline|step-done} (POST)
            rest = path[len("/buttons/api/jobs/"):]
            jid, _, sub = rest.partition("/")
            with jobs_lock:
                job = jobs.get(jid)
            if job is None:
                self._json({"error": "unknown job"}, 404)
                return
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            step = data.get("step")
            if sub == "approve":
                with jobs_lock:
                    if not isinstance(step, int) or isinstance(step, bool) \
                            or not 0 <= step < len(job["plan"]):
                        self._json({"error": "bad step"}, 400)
                        return
                    st = job["plan"][step]
                    if job["state"] != "active" \
                            or st["state"] != "pending":
                        self._json({"error": "step not pending"}, 409)
                        return
                    if any(p["state"] != "done"
                           for p in job["plan"][:step]):
                        self._json({"error": "earlier steps not done"},
                                   409)
                        return
                    st["state"] = "approved"
                    st["approved_at"] = time.time()
                    job["approvals"].append(
                        {"step": step, "decision": "approved",
                         "at": st["approved_at"]})
                    kind = st["kind"]
                if kind == "pr-action":
                    # Synchronous: gates re-checked inside _pr_action.
                    try:
                        _pr_action(job["pr"], st["action"], job["repo"],
                                   _discover_repos())
                        ok, err = True, None
                    except ValueError as exc:
                        ok, err = False, str(exc)
                    except Exception as exc:
                        ok, err = False, str(exc)[:500]
                    with jobs_lock:
                        st["state"] = "done" if ok else "failed"
                        st["finished_at"] = time.time()
                        st["exit_code"] = 0 if ok else 1
                        st["note"] = None if ok else err
                        _job_after_step(job)
                        _jobs_save()
                    _ledger_append({"kind": "job", "job": jid,
                                    "step": step, "event": "step done"
                                    if ok else "step failed",
                                    "ok": ok})
                    self._json({"ok": ok, "state": st["state"],
                                "error": err})
                    return
                if kind == "label-issue":
                    # Synchronous: label the issue 'jules' to start
                    # a Jules session on it (Jules GitHub
                    # integration). Gates re-checked in
                    # _label_issue_jules.
                    try:
                        _label_issue_jules(job["repo"], st["issue"],
                                           _discover_repos())
                        ok, err = True, None
                    except ValueError as exc:
                        ok, err = False, str(exc)
                    except Exception as exc:
                        ok, err = False, str(exc)[:500]
                    with jobs_lock:
                        st["state"] = "done" if ok else "failed"
                        st["finished_at"] = time.time()
                        st["exit_code"] = 0 if ok else 1
                        st["note"] = None if ok else err
                        _job_after_step(job)
                        _jobs_save()
                    _ledger_append({"kind": "job", "job": jid,
                                    "step": step,
                                    "event": "dispatched Jules on #"
                                    + str(st["issue"]) if ok
                                    else "dispatch failed",
                                    "ok": ok})
                    self._json({"ok": ok, "state": st["state"],
                                "error": err})
                    return
                # kind == "action": async run, client polls it.
                run_id, err = _start_run(st["action"], job["repo"])
                with jobs_lock:
                    if err:
                        st["state"] = "pending"
                        st["approved_at"] = None
                        job["approvals"].pop()
                        _jobs_save()
                        self._json({"error": err}, 409)
                        return
                    st["state"] = "running"
                    st["run_id"] = run_id
                    _jobs_save()
                _ledger_append({"kind": "job", "job": jid, "step": step,
                                "event": "step approved", "run": run_id})
                self._json({"run_id": run_id})
                return
            if sub == "decline":
                with jobs_lock:
                    if not isinstance(step, int) or isinstance(step, bool) \
                            or not 0 <= step < len(job["plan"]):
                        self._json({"error": "bad step"}, 400)
                        return
                    st = job["plan"][step]
                    if job["state"] != "active" \
                            or st["state"] != "pending":
                        self._json({"error": "step not pending"}, 409)
                        return
                    st["state"] = "declined"
                    job["approvals"].append(
                        {"step": step, "decision": "declined",
                         "at": time.time()})
                    _job_end(job, "declined")
                self._json({"ok": True})
                return
            if sub == "step-done":
                # Client reports the polled run finished.
                run_id = data.get("run_id")
                with runs_lock:
                    run = runs.get(run_id)
                    run_done = run is not None and run["done"]
                    run_exit = run["exit_code"] if run else None
                with jobs_lock:
                    if not isinstance(step, int) or isinstance(step, bool) \
                            or not 0 <= step < len(job["plan"]):
                        self._json({"error": "bad step"}, 400)
                        return
                    st = job["plan"][step]
                    if st["state"] != "running" \
                            or st.get("run_id") != run_id:
                        self._json({"error": "step not running this run"},
                                   409)
                        return
                    if not run_done:
                        self._json({"error": "run not done"}, 409)
                        return
                    st["state"] = "done" \
                        if run_exit == 0 else "failed"
                    st["finished_at"] = time.time()
                    st["exit_code"] = run_exit
                    _job_after_step(job)
                    _jobs_save()
                    state = st["state"]
                _ledger_append({"kind": "job", "job": jid, "step": step,
                                "event": "step " + state, "run": run_id})
                _job_advance(job)  # read-only next steps start on their own
                self._json({"ok": True, "state": state})
                return
            self._json({"error": "not found"}, 404)
            return
        if path != "/buttons/api/run":
            self._json({"error": "not found"}, 404)
            return
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        action = data.get("action")
        repos = _discover_repos()
        repo = data.get("repo", _default_repo(repos))
        if action not in ACTIONS:
            self._json({"error": "unknown action"}, 400)
            return
        if repo not in repos:
            self._json({"error": "unknown repo"}, 400)
            return
        run_id, err = _start_run(action, repo)
        if err:
            self._json({"error": err}, 409)
            return
        self._json({"id": run_id})


if __name__ == "__main__":
    port = int(os.environ.get("BUTTONS_PORT", "8000"))
    _ledger_append({"kind": "system", "event": "buttons started"})
    _jobs_load()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

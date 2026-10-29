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
REPO_SCOPED_ACTIONS = {"briefing-td", "warm-context", "preflight"}


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
                     "PR actions. Pick it in the Briefing picker.")
        out.append(
            f'<div class="repo"><div class="reponame">'
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
# the full briefing and NOT agent-brief.md (Pi gets limited,
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
                lines.append(f"- {r.get('name')} on {r.get('branch')}")
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
        "If nothing needs doing, output exactly one line: "
        "SUGGEST: none :: <reason>\n"
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
    "status": {
        "label": "Status",
        "group": "Server",
        "sub": "What's running, what's changed, which terminal image is in use",
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
        "sub": "Git pull, rebuild this page's container — page drops for ~30s, terminal stays up",
        "confirm": True,
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
        "sub": "Restart terminal with the current image and .env — no code or image update. Use after editing .env",
        "confirm": True,
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
        "sub": "Last 100 lines from terminal + Ollama, plus Pi session audit (latest session, readable)",
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
        "label": "Repo briefing (td, fast)",
        "group": "Briefing",
        "sub": "Open PRs, CI state, conflicts — fast, from td (repo follows the Briefing picker below)",
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
    # the whole button fail silently. It does NOT cover a missing
    # JULES_API_KEY — both spellings fail that way, and the
    # failure note below says so.
    "sync-nudge": {
        "label": "Sync & nudge agents",
        "group": "Briefing",
        "sub": "List active Jules sessions, then post CI/review feedback to stalled ones",
        "confirm": True,
        "steps": [
            EXEC + [TD + "td --json agent list-sessions || "
                    "td --json jules list-sessions"],
            EXEC + [TD + "td --json agent run-feedback-check"],
        ],
    },
    # Warm context: regenerate .agent-context.json only if stale
    # or missing (context-warm without --force), so td/agy/pi start
    # with a fresh index. The file is gitignored (untracked), so
    # warming never dirties the checkout or blocks a fast-forward.
    "warm-context": {
        "label": "Warm repo context",
        "group": "Briefing",
        "sub": "Regenerate the picked repo's .agent-context.json if stale",
        "confirm": False,
        "steps": [
            EXEC + [TD + "td context-warm"],
        ],
    },
    # Preflight: doctor + typecheck + lint + overlap detection +
    # merge simulation, in one deterministic run.
    "preflight": {
        "label": "Preflight check",
        "group": "Briefing",
        "sub": "doctor + typecheck + lint + overlap + merge simulation for the picked repo",
        "confirm": False,
        "steps": [
            EXEC + [TD + "td --json preflight"],
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
# page covers two repos and the old bare headings left that
# ambiguous: Server = the remote-server stack, Briefing = the
# tech-dancer repo (briefings read tech-dancer only).
GROUP_TITLES = {
    "Server": "Server · arii/remote-server",
    "Briefing": "Briefing · repo from the picker below",
}


def _buttons_html():
    # Server is a closed <details> drawer: the phone screen belongs
    # to repo work, and Status/Logs/Recreate are escape hatches,
    # one tap away. Briefing stays open.
    parts = []
    for group in ("Server", "Briefing"):
        btns = []
        for name, spec in ACTIONS.items():
            if spec["group"] != group or spec.get("hidden"):
                continue
            btns.append(
                f'<button data-action="{escape(name)}" '
                f'data-confirm="{"1" if spec["confirm"] else "0"}" '
                f'data-label="{escape(spec["label"])}">'
                f'<span class="blabel">{escape(spec["label"])}</span>'
                f'<span class="bsub">{escape(spec["sub"])}</span></button>')
        if group == "Server":
            parts.append(
                f'<details><summary class="grouphead">'
                f'{GROUP_TITLES[group]}</summary>{"".join(btns)}</details>')
        else:
            parts.append(
                f'<div class="grouphead">{GROUP_TITLES[group]}</div>'
                f'{"".join(btns)}')
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
LEDGER_KINDS = ("run", "webhook", "pr-action", "system")


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
        "id": run["id"], "action": run["action"], "done": done,
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
                if action == "briefing-td":
                    # The cwd alone does not pick the repo: the
                    # summarizer's gh api paths need --repo and
                    # --gh (it no longer carries a repo table).
                    cmd += (f" --repo {repo}"
                            f" --gh {repos[repo]['gh']}")
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
<title>S5 buttons</title>
<style>
body { font-family: system-ui, sans-serif; margin: 1em; background: #111; color: #eee; }
h1 { font-size: 1.2em; }
button { display: block; width: 100%; margin: 0.5em 0; padding: 1em; font-size: 1.1em;
  background: #2a2a2a; color: #eee; border: 1px solid #555; border-radius: 8px; }
button:disabled { opacity: 0.4; }
.blabel { display: block; }
.bsub { display: block; font-size: 0.85em; opacity: 0.7; font-weight: normal;
  margin-top: 0.25em; }
.grouphead { font-weight: bold; margin: 1em 0 0.2em; color: #bbb; }
summary.grouphead { cursor: pointer; }
.repo { border: 1px solid #555; border-radius: 8px; padding: 0.5em;
  margin: 0.5em 0; }
.reponame { font-weight: bold; }
.reponame a { color: inherit; text-decoration: underline; }
.reposcope { font-size: 0.9em; color: #bbb; }
.repostatus { font-size: 0.9em; color: #bbb; }
.planexcerpt { max-height: 30vh; }
.planact button { display: inline-block; width: auto; margin: 0.5em 0.5em 0.5em 0;
  padding: 0.5em 0.8em; font-size: 0.9em; }
.planact a { margin-left: 0.5em; }
#clearBtn { padding: 0.5em; font-size: 0.9em; }
#jsdiag { color: #ffd75e; border: 1px solid #8a6d1b; background: #3a2f0a;
  padding: 0.5em; border-radius: 8px; margin: 0.5em 0; }
#liveBar { font-size: 0.9em; color: #bbb; margin: 0.5em 0; }
.spinner { display: inline-block; width: 12px; height: 12px;
  margin-right: 0.4em; vertical-align: -2px; border: 2px solid #555;
  border-top-color: #eee; border-radius: 50%;
  animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
#state { margin: 0.5em 0; font-weight: bold; padding: 0.5em; border-radius: 8px;
  white-space: pre-line; border: 1px solid #555; }
#state.ok { color: #7ed07e; background: #12351a; border-color: #2e7d32; }
#state.fail { color: #ff8a8a; background: #3d1414; border-color: #a33; }
#state.running { color: #ffd75e; background: #3a2f0a; border-color: #8a6d1b; }
pre { white-space: pre-wrap; word-break: break-all; background: #000; padding: 0.5em;
  border-radius: 8px; max-height: 60vh; overflow: auto; border: 1px solid #333; }
pre.ok { border-color: #2e7d32; }
pre.fail { border-color: #a33; }
pre.running { border-color: #8a6d1b; }
h2 { font-size: 1.1em; margin-top: 1.5em; }
.prcard { border: 1px solid #555; border-radius: 8px; padding: 0.5em; margin: 0.5em 0; }
.prcard .prtitle { font-weight: bold; }
.prcard .prmeta { font-size: 0.9em; color: #bbb; }
.prcard .prresult { font-size: 0.9em; margin-top: 0.4em; }
.practions { margin-top: 0.4em; }
#staleBar { border: 1px solid #e6a817; border-radius: 8px; padding: 0.5em; margin: 0.5em 0; }
button.needs-update { border-color: #e6a817; box-shadow: 0 0 0 2px rgba(230,168,23,0.4); }
#briefingErr { color: #ff8a8a; }
a { color: #8bf; }
</style>
</head>
<body>
<h1>S5 buttons</h1>
<p><a href="/">Terminal</a> &middot; fixed actions only, no free-text commands.</p>
<div id="repos">
<!--REPO_STRIPS-->
</div>
<div id="liveBar"><span id="liveSpinner" class="spinner" style="display:none"></span><span id="liveUpdated"></span><span id="livePending"></span></div>
<div id="staleBar" style="display:none">Stack code updated since this page was built — tap Deploy buttons below to rebuild.</div>
<div id="jsdiag">JavaScript is not running on this page — if this line stays
visible, hard-refresh and check the browser console. The buttons
below are still visible, but they need JavaScript to start a run.</div>
<div id="buttons"><!--BUTTONS--></div>
<button id="clearBtn">Clear output</button>
<div id="state"></div>
<pre id="out"></pre>
<h2>Briefing</h2>
<p>Live briefing — open PRs, CI, conflicts, reviews, Jules
sessions. Runs on load; Refresh re-fetches. The picker sets the
repo for the cards, their action buttons, and the repo-scoped
buttons above (briefing, warm context, preflight).</p>
<label>Repo: <select id="briefingRepo">
<!--REPO_OPTIONS-->
</select></label>
<div id="briefing"></div>
<button id="refreshBriefing">Refresh briefing</button>
<div id="briefingErr"></div>
<h2>Suggested next actions</h2>
<p>Pi reads the briefing context and suggests td commands, then
runs only what you approve: each suggestion below has its own
Run button with a confirm dialog. Pi runs read-only for this
(no tools that execute anything); suggestions are validated
against a fixed td-command list, and anything else Pi prints
is shown as text, never run. Asking Pi takes minutes on the
local model — the list shows a spinner line while it thinks.</p>
<button id="suggestBtn">Suggest next actions</button>
<div id="suggestions"></div>
<div id="suggestErr"></div>
<h2>Ledger</h2>
<p>Durable audit log — every run, webhook pull, and PR-action tap,
newest first. Survives container restarts (named volume, not the
checkout). Briefing reads are not logged: reads are not actions.
Clear ledger deletes all entries but leaves a 'ledger cleared'
record behind, so the clear itself stays on the record.</p>
<label>Kind: <select id="ledgerKind">
<option value="all">All</option>
<option value="run">Runs</option>
<option value="webhook">Webhooks</option>
<option value="pr-action">PR actions</option>
<option value="system">System</option>
</select></label>
<div id="ledger"></div>
<button id="refreshLedger">Refresh ledger</button>
<button id="clearLedger">Clear ledger</button>
<div id="ledgerErr"></div>
<h2>Pi sessions</h2>
<p>Browse Pi transcripts on disk (pi-data volume). List shows the
newest sessions with date, repo, and first prompt — tap one to
render it readably (the filename is the hover text). The full
JSONL stays on disk — this is the readable view, not the
archive. Clear Pi sessions deletes all transcripts.</p>
<button id="listPiSessions">List sessions</button>
<button id="clearPiSessions">Clear Pi sessions</button>
<div id="piSessionList"></div>
<pre id="piSessionOut"></pre>
<div id="piSessionErr"></div>
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
// run from a reload): its completion scrolls the output into
// view, because on a phone the output pane sits below the
// buttons and "finished: exit 0" in the state line alone looks
// like no output.
let userStartedRun = false;

async function startRun(name, label, confirmFlag) {
  userStartedRun = true;
  if (confirmFlag && !window.confirm(label + " — run it?")) return;
  // Repo-scoped buttons follow the Briefing picker; the rest are
  // stack-wide or global and ignore it.
  const body = {action: name};
  if (name === "briefing-td" || name === "warm-context" || name === "preflight") {
    const picker = document.getElementById("briefingRepo");
    if (picker) body.repo = picker.value;
  }
  const r = await fetch("/buttons/api/run", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  if (!r.ok) { stateEl.textContent = "error: " + (await r.json()).error; return; }
  poll((await r.json()).id, name);
}

btnDiv.querySelectorAll("button").forEach(b => {
  b.onclick = () => startRun(b.dataset.action, b.dataset.label,
    b.dataset.confirm === "1");
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
  // \r overwrites the current line (progress output), so a line
  // built with \r shows only its final state, not every redraw.
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
      " — " + fmt(elapsed) + "\nsee the “step failed” line at the end of the output";
  } else {
    cls = "running";
    text = "running: " + run.action + " — " + step + " — " + fmt(elapsed) +
      " elapsed, last output " + fmt(since) + " ago\n" +
      (run.current_step || "").slice(0, 160);
  }
  stateEl.className = cls;
  outEl.className = cls;
  stateEl.textContent = text;
}

let currentId = null;
let lastRun = null;

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
      if (failures >= 5) {
        clearInterval(timer);
        stateEl.textContent = "connection lost — the buttons server may " +
          "be rebuilding itself; refresh this page in ~30s";
        stateEl.className = "fail";
        outEl.className = "fail";
        setBusy(false);
      }
      return;
    }
    failures = 0;
    const run = await r.json();
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
      if (userStartedRun) {
        userStartedRun = false;
        outEl.scrollIntoView();
      }
      // A finished briefing-td run means the cards are stale:
      // refresh them so "ran briefing" visibly updates the view
      // instead of leaving the reader to guess where output went.
      if (run.action === "briefing-td" &&
          typeof refreshBriefing === "function") refreshBriefing();
      // Any finished run is a new ledger line.
      if (typeof refreshLedger === "function") refreshLedger();
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

</script>
<script>
// Briefing view (script block 2, separate from the core block
// above, so a Briefing failure cannot take the core buttons down).
// Renders the structured briefing from /buttons/api/briefing as
// PR cards — the live view that replaces the old Plans file
// checkboxes. Auto-runs on load; Refresh re-fetches.
const briefingEl = document.getElementById("briefing");
const briefingErrEl = document.getElementById("briefingErr");
// Per-PR actions: one tap per card, gated both client-side
// (which buttons render) and server-side (fresh state re-checked
// at tap time — the card may be minutes old). Merge is squash +
// delete-branch; Update is the GitHub update-branch API, BEHIND
// only; Fix CI dispatches a Jules session, it is not instant.
function prActionButton(pr, action, label, confirmText) {
  const b = document.createElement("button");
  b.textContent = label;
  b.onclick = async () => {
    if (!window.confirm(confirmText)) return;
    b.disabled = true;
    const card = b.closest(".prcard");
    const res = card ? card.querySelector(".prresult") : null;
    try {
      const picker = document.getElementById("briefingRepo");
      const body = {pr_number: pr.number, action: action,
        repo: picker ? picker.value : "tech-dancer"};
      const r = await fetch("/buttons/api/pr-action", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)});
      const data = await r.json();
      if (!r.ok) { if (res) res.textContent = "Failed: " + data.error; b.disabled = false; return; }
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
    const title = document.createElement("div");
    title.className = "prtitle";
    if (pr.url) {
      const link = document.createElement("a");
      link.href = pr.url; link.textContent = "#" + pr.number + " " + pr.title;
      title.appendChild(link);
    } else {
      title.textContent = "#" + pr.number + " " + pr.title;
    }
    const meta = document.createElement("div");
    meta.className = "prmeta";
    meta.textContent = pr.branch + " — CI: " + pr.ci +
      (pr.failing.length ? " (" + pr.failing.join(", ") + ")" : "") +
      " — merge: " + pr.merge +
      (pr.conflicted ? " — CONFLICTED" : "") +
      (pr.stale ? " — stale" : "") +
      (pr.draft ? " — draft" : "") + " — updated " + pr.updated;
    card.appendChild(title);
    card.appendChild(meta);
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
        "Merge PR #" + pr.number + " (squash, delete branch)?"));
    if (pr.merge === "behind" && !pr.draft)
      actions.appendChild(prActionButton(pr, "update", "Update branch",
        "Update PR #" + pr.number + " branch to the base branch?"));
    if (pr.ci === "failing" && !pr.draft)
      actions.appendChild(prActionButton(pr, "fix-ci", "Fix CI with Jules",
        "Dispatch a Jules session to fix CI on PR #" + pr.number + "? This creates an agent session, it is not instant."));
    card.appendChild(actions);
    const result = document.createElement("div");
    result.className = "prresult";
    card.appendChild(result);
    briefingEl.appendChild(card);
  });
  if (b.failing_runs && b.failing_runs.length) {
    const w = document.createElement("div");
    const wHead = document.createElement("b"); wHead.textContent = "Failing runs"; w.appendChild(wHead);
    b.failing_runs.forEach(run => {
      const p = document.createElement("p");
      p.textContent = run.name + " on " + run.branch + " (" + run.event + ")" +
        (run.failed.length ? " — failed: " + run.failed.map(f => f.job + (f.steps.length ? " (" + f.steps.join(", ") + ")" : "")).join("; ") : "");
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
    const p = document.createElement("p"); p.textContent = "No active Jules sessions for tech-dancer."; js.appendChild(p);
  }
  if (j.other) { const p = document.createElement("p"); p.textContent = "(" + j.other + " active session(s) for other repos, not listed.)"; js.appendChild(p); }
  briefingEl.appendChild(js);
}
async function refreshBriefing() {
  const picker = document.getElementById("briefingRepo");
  const repo = picker ? picker.value : "tech-dancer";
  briefingEl.innerHTML = '<span class="spinner"></span> Loading briefing…';
  try {
    const r = await fetch("/buttons/api/briefing?repo=" + encodeURIComponent(repo));
    if (!r.ok) { briefingEl.textContent = ""; briefingErrEl.textContent = "briefing unavailable: " + (await r.json()).error; return; }
    renderBriefing(await r.json());
  } catch (e) {
    briefingEl.textContent = "";
    briefingErrEl.textContent = "briefing unavailable: " + e;
  }
}
document.getElementById("refreshBriefing").onclick = refreshBriefing;
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
        if (!window.confirm("Run: " + s.command + "?")) return;
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

// Ledger view. Entry data is rendered with textContent only —
// never innerHTML — so a log line can never inject markup.
// Output tails hide in <details> so the list stays scannable.
const ledgerEl = document.getElementById("ledger");
const ledgerErrEl = document.getElementById("ledgerErr");
const ledgerKindEl = document.getElementById("ledgerKind");
function ledgerSummary(e) {
  if (e.kind === "run") return "run " + e.action + (e.repo ? " (" + e.repo + ")" : "") + " — exit " + e.exit;
  if (e.kind === "webhook") return "webhook " + e.repo + " — " + (e.ok ? "pulled" : "FAILED") + (e.advanced ? " (advanced)" : "") + ": " + (e.detail || "");
  if (e.kind === "pr-action") return "pr-action " + e.action + " #" + e.pr + (e.repo ? " (" + e.repo + ")" : "") + " — " + (e.ok ? "ok" : "refused/failed");
  if (e.kind === "system") return "system: " + (e.event || "");
  return "unknown entry";
}
async function refreshLedger() {
  if (!ledgerEl) return;
  const kind = ledgerKindEl ? ledgerKindEl.value : "all";
  ledgerEl.textContent = "Loading ledger…";
  try {
    const r = await fetch("/buttons/api/ledger?kind=" + encodeURIComponent(kind) + "&limit=50");
    if (!r.ok) { ledgerEl.textContent = ""; ledgerErrEl.textContent = "ledger unavailable: " + (await r.json()).error; return; }
    const data = await r.json();
    ledgerErrEl.textContent = "";
    ledgerEl.textContent = "";
    if (!data.entries.length) { ledgerEl.textContent = "No ledger entries yet."; return; }
    data.entries.forEach(e => {
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
  if (!window.confirm("Clear the ledger? All entries will be deleted except a 'ledger cleared' record. This cannot be undone.")) return;
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
    const r = await fetch("/buttons/api/pi-session?session=" + encodeURIComponent(file));
    const data = await r.json();
    if (!r.ok) { piSessionOutEl.textContent = ""; piSessionErrEl.textContent = "session unavailable: " + data.error; return; }
    piSessionErrEl.textContent = "";
    piSessionOutEl.textContent = data.output;
  } catch (e) {
    piSessionOutEl.textContent = "";
    piSessionErrEl.textContent = "session unavailable: " + e;
  }
}
const clearPiSessionsBtn = document.getElementById("clearPiSessions");
if (clearPiSessionsBtn) clearPiSessionsBtn.onclick = async () => {
  if (!window.confirm("Clear all Pi sessions? This deletes the transcripts and cannot be undone.")) return;
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
    const r = await fetch("/buttons/api/pi-sessions");
    const data = await r.json();
    if (!r.ok) { piSessionListEl.textContent = ""; piSessionErrEl.textContent = "sessions unavailable: " + data.error; return; }
    piSessionErrEl.textContent = "";
    piSessionListEl.textContent = "";
    if (!data.sessions.length) { piSessionListEl.textContent = "No Pi sessions found."; return; }
    data.sessions.forEach(s => {
      const b = document.createElement("button");
      const when = s.mtime ? new Date(s.mtime * 1000).toLocaleString() : "";
      const repo = s.cwd ? s.cwd.split("/").filter(Boolean).pop() : "";
      b.textContent = s.title ? [when, repo, s.title].filter(Boolean).join(" · ") : s.file;
      b.title = s.file;
      b.onclick = () => showSession(s.file);
      piSessionListEl.appendChild(b);
    });
  } catch (e) {
    piSessionListEl.textContent = "";
    piSessionErrEl.textContent = "sessions unavailable: " + e;
  }
};

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
        if path == "/buttons/api/pr-action":
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
            with runs_lock:
                if active_id is not None:
                    self._json({"error": "a run is already in progress"},
                               409)
                    return
                run_id = uuid.uuid4().hex[:12]
                now = time.time()
                runs[run_id] = {"id": run_id, "action": "run-td",
                                "done": False, "exit_code": None,
                                "output": "", "seq": len(runs),
                                "started_at": now, "finished_at": None,
                                "last_output_at": now, "step_index": 0,
                                "step_total": 1, "current_step": "",
                                "td_argv": tokens}
                active_id = run_id
            print(f"run {run_id} (run-td) started", flush=True)
            threading.Thread(target=run_action,
                             args=(run_id, "run-td", repo),
                             daemon=True).start()
            self._json({"id": run_id})
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
        with runs_lock:
            if active_id is not None:
                self._json({"error": "a run is already in progress"}, 409)
                return
            run_id = uuid.uuid4().hex[:12]
            now = time.time()
            runs[run_id] = {"id": run_id, "action": action, "done": False,
                            "exit_code": None, "output": "",
                            "seq": len(runs), "started_at": now,
                            "finished_at": None, "last_output_at": now,
                            "step_index": 0, "step_total": len(ACTIONS[action]["steps"]),
                            "current_step": ""}
            active_id = run_id
        print(f"run {run_id} ({action}) started", flush=True)
        threading.Thread(target=run_action, args=(run_id, action, repo),
                         daemon=True).start()
        self._json({"id": run_id})


if __name__ == "__main__":
    port = int(os.environ.get("BUTTONS_PORT", "8000"))
    _ledger_append({"kind": "system", "event": "buttons started"})
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

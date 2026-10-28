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

import fcntl
import hashlib
import hmac
import json
import os
import pty
import re
import struct
import subprocess
import termios
from html import escape
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

STACK = "/home/ari/remote-server"
COMPOSE = ["docker", "compose", "-f", f"{STACK}/docker-compose.yml"]
# Run a command inside the terminal container (where td and Pi live).
EXEC = COMPOSE + ["exec", "-T", "terminal", "bash", "-c"]
TD = "set -a && [ -f /srv/remote-server/.env ] && . /srv/remote-server/.env; set +a && cd /srv/repos/tech-dancer && "
# HOME must be the host user's home: ${HOME} in the compose file (repos
# mount) interpolates from the CLI environment, and the daemon resolves
# the resulting host path.
ENV = {**os.environ, "HOME": "/home/ari"}

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
WEBHOOK_REPOS = {
    "arii/remote-server": "/home/ari/remote-server",
    "arii/tech-dancer": "/home/ari/repos/tech-dancer",
}
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


def _webhook_warm(old_head, new_head):
    """Best-effort post-pull warming for tech-dancer. Runs in the
    terminal container (same transport as the briefing buttons):
    pnpm install only when package.json/pnpm-lock.yaml changed
    across the pull, context-warm always (no --force: it rewrites
    only when stale or missing). .agent-context.json is gitignored,
    so warming never dirties the checkout. Any failure is logged,
    never surfaced as a pull failure."""
    try:
        changed = set()
        if old_head and new_head and old_head != new_head:
            diff = subprocess.run(
                ["git", "-c", "safe.directory="
                 + WEBHOOK_REPOS["arii/tech-dancer"], "-C",
                 WEBHOOK_REPOS["arii/tech-dancer"],
                 "diff", "--name-only", old_head, new_head],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=60, text=True)
            changed = set((diff.stdout or "").split())
        if changed & {"package.json", "pnpm-lock.yaml"}:
            subprocess.run(
                EXEC + ["cd /srv/repos/tech-dancer && "
                        "pnpm install --frozen-lockfile"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=300, text=True)
            print("webhook warm: pnpm install done", flush=True)
    except Exception as exc:
        print(f"webhook warm: pnpm install skipped: {exc}", flush=True)
    try:
        subprocess.run(EXEC + [TD + "td context-warm"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=300, text=True)
        print("webhook warm: context-warm done", flush=True)
    except Exception as exc:
        print(f"webhook warm: context-warm skipped: {exc}", flush=True)


def _webhook_worker(repo, path):
    # Serialized: two pushes racing the same checkout must not run
    # fetch+merge concurrently. State/pending updates are separate
    # and brief, so status reads never block on a pull.
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
    print(f"webhook pull {repo}: {'ok' if ok else 'FAILED'}: {detail}",
          flush=True)
    # Warming runs AFTER the pull record lands, in its own thread:
    # it must never hold the pull lock (a slow pnpm install would
    # serialize later pushes) and never fail the pull record.
    if ok and repo == "arii/tech-dancer":
        threading.Thread(target=_webhook_warm,
                         args=(old_head, new_head), daemon=True).start()
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
    # Rebuilds THIS service from the checked-out code. It recreates
    # its own container mid-run, so the run's connection drops and
    # the client must treat connection loss as "refresh in ~30s",
    # not as a failure to report. The terminal stays up throughout,
    # so a failed build is recoverable from there. Confirm first.
    "deploy-buttons": {
        "label": "Deploy buttons (rebuild this page)",
        "group": "Server",
        "sub": "Git pull, rebuild this page's container — page drops for ~30s, terminal stays up",
        "confirm": True,
        "steps": [
            GIT + ["pull", "--ff-only"],
            COMPOSE + ["up", "-d", "--build", "buttons"],
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
        "sub": "Open PRs, CI state, conflicts for tech-dancer — fast, from td",
        "confirm": False,
        "steps": [
            EXEC + [TD + "python3 /srv/remote-server/buttons/briefing_summary.py --briefing"],
        ],
    },
    # Sync & nudge: first list active Jules sessions (td's agent
    # sync is the list-sessions command, not a verb called sync),
    # then run the one-shot feedback check that posts CI failure
    # and review feedback to stalled sessions. Confirm first: the
    # second step writes to live agent sessions.
    "sync-nudge": {
        "label": "Sync & nudge agents",
        "group": "Briefing",
        "sub": "List active Jules sessions, then post CI/review feedback to stalled ones",
        "confirm": True,
        "steps": [
            EXEC + [TD + "td --json agent list-sessions"],
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
        "sub": "Regenerate tech-dancer .agent-context.json if stale",
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
        "sub": "doctor + typecheck + lint + overlap + merge simulation for tech-dancer",
        "confirm": False,
        "steps": [
            EXEC + [TD + "td --json preflight"],
        ],
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
    "Briefing": "Briefing · arii/tech-dancer",
}


def _buttons_html():
    # Server is a closed <details> drawer: the phone screen belongs
    # to repo work, and Status/Logs/Recreate are escape hatches,
    # one tap away. Briefing stays open.
    parts = []
    for group in ("Server", "Briefing"):
        btns = []
        for name, spec in ACTIONS.items():
            if spec["group"] != group:
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


def run_action(run_id, action):
    global active_id
    run = runs[run_id]
    steps = ACTIONS[action]["steps"]
    for i, step in enumerate(steps, 1):
        with runs_lock:
            run["step_index"] = i
            run["step_total"] = len(steps)
            run["current_step"] = " ".join(step)
            _append(run, "$ " + " ".join(step) + "\n")

        def _cb(text, run=run):
            with runs_lock:
                _append(run, text)

        try:
            code = _run_step(step, _cb)
        except Exception as exc:  # e.g. docker binary missing
            with runs_lock:
                _append(run, f"error: {exc}\n")
            code = 1
        if code != 0:
            with runs_lock:
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
    print(f"run {run_id} ({action}) finished: exit={run['exit_code']}", flush=True)
    with runs_lock:
        if active_id == run_id:
            active_id = None


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
<div class="repo"><div class="reponame"><a href="https://github.com/arii/remote-server">arii/remote-server</a></div><div class="reposcope">This stack — terminal, buttons, oauth2-proxy. The Server buttons below act here.</div><div class="repostatus" data-repo-status="arii/remote-server">Auto-pull: not yet</div></div>
<div class="repo"><div class="reponame"><a href="https://github.com/arii/tech-dancer">arii/tech-dancer</a></div><div class="reposcope">Repo work — briefings, agent sessions. The Briefing buttons below read this repo only.</div><div class="repostatus" data-repo-status="arii/tech-dancer">Auto-pull: not yet</div></div>
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
<p>Live briefing for tech-dancer — open PRs, CI, conflicts, reviews,
Jules sessions. Runs on load; Refresh re-fetches.</p>
<div id="briefing"></div>
<button id="refreshBriefing">Refresh briefing</button>
<div id="briefingErr"></div>
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
  const r = await fetch("/buttons/api/run", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: name})});
  if (!r.ok) { stateEl.textContent = "error: " + (await r.json()).error; return; }
  poll((await r.json()).id);
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

async function poll(id) {
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
      // The buttons server itself is unreachable — most notably
      // when Deploy buttons recreates its own container mid-run.
      // That is expected there, not a run failure: say so and
      // stop polling instead of spinning forever with disabled
      // buttons and a stale "running" line.
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
      const r = await fetch("/buttons/api/pr-action", {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({pr_number: pr.number, action: action})});
      const data = await r.json();
      if (!r.ok) { if (res) res.textContent = "Failed: " + data.error; b.disabled = false; return; }
      if (res) res.textContent = "Done. Refresh the briefing to see the new state.";
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
  briefingEl.innerHTML = '<span class="spinner"></span> Loading briefing…';
  try {
    const r = await fetch("/buttons/api/briefing");
    if (!r.ok) { briefingEl.textContent = ""; briefingErrEl.textContent = "briefing unavailable: " + (await r.json()).error; return; }
    renderBriefing(await r.json());
  } catch (e) {
    briefingEl.textContent = "";
    briefingErrEl.textContent = "briefing unavailable: " + e;
  }
}
document.getElementById("refreshBriefing").onclick = refreshBriefing;
refreshBriefing();

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


def _td_run(cmd, timeout=120):
    """Run one td/gh command in the terminal container; return
    stdout. Raises RuntimeError with the last stderr line on
    failure. The command is a fixed string built from a validated
    integer only — never from free text."""
    proc = subprocess.run(EXEC + [TD + cmd], stdout=subprocess.PIPE,
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


def _pr_view(pr_number):
    """Fresh (CI label, merge state) for gating, at tap time."""
    view = json.loads(_td_run(
        f"gh pr view {pr_number} --json statusCheckRollup,mergeStateStatus",
        timeout=60))
    return (_ci_label(view.get("statusCheckRollup")),
            str(view.get("mergeStateStatus") or "?").lower())


def _pr_action(pr_number, action):
    """Per-PR actions from the Briefing view. Every action re-checks
    fresh state at tap time — the card may be minutes old. Merge is
    gated on CI passing + merge clean; update only on BEHIND (a
    DIRTY branch needs conflict resolution in the terminal, not a
    one-tap button — td's resolve-conflicts resolves in a worktree
    without pushing, so it cannot update the PR branch from here);
    fix-ci requires CI failing and dispatches a Jules session via
    `fix-ci --execute` (fix-ci defaults to dry-run, so the flag is
    load-bearing). ValueError = gate refused (400)."""
    if action == "merge":
        ci, merge = _pr_view(pr_number)
        if ci != "passing" or merge != "clean":
            raise ValueError(f"not mergeable: CI {ci}, merge {merge}")
        out = _td_run(f"gh pr merge {pr_number} --squash --delete-branch")
        return {"ok": True, "output": out[-2000:]}
    if action == "update":
        _ci, merge = _pr_view(pr_number)
        if merge != "behind":
            raise ValueError(f"cannot update: merge state is {merge} "
                             "(only behind branches update this way)")
        out = _td_run(f"gh api repos/arii/tech-dancer/pulls/{pr_number}/"
                      "update-branch --method PUT")
        return {"ok": True, "output": out[-2000:]}
    ci, _merge = _pr_view(pr_number)
    if ci != "failing":
        raise ValueError(f"CI is {ci}, not failing — nothing to fix")
    out = _td_run(f"td --json agent fix-ci --pr {pr_number} --execute")
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
        if event == "ping":
            print(f"webhook ping {repo or '?'}: pong", flush=True)
            if repo in WEBHOOK_REPOS:
                with webhook_state_lock:
                    if repo not in webhook_pending:
                        webhook_pending.add(repo)
                        threading.Thread(target=_webhook_worker,
                                         args=(repo, WEBHOOK_REPOS[repo]),
                                         daemon=True).start()
            self._json({"ok": True, "msg": "pong"})
            return
        if event != "push":
            self._json({"ignored": event})
            return
        if repo not in WEBHOOK_REPOS:
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
                         args=(repo, WEBHOOK_REPOS[repo]),
                         daemon=True).start()
        self._json({"queued": True, "repo": repo}, 202)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/buttons", "/buttons/"):
            body = HTML.encode()
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
            rs = hooks.get("arii/remote-server", {})
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
            # shows a spinner while it loads.
            try:
                proc = subprocess.run(
                    EXEC + [TD + "python3 /srv/remote-server/buttons/"
                            "briefing_summary.py --briefing-json"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=180, text=True)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (proc.stderr or "").strip().splitlines()[-1]
                        if proc.stderr.strip() else "briefing failed")
                self._json(json.loads(proc.stdout))
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
            if not isinstance(pr_number, int) or isinstance(pr_number, bool) \
                    or pr_number <= 0 \
                    or action not in ("merge", "update", "fix-ci"):
                self._json({"error": "bad pr_number or action"}, 400)
                return
            try:
                self._json(_pr_action(pr_number, action))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": str(exc)[:500]}, 500)
            return
        if path != "/buttons/api/run":
            self._json({"error": "not found"}, 404)
            return
        try:
            action = json.loads(body).get("action") if body else None
        except Exception:
            action = None
        if action not in ACTIONS:
            self._json({"error": "unknown action"}, 400)
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
        threading.Thread(target=run_action, args=(run_id, action),
                         daemon=True).start()
        self._json({"id": run_id})


if __name__ == "__main__":
    port = int(os.environ.get("BUTTONS_PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

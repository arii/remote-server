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


def _webhook_secret():
    return os.environ.get("WEBHOOK_SECRET", "")


def _verify_signature(secret, body, header):
    if not header or not header.startswith("sha256="):
        return False
    expect = "sha256=" + hmac.new(secret.encode(), body,
                                  hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, header)


def _webhook_worker(repo, path):
    # Serialized: two pushes racing the same checkout must not run
    # fetch+merge concurrently. State/pending updates are separate
    # and brief, so status reads never block on a pull.
    with webhook_pull_lock:
        git = ["git", "-c", f"safe.directory={path}", "-C", path,
               "-c", "core.hooksPath=/dev/null"]
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
        except Exception as exc:
            ok, detail = False, str(exc)[:200]
    with webhook_state_lock:
        webhook_state[repo] = {"at": time.time(), "ok": ok,
                               "detail": detail}
        webhook_pending.discard(repo)
    print(f"webhook pull {repo}: {'ok' if ok else 'FAILED'}: {detail}",
          flush=True)
# Steps run under a PTY (see _run_step): commands then emit the
# ANSI colours they would show in the terminal, and the page renders
# them. A TTY stdout also makes some tools page their output (git
# log -> less), which would hang a run waiting for input nobody can
# send — so pagers are forced off here, not in each command.
RUN_ENV = {**ENV, "TERM": "xterm-256color",
           "GIT_PAGER": "cat", "PAGER": "cat"}
MAX_OUTPUT = 200_000  # keep the tail; model pulls are verbose

GIT = ["git", "-c", f"safe.directory={STACK}", "-C", STACK]

# Plans view: tech-dancer briefing plans live in the TERMINAL
# container (the buttons container has no /srv/repos mount), so list
# and toggle run through `compose exec terminal python3 -`, the same
# transport the briefing buttons use. The script below is fixed code:
# the only caller-supplied values are a validated plan filename and
# a line number, and a toggle flips exactly one checkbox character
# on a line that is still a checkbox — never free text, never a
# shell command. Toggle state therefore lives in the plan file
# itself (untracked in tech-dancer until committed) and matches
# what the terminal shows.
PLANS_PATH = "/srv/repos/tech-dancer/.agents/plans"
PLAN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")
PLANS_SCRIPT = r'''
import sys, json, os, re, glob
path = __PLANS_PATH__
pat = re.compile(r"^(\s*[-*] \[)([ xX])(\].*)$")
def parse(text):
    items = []
    for i, line in enumerate(text.splitlines()):
        m = pat.match(line)
        if m:
            items.append({"line": i, "checked": m.group(2).lower() == "x",
                          "text": m.group(3)[1:].strip()})
    return items
def excerpt(text):
    # The briefing itself, not just the checklist: the first
    # non-blank, non-heading, non-checkbox lines of the plan file
    # (td status-board / PR / conflict output), truncated. Without
    # this the Plans view shows only generic checklist items and
    # hides everything that made the briefing worth reading.
    out = []
    for line in text.splitlines():
        s = line.strip()
        # The plan file's raw appendix is an audit trail, not reading
        # material: stop the excerpt there, or it bleeds JSON into
        # the page after the readable sections run out.
        if s == "## Raw output":
            break
        if not s or s.startswith("#") or pat.match(line):
            continue
        out.append(line.rstrip())
        if sum(len(x) for x in out) > 800:
            break
    return "\n".join(out)[:800]
def plan(name):
    fp = os.path.join(path, name)
    with open(fp, encoding="utf-8", errors="replace") as f:
        text = f.read()
    items = parse(text)
    return {"name": name, "total": len(items),
            "done": sum(1 for it in items if it["checked"]), "items": items,
            "mtime": int(os.path.getmtime(fp)), "excerpt": excerpt(text)}
# Payload is embedded by the caller (python3 - reads the script
# itself from stdin, so stdin cannot also carry the payload).
payload = __PAYLOAD__
cmd = payload.get("cmd")
if cmd == "list":
    names = sorted((os.path.basename(p) for p in glob.glob(os.path.join(path, "*.md"))),
                   reverse=True) if os.path.isdir(path) else []
    print(json.dumps({"plans": [plan(n) for n in names]}))
elif cmd == "toggle":
    name = payload.get("name", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md", name):
        print(json.dumps({"error": "bad plan name"})); sys.exit(1)
    fp = os.path.join(path, name)
    if not os.path.isfile(fp):
        print(json.dumps({"error": "unknown plan"})); sys.exit(1)
    with open(fp, encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.splitlines()
    line_no = payload.get("line")
    if not isinstance(line_no, int) or not 0 <= line_no < len(lines):
        print(json.dumps({"error": "bad line"})); sys.exit(1)
    m = pat.match(lines[line_no])
    if not m:
        print(json.dumps({"error": "line is no longer a checkbox"})); sys.exit(1)
    lines[line_no] = m.group(1) + (" " if m.group(2).lower() == "x" else "x") + m.group(3)
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    print(json.dumps({"plan": plan(name)}))
else:
    print(json.dumps({"error": "unknown plans command"})); sys.exit(1)
'''


def _run_plans(payload):
    script = PLANS_SCRIPT.replace("__PLANS_PATH__", json.dumps(PLANS_PATH)) \
                         .replace("__PAYLOAD__", json.dumps(payload))
    proc = subprocess.run(
        COMPOSE + ["exec", "-T", "terminal", "python3", "-"],
        input=script.encode(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, cwd=STACK, env=ENV)
    if proc.returncode != 0:
        try:
            msg = json.loads(proc.stdout.decode()).get("error", "plans failed")
        except Exception:
            msg = (proc.stderr.decode() or "plans failed").strip()[-200:]
        raise ValueError(msg)
    return json.loads(proc.stdout.decode())

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
    "pull-models": {
        "label": "Pull models",
        "group": "Server",
        "sub": "Download the Ollama models in .env — slow, gigabytes",
        "confirm": True,
        "steps": [
            ["bash", f"{STACK}/scripts/pull-models.sh"],
        ],
    },
    "logs": {
        "label": "Logs",
        "group": "Server",
        "sub": "Last 100 lines from terminal + Ollama",
        "confirm": False,
        "steps": [
            COMPOSE + ["logs", "--tail=100", "terminal", "ollama"],
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
        "sub": "Open PRs, CI state, conflicts — fast, from td",
        "confirm": False,
        "steps": [
            EXEC + [TD + "python3 /srv/remote-server/buttons/briefing_summary.py --briefing"],
        ],
    },
    # Agent-written briefing: Pi print mode (-p: one-shot reply and
    # exit, no TUI) with the agent brief as context. Slow on the local
    # 8b model (minutes). MCP tools in print mode are unverified on
    # the S5 — the prompt falls back to td/gh CLI if they are absent.
    "briefing-agent": {
        "label": "Repo briefing (agent, slow)",
        "group": "Briefing",
        "sub": "Same briefing, written by Pi — slow (minutes on the local model)",
        "confirm": True,
        "steps": [
            EXEC + [TD + 'pi -p "Read /root/.pi/agent/agent-brief.md '
                     '(fallback: /srv/remote-server/terminal/pi-config/agent-brief.md) '
                     'and follow it. Give me the repo briefing for tech-dancer: '
                     'open PRs with CI state, merge conflicts, failing checks, '
                     'unresolved review comments, stale PRs. End with the single '
                     'most important action. Use boomtick MCP tools if available, '
                     'otherwise td and gh CLI."'],
        ],
    },
    # Plan file from the deterministic briefing. Written into the
    # tech-dancer checkout (untracked until committed) at
    # .agents/plans/<date>-briefing-plan.md, next to the repo's other
    # agent workflows. The summarizer writes the readable briefing,
    # then a Next-actions checklist DERIVED from the findings (one
    # checkbox per attention item), then the raw td JSON as an
    # appendix. A failing source degrades its section instead of
    # losing the whole plan — the file landing is what exit 0 means
    # here, not "every source passed"; check the file's contents.
    "create-plan": {
        "label": "Create plan from briefing",
        "group": "Briefing",
        "sub": "Save today's briefing as a plan file in tech-dancer/.agents/plans/",
        "confirm": True,
        "steps": [
            EXEC + [TD + "python3 /srv/remote-server/buttons/briefing_summary.py --plan-dir .agents/plans "
                     "&& git status --short .agents/plans/"],
        ],
    },
}

# Action buttons are rendered into the HTML server-side, not built
# by JavaScript. Lesson from 2026-10-28: when the buttons were
# JS-generated, any failure in the page script (parse or early
# runtime) left the page with NO action buttons at all, while the
# static parts (Clear output, Plans heading) still rendered — so
# the page looked half-alive and gave no clue what broke. Now the
# buttons exist in the raw HTML whether or not any script runs;
# JavaScript only attaches handlers to them. A second, separate
# script block holds the Plans code, so a Plans failure cannot
# take the core buttons down with it.
# Buttons are grouped (Server / Briefing) with a one-line subtitle
# under each label, so the page itself says what a button does and
# when to use it — the docs are not where anyone looks mid-task.
# data-label carries the bare label for the confirm dialog; the
# button's textContent now includes the subtitle too.
def _buttons_html():
    parts = []
    for group in ("Server", "Briefing"):
        parts.append(f'<div class="grouphead">{group}</div>')
        for name, spec in ACTIONS.items():
            if spec["group"] != group:
                continue
            parts.append(
                f'<button data-action="{escape(name)}" '
                f'data-confirm="{"1" if spec["confirm"] else "0"}" '
                f'data-label="{escape(spec["label"])}">'
                f'<span class="blabel">{escape(spec["label"])}</span>'
                f'<span class="bsub">{escape(spec["sub"])}</span></button>')
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
.planexcerpt { max-height: 30vh; }
.planact button { display: inline-block; width: auto; margin: 0.5em 0.5em 0.5em 0;
  padding: 0.5em 0.8em; font-size: 0.9em; }
.planact a { margin-left: 0.5em; }
#clearBtn { padding: 0.5em; font-size: 0.9em; }
#jsdiag { color: #ffd75e; border: 1px solid #8a6d1b; background: #3a2f0a;
  padding: 0.5em; border-radius: 8px; margin: 0.5em 0; }
#webhookStatus { font-size: 0.9em; color: #bbb; margin: 0.5em 0; }
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
.plan { border: 1px solid #555; border-radius: 8px; padding: 0.5em; margin: 0.5em 0; }
.planhead { cursor: pointer; font-weight: bold; }
.planbar { background: #222; border-radius: 4px; height: 8px; margin: 0.4em 0; }
.planbar > div { background: #4caf50; height: 8px; border-radius: 4px; }
.planitems label { display: block; margin: 0.4em 0; font-weight: normal; }
.planitems input { margin-right: 0.5em; }
#plansErr { color: #ff8a8a; }
a { color: #8bf; }
</style>
</head>
<body>
<h1>S5 buttons</h1>
<p><a href="/">Terminal</a> &middot; fixed actions only, no free-text commands.</p>
<div id="webhookStatus"></div>
<div id="jsdiag">JavaScript is not running on this page — if this line stays
visible, hard-refresh and check the browser console. The buttons
below are still visible, but they need JavaScript to start a run.</div>
<div id="buttons"><!--BUTTONS--></div>
<button id="clearBtn">Clear output</button>
<div id="state"></div>
<pre id="out"></pre>
<h2>Plans</h2>
<p>Briefing plans from tech-dancer <code>.agents/plans/</code>. Tapping a
checkbox flips that line in the plan file itself.</p>
<div id="plans"></div>
<button id="refreshPlans">Refresh plans</button>
<div id="plansErr"></div>
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

// startRun is shared with the Plans block: its per-plan action
// buttons start the same allowlisted runs (briefing, new plan).
async function startRun(name, label, confirmFlag) {
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
      if (run.action === "create-plan" && run.exit_code === 0 &&
          typeof refreshPlans === "function") refreshPlans();
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
// Plans script (script block 2, separate from the core block above).
// If this block fails, the core action buttons above still work;
// only the Plans section stays empty.
// Plans: list + toggle. A toggle sends only a validated plan name
// and a line number; the server flips one checkbox character in
// that file and returns the updated counts. No free text anywhere.
const plansEl = document.getElementById("plans");
const plansErrEl = document.getElementById("plansErr");

function renderPlans(plans) {
  plansEl.textContent = "";
  plansErrEl.textContent = "";
  if (!plans.length) {
    plansEl.textContent = "No plans yet — press Create plan from briefing.";
    return;
  }
  plans.forEach((plan, planIndex) => {
    const div = document.createElement("div");
    div.className = "plan";
    const head = document.createElement("div");
    head.className = "planhead";
    const bar = document.createElement("div");
    bar.className = "planbar";
    const fill = document.createElement("div");
    bar.appendChild(fill);
    const itemsDiv = document.createElement("div");
    itemsDiv.className = "planitems";
    // Newest plan (first in the list) starts expanded; the rest
    // stay collapsed so the section stays scannable on a phone.
    itemsDiv.style.display = planIndex === 0 ? "block" : "none";
    function updateHead(p) {
      head.textContent = p.name + " — " + p.done + "/" + p.total +
        (p.mtime ? " — " + new Date(p.mtime * 1000).toLocaleDateString() : "");
      fill.style.width = p.total ? (100 * p.done / p.total) + "%" : "0%";
    }
    updateHead(plan);
    head.onclick = () => {
      itemsDiv.style.display = itemsDiv.style.display === "none" ? "block" : "none";
    };
    // The briefing itself, not just its checklist: the plan file's
    // td output (PRs, CI, conflicts) as a short excerpt, so an
    // expanded plan is actually informative on this page.
    if (plan.excerpt) {
      const pre = document.createElement("pre");
      pre.className = "planexcerpt";
      pre.textContent = plan.excerpt;
      itemsDiv.appendChild(pre);
    }
    // Actions you can take from a plan, using the same allowlisted
    // runs as the main buttons — a plan used to be a dead end:
    // checkboxes and nothing else. These run into the output pane
    // above (create-plan refreshes this list when it finishes).
    const act = document.createElement("div");
    act.className = "planact";
    const rb = document.createElement("button");
    rb.textContent = "Re-run briefing (td, fast)";
    rb.onclick = () => { if (typeof startRun === "function")
      startRun("briefing-td", "Repo briefing (td, fast)", false); };
    const np = document.createElement("button");
    np.textContent = "Create a new plan";
    np.onclick = () => { if (typeof startRun === "function")
      startRun("create-plan", "Create plan from briefing", true); };
    const tl = document.createElement("a");
    tl.href = "/";
    tl.textContent = "Terminal";
    act.appendChild(rb);
    act.appendChild(np);
    act.appendChild(tl);
    itemsDiv.appendChild(act);
    for (const item of plan.items) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = item.checked;
      box.onchange = async () => {
        box.disabled = true;
        const r = await fetch("/buttons/api/plans/toggle", {method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: plan.name, line: item.line})});
        if (!r.ok) {
          box.checked = item.checked;
          plansErrEl.textContent = "toggle failed: " + (await r.json()).error;
        } else {
          const updated = (await r.json()).plan;
          item.checked = box.checked;
          plan.done = updated.done;
          plan.total = updated.total;
          updateHead(plan);
        }
        box.disabled = false;
      };
      label.appendChild(box);
      label.appendChild(document.createTextNode(item.text));
      itemsDiv.appendChild(label);
    }
    div.appendChild(head);
    div.appendChild(bar);
    div.appendChild(itemsDiv);
    plansEl.appendChild(div);
  });
}

async function refreshPlans() {
  const r = await fetch("/buttons/api/plans");
  if (!r.ok) {
    plansErrEl.textContent = "plans unavailable: " + (await r.json()).error;
    return;
  }
  renderPlans((await r.json()).plans);
}

document.getElementById("refreshPlans").onclick = refreshPlans;
refreshPlans();

// Re-attach to a run still going after a page reload, and show
// the last webhook auto-pull per repo (or that none has happened).
fetch("/buttons/api/status").then(r => r.json()).then(s => {
  if (s.active) poll(s.active);
  const el = document.getElementById("webhookStatus");
  if (!el) return;
  const w = s.webhooks || {};
  const parts = Object.keys(w).map(repo => {
    const st = w[repo];
    return repo + ": " + (st.ok ? "pulled ✓ " : "pull failed ✗ ") +
      new Date(st.at * 1000).toLocaleString();
  });
  el.textContent = parts.length ? "Auto-pull: " + parts.join(" · ")
    : "Auto-pull: no webhook pulls yet";
});
</script>
</body>
</html>
"""

HTML = HTML.replace("<!--BUTTONS-->", BUTTONS_HTML)


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
            self._json({"active": active_id, "runs": recent,
                        "webhooks": hooks})
        elif path == "/buttons/api/plans":
            try:
                self._json(_run_plans({"cmd": "list"}))
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
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
        if path == "/buttons/api/plans/toggle":
            try:
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            name, line_no = data.get("name"), data.get("line")
            if not isinstance(name, str) or not PLAN_NAME_RE.fullmatch(name) \
                    or not isinstance(line_no, int) or isinstance(line_no, bool):
                self._json({"error": "bad plan name or line"}, 400)
                return
            try:
                self._json(_run_plans({"cmd": "toggle", "name": name, "line": line_no}))
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
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

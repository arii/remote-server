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

import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

STACK = "/home/ari/remote-server"
COMPOSE = ["docker", "compose", "-f", f"{STACK}/docker-compose.yml"]
# Run a command inside the terminal container (where td and Pi live).
EXEC = COMPOSE + ["exec", "-T", "terminal", "bash", "-c"]
TD = "cd /srv/repos/tech-dancer && "
# HOME must be the host user's home: ${HOME} in the compose file (repos
# mount) interpolates from the CLI environment, and the daemon resolves
# the resulting host path.
ENV = {**os.environ, "HOME": "/home/ari"}
MAX_OUTPUT = 200_000  # keep the tail; model pulls are verbose

GIT = ["git", "-c", f"safe.directory={STACK}", "-C", STACK]

ACTIONS = {
    "status": {
        "label": "Status",
        "confirm": False,
        "steps": [
            GIT + ["status", "--short", "--branch"],
            GIT + ["log", "--oneline", "-3"],
            COMPOSE + ["ps"],
            ["docker", "images", "ghcr.io/arii/remote-server-terminal",
             "--format", "{{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}"],
        ],
    },
    "deploy": {
        "label": "Deploy latest",
        "confirm": True,
        "steps": [
            GIT + ["pull", "--ff-only"],
            COMPOSE + ["pull", "terminal"],
            COMPOSE + ["up", "-d", "terminal"],
        ],
    },
    "recreate": {
        "label": "Recreate terminal",
        "confirm": True,
        "steps": [
            COMPOSE + ["up", "-d", "--force-recreate", "terminal"],
        ],
    },
    "pull-models": {
        "label": "Pull models",
        "confirm": True,
        "steps": [
            ["bash", f"{STACK}/scripts/pull-models.sh"],
        ],
    },
    "logs": {
        "label": "Logs",
        "confirm": False,
        "steps": [
            COMPOSE + ["logs", "--tail=100", "terminal", "ollama"],
        ],
    },
    # Deterministic briefing: td commands (read-only) inside the
    # terminal container. tech-dancer has project_config.json, so td
    # passes config validation there. Needs gh auth or GITHUB_TOKEN
    # in the terminal container; auth errors show in the output.
    "briefing-td": {
        "label": "Repo briefing (td, fast)",
        "confirm": False,
        "steps": [
            EXEC + [TD + "td gh status-board"],
            EXEC + [TD + "td gh search-prs --state open"],
            EXEC + [TD + "td gh detect-conflicts --all"],
        ],
    },
    # Agent-written briefing: Pi print mode (-p: one-shot reply and
    # exit, no TUI) with the agent brief as context. Slow on the local
    # 8b model (minutes). MCP tools in print mode are unverified on
    # the S5 — the prompt falls back to td/gh CLI if they are absent.
    "briefing-agent": {
        "label": "Repo briefing (agent, slow)",
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
    # agent workflows. No set -e: a failing td step still lands in
    # the file (with its error) instead of losing the whole plan.
    "create-plan": {
        "label": "Create plan from briefing",
        "confirm": True,
        "steps": [
            EXEC + [TD + 'mkdir -p .agents/plans && { echo "# Briefing plan $(date +%F)"; '
                     'echo; td gh status-board; echo; td gh search-prs --state open; '
                     'echo; td gh detect-conflicts --all; } '
                     '> .agents/plans/$(date +%F)-briefing-plan.md 2>&1; '
                     'echo "wrote .agents/plans/$(date +%F)-briefing-plan.md"; '
                     'git status --short .agents/plans/'],
        ],
    },
}

runs = {}
runs_lock = threading.Lock()
active_id = None


def _append(run, text):
    run["output"] += text
    if len(run["output"]) > MAX_OUTPUT:
        run["output"] = run["output"][-MAX_OUTPUT:]


def run_action(run_id, action):
    global active_id
    run = runs[run_id]
    for step in ACTIONS[action]["steps"]:
        with runs_lock:
            _append(run, "$ " + " ".join(step) + "\n")
        try:
            proc = subprocess.Popen(
                step, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=STACK, env=ENV,
            )
            for line in proc.stdout:
                with runs_lock:
                    _append(run, line)
            code = proc.wait()
        except Exception as exc:  # e.g. docker binary missing
            with runs_lock:
                _append(run, f"error: {exc}\n")
            code = 1
        if code != 0:
            with runs_lock:
                _append(run, f"step failed with exit code {code}; aborting\n")
                run["exit_code"] = code
                run["done"] = True
            break
    else:
        with runs_lock:
            run["exit_code"] = 0
            run["done"] = True
    print(f"run {run_id} ({action}) finished: exit={run['exit_code']}", flush=True)
    with runs_lock:
        if active_id == run_id:
            active_id = None


HTML = """<!DOCTYPE html>
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
#state { margin: 0.5em 0; font-weight: bold; }
pre { white-space: pre-wrap; word-break: break-all; background: #000; padding: 0.5em;
  border-radius: 8px; max-height: 60vh; overflow: auto; }
a { color: #8bf; }
</style>
</head>
<body>
<h1>S5 buttons</h1>
<p><a href="/">Terminal</a> &middot; fixed actions only, no free-text commands.</p>
<div id="buttons"></div>
<div id="state"></div>
<pre id="out"></pre>
<script>
const ACTIONS = [
  ["status", "Status", false],
  ["deploy", "Deploy latest (git pull + pull + up)", true],
  ["recreate", "Recreate terminal (applies .env changes)", true],
  ["pull-models", "Pull models (slow, GBs)", true],
  ["logs", "Logs (terminal + ollama, tail 100)", false],
  ["briefing-td", "Repo briefing (td, fast)", false],
  ["briefing-agent", "Repo briefing (agent, slow)", true],
  ["create-plan", "Create plan from briefing", true],
];
const btnDiv = document.getElementById("buttons");
const stateEl = document.getElementById("state");
const outEl = document.getElementById("out");
let timer = null;

for (const [name, label, confirm] of ACTIONS) {
  const b = document.createElement("button");
  b.textContent = label;
  b.onclick = async () => {
    if (confirm && !window.confirm(label + " — run it?")) return;
    const r = await fetch("/buttons/api/run", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: name})});
    if (!r.ok) { stateEl.textContent = "error: " + (await r.json()).error; return; }
    poll((await r.json()).id);
  };
  btnDiv.appendChild(b);
}

function setBusy(busy) {
  btnDiv.querySelectorAll("button").forEach(b => b.disabled = busy);
}

async function poll(id) {
  setBusy(true);
  clearInterval(timer);
  timer = setInterval(async () => {
    const r = await fetch("/buttons/api/run/" + id);
    const run = await r.json();
    outEl.textContent = run.output;
    outEl.scrollTop = outEl.scrollHeight;
    if (run.done) {
      clearInterval(timer);
      stateEl.textContent = run.exit_code === 0 ? "done ✓" : "failed (exit " + run.exit_code + ")";
      setBusy(false);
    } else {
      stateEl.textContent = "running: " + run.action + "…";
    }
  }, 2000);
}

// Re-attach to a run still going after a page reload.
fetch("/buttons/api/status").then(r => r.json()).then(s => {
  if (s.active) poll(s.active);
});
</script>
</body>
</html>
"""


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
                recent = [{"id": r["id"], "action": r["action"], "done": r["done"],
                           "exit_code": r["exit_code"]}
                          for r in sorted(runs.values(), key=lambda r: r["seq"])[-10:]]
                self._json({"active": active_id, "runs": recent})
        elif path.startswith("/buttons/api/run/"):
            run_id = path.rsplit("/", 1)[-1]
            with runs_lock:
                run = runs.get(run_id)
                if run is None:
                    self._json({"error": "unknown run"}, 404)
                else:
                    self._json({k: run[k] for k in
                                ("id", "action", "done", "exit_code", "output")})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/buttons/api/run":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            action = json.loads(self.rfile.read(length)).get("action")
        except Exception:
            action = None
        if action not in ACTIONS:
            self._json({"error": "unknown action"}, 400)
            return
        global active_id
        with runs_lock:
            if active_id is not None:
                self._json({"error": "a run is already in progress"}, 409)
                return
            run_id = uuid.uuid4().hex[:12]
            runs[run_id] = {"id": run_id, "action": action, "done": False,
                            "exit_code": None, "output": "",
                            "seq": len(runs)}
            active_id = run_id
        print(f"run {run_id} ({action}) started", flush=True)
        threading.Thread(target=run_action, args=(run_id, action),
                         daemon=True).start()
        self._json({"id": run_id})


if __name__ == "__main__":
    port = int(os.environ.get("BUTTONS_PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

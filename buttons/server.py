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
import re
import subprocess
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
def plan(name):
    with open(os.path.join(path, name), encoding="utf-8", errors="replace") as f:
        text = f.read()
    items = parse(text)
    return {"name": name, "total": len(items),
            "done": sum(1 for it in items if it["checked"]), "items": items}
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
                     'echo; td gh detect-conflicts --all; '
                     'echo; echo "## Next actions"; '
                     'echo "- [ ] Review open PRs and CI state above"; '
                     'echo "- [ ] Resolve merge conflicts above, if any"; '
                     'echo "- [ ] Triage failing checks and unresolved review comments"; '
                     'echo "- [ ] Do the single most important action, then re-run the briefing"; } '
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
#clearBtn { padding: 0.5em; font-size: 0.9em; }
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
<div id="buttons"></div>
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

function fmt(s) {
  s = Math.max(0, Math.floor(s));
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
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
  outEl.textContent = "";
  stateEl.textContent = "";
  stateEl.className = "";
  outEl.className = "";
  setBusy(true);
  clearInterval(timer);
  timer = setInterval(async () => {
    const r = await fetch("/buttons/api/run/" + id);
    if (!r.ok) return;
    const run = await r.json();
    lastRun = run;
    outEl.textContent = run.output;
    outEl.scrollTop = outEl.scrollHeight;
    renderState(run);
    if (run.done) {
      clearInterval(timer);
      setBusy(false);
      if (run.action === "create-plan" && run.exit_code === 0) refreshPlans();
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
  outEl.textContent = "";
  if (!lastRun || lastRun.done) {
    stateEl.textContent = "output cleared";
    stateEl.className = "";
    outEl.className = "";
  }
};

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
  for (const plan of plans) {
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
    itemsDiv.style.display = "none";
    function updateHead(p) {
      head.textContent = p.name + " — " + p.done + "/" + p.total;
      fill.style.width = p.total ? (100 * p.done / p.total) + "%" : "0%";
    }
    updateHead(plan);
    head.onclick = () => {
      itemsDiv.style.display = itemsDiv.style.display === "none" ? "block" : "none";
    };
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
  }
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
                recent = [{k: v for k, v in _public(r).items() if k != "output"}
                          for r in sorted(runs.values(), key=lambda r: r["seq"])[-10:]]
                self._json({"active": active_id, "runs": recent})
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

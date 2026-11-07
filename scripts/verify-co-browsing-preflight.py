"""End-to-end co-browsing preflight (follow_up Issue 12).

Runs the 6 verification stages against the live stack from inside the
terminal container (compose network). Human or `agy` usage::

    python3 scripts/verify-co-browsing-preflight.py [--repo /srv/remote-server]

Environment (all optional)::

    BROWSER_HTTP       Selenium Grid base URL (default http://browser:4444).
                       The headed browser service must exist; no fail-safe.
    AGENT_API          agent-control API base (default http://buttons:8000/api/agent)
    PREVIEW_URL        page the visual audit loads (default
                       http://caddy:8080/buttons/ -- the console itself)
    TD_BIN             td entrypoint, shlex-split (default "td").
                       The installed td must know --preview-url; when it is
                       stale (see docs/follow-ups.md section 3) point at a
                       checkout, e.g. TD_BIN="python3 dev_tools/cli.py"
                       with TD_CWD=/srv/repos/boomtick/cli.
    TD_CWD             working directory for TD_BIN (default: none)
    REPO               repo to probe with td audit-impact (default /srv/remote-server)

Exit 0: all stages pass. Exit 1: a stage failed. Exit 2: a required
tool is missing (playwright/td) or the stack is unreachable.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BROWSER_HTTP = os.environ.get("BROWSER_HTTP", "http://browser:4444")
AGENT_API = os.environ.get("AGENT_API", "http://buttons:8000/api/agent")
PREVIEW_URL = os.environ.get(
    "PREVIEW_URL", "http://caddy:8080/buttons/")
TD_BIN = os.environ.get("TD_BIN", "td")
TD_CWD = os.environ.get("TD_CWD") or None
REPO = os.environ.get("REPO", "/srv/remote-server")


class CheckFailed(Exception):
    """A stage ran and its assertion did not hold."""


class MissingTool(Exception):
    """A stage cannot run: dependency or service unreachable."""


def http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise MissingTool(f"{url} unreachable: {exc}")


def http_post(url, timeout=10):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise MissingTool(f"{url} unreachable: {exc}")


# Grid Chrome flags, single-sourced so tests pin them (#1104):
# --disable-dev-shm-usage complements the container shm_size, same
# as browser_view.py — /dev/shm exhaustion SIGKILLs renderers.
GRID_CHROME_ARGS = [
    "--user-data-dir=/data/shared-browser/agent",
    "--enable-features=WebMCP",
    "--disable-dev-shm-usage",
]


def grid_cdp_ws_url(session_id):
    """Per-session CDP websocket URL, the same way browser_cdp derives it."""
    parts = urllib.parse.urlsplit(BROWSER_HTTP)
    scheme = "wss" if parts.scheme == "https" else "ws"
    base = f"{scheme}://{parts.netloc or parts.path}".rstrip("/")
    return f"{base}/session/{session_id}/se/cdp"


def create_grid_session():
    """Open a Grid session; returns (session_id, cdp_ws_url).

    The agent profile lives on the mounted host dir, so logins and
    localStorage survive session and container restarts. ChromeDriver
    yields to a user-supplied --user-data-dir."""
    req = urllib.request.Request(
        BROWSER_HTTP.rstrip("/") + "/session",
        data=json.dumps(
            {"capabilities": {"alwaysMatch": {
                "browserName": "chrome",
                "goog:chromeOptions": {"args": list(GRID_CHROME_ARGS)}}}
             }).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise MissingTool(f"grid session create failed: {exc}")
    try:
        session_id = payload["value"]["sessionId"]
    except (KeyError, TypeError) as exc:
        raise CheckFailed(f"grid session payload unparseable: {exc}")
    return session_id, grid_cdp_ws_url(session_id)


def delete_grid_session(session_id):
    """Free the single grid slot; best-effort, never raises."""
    req = urllib.request.Request(
        BROWSER_HTTP.rstrip("/") + f"/session/{session_id}",
        method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def stage_grid_ready():
    """Stage 1: Selenium Grid reports a ready node with a free slot."""
    status, body = http_get(BROWSER_HTTP.rstrip("/") + "/status")
    if status != 200:
        raise CheckFailed(f"grid status HTTP {status}")
    try:
        value = json.loads(body.decode("utf-8"))["value"]
    except (ValueError, KeyError) as exc:
        raise CheckFailed(f"grid status payload unparseable: {exc}")
    if not value.get("ready"):
        raise CheckFailed("grid not ready")
    nodes = value.get("nodes", [])
    free = sum(1 for node in nodes for slot in node.get("slots", [])
               if slot.get("session") is None)
    if not free:
        raise CheckFailed(
            "no free grid slot (single session; a leaked session may "
            "hold it — DELETE /session/<id>)")
    return {"nodes": len(nodes), "free_slots": free}


def stage_cdp_snapshot(out_dir):
    """Stage 2: Grid session, CDP connect, navigate the preview,
    read DOM metrics. The session is always deleted so the single
    grid slot never wedges."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise MissingTool("playwright not installed (pip install playwright)")
    session_id, endpoint = create_grid_session()
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    endpoint, timeout=10000)
            except Exception as exc:
                raise CheckFailed(f"CDP connect failed: {exc}")
            context = (browser.contexts[0] if browser.contexts
                       else browser.new_context(viewport={"width": 1280,
                                                          "height": 800}))
            page = context.new_page()
            try:
                page.goto(PREVIEW_URL, wait_until="domcontentloaded",
                          timeout=20000)
                metrics = page.evaluate(
                    """() => ({
                        dom_node_count: document.querySelectorAll('*').length,
                        title: document.title
                    })""")
                shot = os.path.join(out_dir, "preflight_viewport.png")
                page.screenshot(path=shot, full_page=False)
            finally:
                page.close()
                browser.close()
    finally:
        delete_grid_session(session_id)
    if not metrics.get("dom_node_count"):
        raise CheckFailed("preview page has no DOM nodes")
    return {"dom_node_count": metrics["dom_node_count"],
            "title": metrics.get("title", ""),
            "screenshot": shot}


def stage_screenshot_integrity(png_path):
    """Stage 3: the captured screenshot is a non-empty PNG."""
    with open(png_path, "rb") as handle:
        head = handle.read(8)
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
    if head != b"\x89PNG\r\n\x1a\n":
        raise CheckFailed("screenshot is not a PNG")
    if size < 1024:
        raise CheckFailed(f"screenshot suspiciously small ({size}b)")
    return {"bytes": size}


def _agent_state():
    status, body = http_get(AGENT_API.rstrip("/") + "/state")
    if status != 200:
        raise CheckFailed(f"agent state HTTP {status}")
    return json.loads(body.decode("utf-8"))


def stage_pause_halts():
    """Stage 4: POST pause flips the shared state the pipeline honors
    (the wait itself is covered by tests/test_co_browsing_pause.py)."""
    status, _ = http_post(AGENT_API.rstrip("/") + "/pause")
    if status != 200:
        raise CheckFailed(f"pause HTTP {status}")
    state = _agent_state()
    if state.get("state") != "PAUSED_BY_USER":
        raise CheckFailed(f"pause did not stick: {state}")
    if not state.get("requires_dom_refresh"):
        raise CheckFailed("pause did not flag a DOM refresh")
    return {"state": state["state"]}


def stage_resume_refresh():
    """Stage 5: POST resume returns to RUNNING with a re-snapshot
    scheduled (fresh DOM before the next automated action)."""
    status, _ = http_post(AGENT_API.rstrip("/") + "/resume")
    if status != 200:
        raise CheckFailed(f"resume HTTP {status}")
    state = _agent_state()
    if state.get("state") != "RUNNING":
        raise CheckFailed(f"resume did not stick: {state}")
    if not state.get("requires_dom_refresh"):
        raise CheckFailed("resume did not schedule a DOM re-snapshot")
    return {"state": state["state"]}


def _td_supports_preview():
    try:
        proc = subprocess.run(shlex.split(TD_BIN) + ["audit-impact", "--help"],
                              capture_output=True, text=True, timeout=30,
                              cwd=TD_CWD)
    except (FileNotFoundError, OSError) as exc:
        raise MissingTool(f"td entrypoint missing ({TD_BIN}): {exc}")
    return "--preview-url" in (proc.stdout or "")


def stage_td_impact(repo, out_dir):
    """Stage 6: td audit-impact attaches a visual_diff from the live
    head (stdout stays pure JSON)."""
    if not _td_supports_preview():
        raise MissingTool(
            "installed td predates --preview-url (stale td); set TD_BIN "
            "to a checkout entrypoint, e.g. TD_BIN='python3 "
            "dev_tools/cli.py' TD_CWD=/srv/repos/boomtick/cli")
    worktree = os.path.join(out_dir, "vis-preflight")
    css_rel = os.path.join("frontend", "src", "index.css")
    css_abs = os.path.join(repo, css_rel)
    if not os.path.isfile(css_abs):
        raise MissingTool(f"no UI file to probe at {css_abs}")
    subprocess.run(["git", "worktree", "add", "--detach", worktree,
                    "origin/main"], capture_output=True, text=True, timeout=60,
                   cwd=repo, check=False)
    try:
        if subprocess.run(["git", "diff", "--name-only", "origin/main",
                           "--", "."], capture_output=True, text=True,
                          timeout=60, cwd=worktree).returncode != 0:
            raise MissingTool(f"{worktree} is not a git worktree of {repo}")
        with open(os.path.join(worktree, css_rel), "a",
                  encoding="utf-8") as handle:
            handle.write("\n/* vis-preflight probe */\n")
        cmd = (shlex.split(TD_BIN)
               + ["audit-impact", "--worktree", worktree,
                  "--preview-url", PREVIEW_URL, "--json"])
        env = dict(os.environ)
        # browser_cdp opens its own Grid session from this base URL.
        env.setdefault("BROWSER_ENDPOINT", BROWSER_HTTP)
        if TD_CWD:
            # Checkout entrypoints (TD_BIN="python3 dev_tools/cli.py")
            # must resolve dev_tools from the checkout, not the stale
            # installed td in /opt/venvs/td.
            env["PYTHONPATH"] = TD_CWD + (os.pathsep + env["PYTHONPATH"]
                                          if env.get("PYTHONPATH") else "")
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=180, cwd=TD_CWD or repo, env=env)
        try:
            payload = json.loads((proc.stdout or "").strip())
        except ValueError:
            raise CheckFailed(
                f"td stdout not pure JSON (rc={proc.returncode}, "
                f"stderr={proc.stderr[-300:]})")
        visual = payload.get("visual_diff") or {}
        if not visual.get("reachable"):
            raise CheckFailed(
                f"visual snapshot unreachable: {visual.get('error')}")
        if not visual.get("dom_node_count"):
            raise CheckFailed("visual snapshot has no DOM metrics")
        return {"dom_node_count": visual["dom_node_count"],
                "mismatch_percentage": visual.get("mismatch_percentage"),
                "passed": payload.get("passed")}
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", worktree],
                       capture_output=True, timeout=60, cwd=repo)


STAGES = [
    ("grid_ready", lambda ctx: stage_grid_ready()),
    ("cdp_snapshot", lambda ctx: stage_cdp_snapshot(ctx["dir"])),
    ("screenshot_integrity",
     lambda ctx: stage_screenshot_integrity(ctx["snapshot"]["screenshot"])),
    ("pause_halts", lambda _ctx: stage_pause_halts()),
    ("resume_refresh", lambda _ctx: stage_resume_refresh()),
    ("td_impact",
     lambda ctx: stage_td_impact(ctx["repo"], ctx["dir"])),
]


def run_preflight(repo):
    results = {}
    with tempfile.TemporaryDirectory(prefix="cobrowse-preflight-") as tmp:
        ctx = {"dir": tmp, "repo": repo, "snapshot": {}}
        for name, fn in STAGES:
            started = time.time()
            try:
                detail = fn(ctx)
            except MissingTool as exc:
                results[name] = {"status": "ENV_MISSING",
                                 "detail": str(exc),
                                 "elapsed_s": round(time.time() - started, 1)}
                break
            except CheckFailed as exc:
                results[name] = {"status": "FAIL", "detail": str(exc),
                                 "elapsed_s": round(time.time() - started, 1)}
                break
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                results[name] = {"status": "ERROR",
                                 "detail": f"{type(exc).__name__}: {exc}",
                                 "elapsed_s": round(time.time() - started, 1)}
                break
            if name == "cdp_snapshot":
                ctx["snapshot"] = detail
            results[name] = {"status": "PASS", "detail": detail,
                             "elapsed_s": round(time.time() - started, 1)}
    return results


def main(argv):
    repo = argv[1] if len(argv) > 1 else REPO
    results = run_preflight(repo)
    failed = [n for n, r in results.items() if r["status"] in ("FAIL", "ERROR")]
    missing = [n for n, r in results.items() if r["status"] == "ENV_MISSING"]
    for name, result in results.items():
        print(f"[{result['status']}] {name} "
              f"({result['elapsed_s']}s): {result['detail']}")
    print(json.dumps({"ok": not failed and not missing, "stages": results},
                     indent=2))
    if missing and not failed:
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

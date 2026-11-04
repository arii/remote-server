"""Pin the TLS-edge liveness watcher (#537).

oauth2-proxy has no in-container healthcheck (its image ships no
shell/curl), but GET /ping answers 200 unauthenticated — so a host cron
job probes it and restarts the proxy after N consecutive failures. This
pins the watcher script, its installer, and the durability-doc claim
together (config/doc drift is what #537 was filed against).
"""
import http.server
import os
import stat
import subprocess
import tempfile
import threading
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
WATCH = os.path.join(ROOT, "scripts", "edge-liveness-watch.sh")
INSTALL = os.path.join(ROOT, "scripts", "install-edge-liveness-cron.sh")
DOC = os.path.join(ROOT, "docs", "durability.md")


class _PingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            body = b"OK"
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start_stub():
    server = http.server.HTTPServer(("127.0.0.1", 0), _PingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run_watch(env, tmpdir):
    full_env = dict(os.environ)
    full_env.update(env)
    full_env["EDGE_LIVENESS_STATE"] = os.path.join(tmpdir, "failures")
    return subprocess.run(
        ["bash", os.path.abspath(WATCH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=full_env,
        timeout=60,
    )


class TestEdgeLiveness(unittest.TestCase):
    def test_scripts_present_and_valid(self):
        for path in (WATCH, INSTALL):
            with self.subTest(script=path):
                self.assertTrue(os.path.isfile(path))
                mode = os.stat(path).st_mode
                self.assertTrue(mode & stat.S_IXUSR, "script must be executable")
                res = subprocess.run(["bash", "-n", path],
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_watcher_probes_ping_and_restarts(self):
        with open(WATCH, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("/ping", content)
        self.assertIn("docker restart", content)

    def test_installer_wires_host_cron(self):
        with open(INSTALL, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("/etc/cron.d/edge-liveness-watch", content)
        self.assertIn("/usr/local/bin/edge-liveness-watch", content)

    def test_doc_describes_coverage(self):
        with open(DOC, encoding="utf-8") as f:
            doc = f.read()
        self.assertNotIn("no plain probe", doc)
        self.assertIn("edge-liveness-watch", doc)
        self.assertIn("/ping", doc)

    def test_healthy_edge_clears_state(self):
        server = _start_stub()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                state = os.path.join(tmpdir, "failures")
                with open(state, "w", encoding="utf-8") as f:
                    f.write("2")
                url = "http://127.0.0.1:%d/ping" % server.server_port
                res = _run_watch({"EDGE_LIVENESS_URL": url}, tmpdir)
                self.assertEqual(res.returncode, 0, res.stdout)
                self.assertFalse(os.path.exists(state))
        finally:
            server.shutdown()
            server.server_close()

    def test_failures_count_then_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Fake docker: `ps -q` reports one container, `restart` is logged.
            bindir = os.path.join(tmpdir, "bin")
            os.mkdir(bindir)
            docker_log = os.path.join(tmpdir, "docker.log")
            with open(os.path.join(bindir, "docker"), "w", encoding="utf-8") as f:
                f.write('#!/usr/bin/env bash\n'
                        'echo "$@" >> "%s"\n'
                        'if [[ "$1" == "ps" ]]; then echo deadbeef1234; fi\n'
                        % docker_log)
            os.chmod(os.path.join(bindir, "docker"), 0o755)
            env = {"EDGE_LIVENESS_URL": "http://127.0.0.1:1/ping",
                   "EDGE_LIVENESS_TIMEOUT": "2",
                   "EDGE_LIVENESS_MAX_FAILURES": "3",
                   "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
            state = os.path.join(tmpdir, "failures")
            for expect in ("1", "2"):
                res = _run_watch(env, tmpdir)
                self.assertEqual(res.returncode, 0, res.stdout)
                with open(state, encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), expect)
            self.assertFalse(os.path.exists(docker_log),
                             "no restart before the threshold")
            res = _run_watch(env, tmpdir)
            self.assertEqual(res.returncode, 0, res.stdout)
            self.assertIn("restarted oauth2-proxy", res.stdout)
            self.assertFalse(os.path.exists(state))
            with open(docker_log, encoding="utf-8") as f:
                self.assertIn("restart deadbeef1234", f.read())


if __name__ == "__main__":
    unittest.main()

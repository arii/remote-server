"""Pin the certbot deploy-hook health verification (#527).

The hook restarts oauth2-proxy after cert renewal; it must verify the edge
actually came back (container running + TLS probe answers) and fail loudly
when it did not — a restart the daemon merely ACCEPTED is not success.

Runs the real hook with a fake `docker` on PATH and a stub TLS server, so
no real renewal or container is needed.
"""
import http.server
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import unittest

HOOK = os.path.abspath(
    os.path.join("scripts", "certbot-deploy-hook.sh"))


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


def _start_tls_stub(cert, key):
    server = http.server.HTTPServer(("127.0.0.1", 0), _PingHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _make_cert(tmpdir):
    cert = os.path.join(tmpdir, "cert.pem")
    key = os.path.join(tmpdir, "key.pem")
    res = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", cert, "-days", "2",
         "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError("openssl cert generation failed: " + res.stderr)
    return cert, key


def _fake_docker(bindir, log, running="true", present=True):
    with open(os.path.join(bindir, "docker"), "w", encoding="utf-8") as f:
        f.write('#!/usr/bin/env bash\n'
                'echo "$@" >> "%s"\n'
                'if [[ "$1" == "ps" ]]; then %s; fi\n'
                'if [[ "$1" == "restart" ]]; then echo deadbeef1234; fi\n'
                'if [[ "$1" == "inspect" ]]; then echo "%s"; fi\n'
                % (log, 'echo deadbeef1234' if present else 'true', running))
    os.chmod(os.path.join(bindir, "docker"), 0o755)


def _run_hook(env, timeout=90):
    full_env = dict(os.environ)
    # Scrub anything that could resolve a real probe target.
    for var in ("CERTBOT_HOOK_URL", "TERMINAL_DOMAIN", "TERMINAL_PORT",
                "REMOTE_SERVER_DIR"):
        full_env.pop(var, None)
    full_env.update(env)
    return subprocess.run(
        ["bash", HOOK],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=full_env,
        timeout=timeout,
    )


class TestCertbotDeployHook(unittest.TestCase):
    def test_script_present_and_valid(self):
        self.assertTrue(os.path.isfile(HOOK))
        mode = os.stat(HOOK).st_mode
        self.assertTrue(mode & stat.S_IXUSR, "hook must be executable")
        res = subprocess.run(["bash", "-n", HOOK],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_healthy_restart_logs_served_expiry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cert, key = _make_cert(tmpdir)
            server = _start_tls_stub(cert, key)
            try:
                bindir = os.path.join(tmpdir, "bin")
                os.mkdir(bindir)
                log = os.path.join(tmpdir, "docker.log")
                _fake_docker(bindir, log, running="true")
                url = "https://127.0.0.1:%d/ping" % server.server_port
                env = {"CERTBOT_HOOK_URL": url,
                       "SSL_CERT_FILE": cert,
                       "CERTBOT_HOOK_TIMEOUT": "20",
                       "CERTBOT_HOOK_INTERVAL": "1",
                       "REMOTE_SERVER_DIR": tmpdir,
                       "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
                res = _run_hook(env)
                self.assertEqual(res.returncode, 0, res.stdout)
                self.assertIn("restarted oauth2-proxy", res.stdout)
                # Proves the NEW cert is live, not just the old process.
                self.assertIn("serving renewed certificate: notAfter=",
                              res.stdout)
                with open(log, encoding="utf-8") as f:
                    calls = f.read()
                self.assertIn("restart deadbeef1234", calls)
                self.assertIn("inspect", calls)
            finally:
                server.shutdown()
                server.server_close()

    def test_crashed_container_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bindir = os.path.join(tmpdir, "bin")
            os.mkdir(bindir)
            log = os.path.join(tmpdir, "docker.log")
            _fake_docker(bindir, log, running="false")
            env = {"CERTBOT_HOOK_URL": "http://127.0.0.1:1/ping",
                   "CERTBOT_HOOK_TIMEOUT": "4",
                   "CERTBOT_HOOK_INTERVAL": "1",
                   "REMOTE_SERVER_DIR": tmpdir,
                   "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
            res = _run_hook(env)
            self.assertNotEqual(res.returncode, 0, res.stdout)
            self.assertIn("unhealthy", res.stdout)
            self.assertIn("not running", res.stdout)

    def test_dark_edge_fails_loudly(self):
        # Restart accepted and container reports running, but nothing
        # answers the TLS probe: the hook must still fail.
        with tempfile.TemporaryDirectory() as tmpdir:
            bindir = os.path.join(tmpdir, "bin")
            os.mkdir(bindir)
            log = os.path.join(tmpdir, "docker.log")
            _fake_docker(bindir, log, running="true")
            url = "https://127.0.0.1:%d/ping" % _closed_port()
            env = {"CERTBOT_HOOK_URL": url,
                   "CERTBOT_HOOK_TIMEOUT": "4",
                   "CERTBOT_HOOK_INTERVAL": "1",
                   "REMOTE_SERVER_DIR": tmpdir,
                   "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
            res = _run_hook(env)
            self.assertNotEqual(res.returncode, 0, res.stdout)
            self.assertIn("unhealthy", res.stdout)
            self.assertIn("not serving", res.stdout)

    def test_no_container_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bindir = os.path.join(tmpdir, "bin")
            os.mkdir(bindir)
            log = os.path.join(tmpdir, "docker.log")
            _fake_docker(bindir, log, present=False)
            env = {"CERTBOT_HOOK_TIMEOUT": "4",
                   "CERTBOT_HOOK_INTERVAL": "1",
                   "REMOTE_SERVER_DIR": tmpdir,
                   "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
            res = _run_hook(env)
            self.assertEqual(res.returncode, 0, res.stdout)
            self.assertIn("nothing to restart", res.stdout)

    def test_no_probe_target_verifies_container_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bindir = os.path.join(tmpdir, "bin")
            os.mkdir(bindir)
            log = os.path.join(tmpdir, "docker.log")
            _fake_docker(bindir, log, running="true")
            env = {"CERTBOT_HOOK_TIMEOUT": "4",
                   "CERTBOT_HOOK_INTERVAL": "1",
                   "REMOTE_SERVER_DIR": tmpdir,
                   "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
            res = _run_hook(env)
            self.assertEqual(res.returncode, 0, res.stdout)
            self.assertIn("verifying container state only", res.stdout)


if __name__ == "__main__":
    unittest.main()

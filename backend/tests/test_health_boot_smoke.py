"""Boot smoke test (#1508): uvicorn serves backend.server:app and /health is ok.

Runs the same module path as the image entrypoint with the same
PYTHONPATH contract as the Dockerfile (repo root + deep-agent/, which
backend imports directly: runs, pricing, model_router, s5tools). The
server runs in a subprocess on a free port and is always terminated.
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOOT_TIMEOUT_S = 60


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HealthBootSmokeTest(unittest.TestCase):
    def test_uvicorn_boots_and_health_is_ok(self):
        port = _free_port()
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([REPO_ROOT, os.path.join(REPO_ROOT, "deep-agent")])
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        url = f"http://127.0.0.1:{port}/health"
        body = None
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT_S
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    self.fail(f"uvicorn exited {proc.returncode} before /health:\n{proc.stdout.read()}")
                try:
                    with urllib.request.urlopen(url, timeout=2) as resp:
                        body = json.load(resp)
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.25)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        self.assertIsNotNone(body, f"/health not reachable within {BOOT_TIMEOUT_S}s")
        self.assertIs(body["ok"], True)


if __name__ == "__main__":
    unittest.main()

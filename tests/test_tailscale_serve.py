#!/usr/bin/env python3
"""Opt-in Tailscale serve gate (user directive 2026-11-08).

scripts/tailscale-entrypoint.sh joins the tailnet as a VPN on every boot
and additionally runs `tailscale serve` ONLY when TS_SERVE=1 (default off:
VPN-only, no funnel). These tests execute the real entrypoint against
stubbed `tailscale`/`seq`/`sleep` binaries on PATH and assert which
tailnet commands it issued; the compose test pins the default-off env
passthrough exactly.

Hermetic: no network, no daemon. The entrypoint execs
/usr/local/bin/tailscaled by absolute path, which cannot be stubbed, so
the execution tests skip where that binary exists (live serving is
verified by hand on the box); the wait loop is collapsed with stubbed
seq/sleep, which only accelerates timing and cannot change gating.
"""
import os
import subprocess
import tempfile
import unittest

ENTRYPOINT = os.path.abspath(os.path.join("scripts", "tailscale-entrypoint.sh"))
COMPOSE = "docker-compose.yml"

TAILSCALED = "/usr/local/bin/tailscaled"

STUB_TAILSCALE = """#!/bin/sh
echo "$*" >> "$TAILSCALE_LOG"
case "$1" in
  ip) echo "100.64.0.1" ;;
esac
exit 0
"""
STUB_SEQ = "#!/bin/sh\necho 1\n"
STUB_SLEEP = "#!/bin/sh\nexit 0\n"


def _commands(log_path):
    with open(log_path, encoding="utf-8") as f:
        return [line.split() for line in f.read().splitlines() if line.strip()]


def _serve_calls(commands):
    return [c for c in commands if c[0:1] == ["serve"] and c[1:2] != ["status"]]


class TestTailscaleServeGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        for name, body in (("tailscale", STUB_TAILSCALE),
                           ("seq", STUB_SEQ),
                           ("sleep", STUB_SLEEP)):
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.log = os.path.join(self.tmp.name, "calls.log")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, extra_env=None, drop=()):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["TAILSCALE_LOG"] = self.log
        env["TS_AUTHKEY"] = "test-authkey"
        for var in ("TS_SERVE", "TS_SERVE_TARGET", "TERMINAL_PORT"):
            env.pop(var, None)
        for var in drop:
            env.pop(var, None)
        env.update(extra_env or {})
        return subprocess.run(
            ["sh", ENTRYPOINT], capture_output=True, text=True,
            env=env, timeout=60,
        )

    @unittest.skipIf(os.path.exists(TAILSCALED),
                     "tailscaled installed: absolute-path exec cannot be "
                     "stubbed, would join a real tailnet")
    def test_default_off_serves_nothing(self):
        """Unset TS_SERVE must join the VPN without serving the edge."""
        # TERMINAL_PORT is always configured in production compose; setting
        # it here forces the test to discriminate the TS_SERVE gate itself
        # rather than the TERMINAL_PORT fail-closed guard below it.
        self._run({"TERMINAL_PORT": "8443"})
        commands = _commands(self.log)
        self.assertTrue(any(c[0:1] == ["up"] for c in commands),
                        "entrypoint must always join the tailnet (VPN)")
        self.assertEqual(_serve_calls(commands), [],
                         "default-off entrypoint must not invoke serve")

    @unittest.skipIf(os.path.exists(TAILSCALED),
                     "tailscaled installed: absolute-path exec cannot be "
                     "stubbed, would join a real tailnet")
    def test_serve_opt_in_default_target(self):
        """TS_SERVE=1 must serve the proxy listener over the tailnet."""
        self._run({"TS_SERVE": "1", "TERMINAL_PORT": "8443"})
        self.assertEqual(
            _serve_calls(_commands(self.log)),
            [["serve", "--https=443", "--set-path", "/",
              "http://127.0.0.1:8443"]])

    @unittest.skipIf(os.path.exists(TAILSCALED),
                     "tailscaled installed: absolute-path exec cannot be "
                     "stubbed, would join a real tailnet")
    def test_serve_target_override(self):
        """TS_SERVE_TARGET must override the default serve target."""
        self._run({"TS_SERVE": "1", "TERMINAL_PORT": "8443",
                   "TS_SERVE_TARGET": "http://127.0.0.1:9000"})
        self.assertEqual(
            _serve_calls(_commands(self.log)),
            [["serve", "--https=443", "--set-path", "/",
              "http://127.0.0.1:9000"]])

    @unittest.skipIf(os.path.exists(TAILSCALED),
                     "tailscaled installed: absolute-path exec cannot be "
                     "stubbed, would join a real tailnet")
    def test_serve_requires_terminal_port(self):
        """TS_SERVE=1 without TERMINAL_PORT must fail closed, no serve."""
        proc = self._run({"TS_SERVE": "1"})
        self.assertNotEqual(proc.returncode, 0,
                            "opt-in serve without a port must refuse")
        self.assertEqual(_serve_calls(_commands(self.log)), [],
                         "refused serve must not reach the tailnet CLI")


class TestTailscaleComposePassthrough(unittest.TestCase):
    def test_tailscale_env_defaults_off(self):
        """tailscale service must pass TS_SERVE through with an empty default.

        An empty default keeps the tailnet VPN-only unless the operator
        opts in; hardcoding serve on (or dropping the passthrough) would
        expose the edge on every boot (user directive 2026-11-08).
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        service = None
        env_lines = []
        entrypoints = []
        import re
        for line in lines:
            if line and not line.startswith((" ", "\t", "#")):
                service = None
                continue
            svc = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
            if svc:
                service = svc.group(1)
                continue
            if service == "tailscale":
                stripped = line.strip().split("#")[0].rstrip()
                if stripped.startswith("TS_SERVE"):
                    env_lines.append(stripped)
                if stripped.startswith("entrypoint:"):
                    entrypoints.append(stripped)
        self.assertEqual(
            env_lines,
            ["TS_SERVE: ${TS_SERVE:-}",
             "TS_SERVE_TARGET: ${TS_SERVE_TARGET:-}"],
            "tailscale env must pass TS_SERVE through default-off exactly once")
        self.assertEqual(
            entrypoints,
            ['entrypoint: ["/usr/local/bin/tailscale-entrypoint.sh"]'],
            "tailscale service must run the gating entrypoint")

    def test_entrypoint_syntax(self):
        proc = subprocess.run(
            ["sh", "-n", ENTRYPOINT], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, "sh -n failed: %s" % proc.stderr)


if __name__ == "__main__":
    unittest.main()

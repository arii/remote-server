"""Regression tests: fail-closed MCP health check before dispatch (#1215).

A dead endpoint raises MCPHealthError carrying the endpoint identity
before any turn starts (fast); a live endpoint dispatches. No fallback
or alternate-endpoint path exists on the guard.

NOTE (independent tester): no MCP dispatch code calls this guard yet —
s5tools.py holds no dispatcher — so the guard is an untested contract
until a dispatch entry point wires require_mcp_healthy in. These tests
pin the guard itself; the wiring stays open.

Run: PYTHONPATH=./deep-agent python3 -m unittest tests.test_mcp_health_guard
"""

import inspect
import io
import json
import socket
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import s5tools

ENDPOINT = "http://127.0.0.1:18099"


def good_reply(result=None):
    payload = {
        "jsonrpc": "2.0",
        "id": "s5-healthcheck",
        "result": {"ok": True} if result is None else result,
    }
    return json.dumps(payload).encode()


def urlopen_double(raw=None, status=200, exc=None):
    """urllib.request.urlopen replacement returning one canned reply."""
    if exc is not None:

        def _raise(_req, timeout=None):
            raise exc

        return _raise
    resp = mock.MagicMock()
    resp.status = status
    resp.read.return_value = raw
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = resp
    ctx.__exit__.return_value = False

    def _reply(_req, timeout=None):
        return ctx

    return _reply


class TestMcpPingDead(unittest.TestCase):
    def _assert_unhealthy(self, exc, *fragments):
        with mock.patch.object(urllib.request, "urlopen", side_effect=exc):
            with self.assertRaises(s5tools.MCPHealthError) as ctx:
                s5tools.mcp_ping(ENDPOINT, timeout=1)
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertIn(ENDPOINT, str(ctx.exception))
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))

    def test_refused_endpoint_raises_with_identity(self):
        self._assert_unhealthy(
            urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        )

    def test_timeout_raises_with_identity(self):
        self._assert_unhealthy(socket.timeout("timed out"))

    def test_http_error_raises_with_identity_and_code(self):
        exc = urllib.error.HTTPError(
            ENDPOINT, 503, "Service Unavailable", {}, io.BytesIO(b"down")
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=exc):
            with self.assertRaises(s5tools.MCPHealthError) as ctx:
                s5tools.mcp_ping(ENDPOINT, timeout=1)
        self.assertIn(ENDPOINT, str(ctx.exception))
        self.assertIn("503", str(ctx.exception))

    def test_non_json_reply_raises_with_identity(self):
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urlopen_double(raw=b"not json"),
        ):
            with self.assertRaises(s5tools.MCPHealthError) as ctx:
                s5tools.mcp_ping(ENDPOINT, timeout=1)
        self.assertIn(ENDPOINT, str(ctx.exception))

    def test_rpc_error_payload_raises_with_identity(self):
        raw = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "s5-healthcheck",
                "error": {"code": -32601, "message": "no such method"},
            }
        ).encode()
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urlopen_double(raw=raw),
        ):
            with self.assertRaises(s5tools.MCPHealthError) as ctx:
                s5tools.mcp_ping(ENDPOINT, timeout=1)
        self.assertIn(ENDPOINT, str(ctx.exception))

    def test_bad_reply_id_raises_with_identity(self):
        raw = json.dumps(
            {"jsonrpc": "2.0", "id": "someone-else", "result": {}}
        ).encode()
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urlopen_double(raw=raw),
        ):
            with self.assertRaises(s5tools.MCPHealthError) as ctx:
                s5tools.mcp_ping(ENDPOINT, timeout=1)
        self.assertIn(ENDPOINT, str(ctx.exception))

    def test_dead_endpoint_fails_fast(self):
        # Mocked refusal lands on the ~0.01s scale: the gate must run
        # before any turn starts, not after a long dispatch hangs.
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        ):
            start = time.monotonic()
            with self.assertRaises(s5tools.MCPHealthError):
                s5tools.mcp_ping(ENDPOINT, timeout=5)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0)

    def test_real_refused_port_fails_fast_with_identity(self):
        # Unmocked: nothing listens on discard-port 9, so this is a
        # real ECONNREFUSED through the real guard path.
        endpoint = "http://127.0.0.1:9"
        start = time.monotonic()
        with self.assertRaises(s5tools.MCPHealthError) as ctx:
            s5tools.mcp_ping(endpoint, timeout=2)
        elapsed = time.monotonic() - start
        self.assertIn(endpoint, str(ctx.exception))
        self.assertLess(elapsed, 2.0)

    def test_bad_endpoints_rejected(self):
        for bad in ("", "   ", "ftp://x/y", "http://", None, 12345):
            with self.assertRaises(ValueError, msg=repr(bad)):
                s5tools.mcp_ping(bad, timeout=1)
            with self.assertRaises(ValueError, msg=repr(bad)):
                s5tools.require_mcp_healthy(bad, timeout=1)


class TestMcpPingLive(unittest.TestCase):
    def test_live_endpoint_returns_result(self):
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urlopen_double(raw=good_reply()),
        ) as urlopen:
            self.assertEqual(s5tools.mcp_ping(ENDPOINT, timeout=3), {"ok": True})
            _req, kwargs = urlopen.call_args[0][0], urlopen.call_args[1]
            self.assertEqual(kwargs.get("timeout"), 3)

    def test_require_mcp_healthy_returns_ping_result(self):
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urlopen_double(raw=good_reply({"v": 1})),
        ):
            self.assertEqual(s5tools.require_mcp_healthy(ENDPOINT, timeout=1), {"v": 1})

    def test_require_mcp_healthy_raises_before_turn(self):
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError(OSError("down")),
        ):
            with self.assertRaises(s5tools.MCPHealthError) as ctx:
                s5tools.require_mcp_healthy(ENDPOINT, timeout=1)
        self.assertIn(ENDPOINT, str(ctx.exception))


class TestNoFallback(unittest.TestCase):
    def test_no_alternate_endpoint_logic(self):
        src = (
            inspect.getsource(s5tools.mcp_ping)
            + inspect.getsource(s5tools.require_mcp_healthy)
        ).lower()
        for token in (
            "fallback to",
            "failover",
            "alternate endpoint",
            "backup endpoint",
            "try another",
            "secondary endpoint",
            "next endpoint",
        ):
            self.assertNotIn(token, src)

    def test_every_except_raises_fail_closed(self):
        # No silent except-pass: each handler in the guard converts to
        # MCPHealthError instead of swallowing the outage.
        src = inspect.getsource(s5tools.mcp_ping)
        self.assertGreaterEqual(src.count("raise MCPHealthError"), src.count("except"))

    def test_health_error_surfaces_to_turn_error_paths(self):
        self.assertTrue(issubclass(s5tools.MCPHealthError, RuntimeError))


if __name__ == "__main__":
    unittest.main()

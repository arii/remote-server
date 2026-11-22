"""Tests for container restart-loop detection (#1514), fake docker output."""

import json
import os
import tempfile
import unittest

from backend import ledger
from backend.api.routers import attention
from backend.core import container_restarts as cr


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


PS = json.dumps(
    [
        {"Name": "remote-tailscale-1", "Service": "tailscale", "State": "restarting"},
        {"Name": "remote-caddy-1", "Service": "caddy", "State": "running"},
    ]
)


def fake_docker(counts, inspect_rc=0):
    def run(cmd, **kw):
        if cmd[:2] == ["docker", "inspect"]:
            if inspect_rc:
                return FakeProc(stderr="boom", returncode=inspect_rc)
            names = cmd[4:]
            return FakeProc(stdout="".join(f"/{n} {counts[n]}\n" for n in names))
        return FakeProc(stdout=PS)

    return run


class ContainerRestartsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {
            "BUTTONS_CONTAINER_RESTARTS_PATH": os.path.join(self.tmp, "r.json"),
            "BUTTONS_LEDGER_PATH": os.path.join(self.tmp, "ledger.jsonl"),
        }
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def tick(self, tail, caddy, now):
        counts = {"remote-tailscale-1": tail, "remote-caddy-1": caddy}
        return cr.record_container_restarts(run=fake_docker(counts), now=now)

    def test_first_sighting_is_baseline_only(self):
        self.assertEqual(self.tick(40, 0, 1000.0), [])
        self.assertEqual(ledger.read("system"), [])

    def test_growth_writes_one_history_row_per_container(self):
        self.tick(40, 0, 1000.0)
        grew = self.tick(43, 0, 1060.0)
        self.assertEqual(grew, [{"name": "remote-tailscale-1", "restarts": 3}])
        rows = ledger.read("system")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["event"], "container remote-tailscale-1 restarted on its own"
        )
        self.assertEqual(rows[0]["restarts"], 3)
        self.assertEqual(self.tick(43, 0, 1120.0), [])
        self.assertEqual(len(ledger.read("system")), 1)

    def test_recreated_container_resets_baseline(self):
        self.tick(40, 0, 1000.0)
        self.assertEqual(self.tick(0, 0, 1060.0), [])
        self.assertEqual(ledger.read("system"), [])

    def test_loop_threshold_and_window(self):
        self.tick(0, 0, 1000.0)
        self.tick(2, 0, 1060.0)
        self.assertEqual(cr.list_loops(now=1100.0), [])
        self.tick(3, 0, 1120.0)
        loops = cr.list_loops(now=1200.0)
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["name"], "remote-tailscale-1")
        self.assertEqual(loops[0]["service"], "tailscale")
        self.assertEqual(loops[0]["restarts"], 3)
        self.assertEqual(cr.list_loops(now=1060.0 + 3601), [])

    def test_docker_failure_raises_and_keeps_state(self):
        self.tick(0, 0, 1000.0)
        with self.assertRaises(RuntimeError):
            cr.record_container_restarts(run=fake_docker({}, inspect_rc=1), now=1060.0)
        self.assertEqual(
            self.tick(1, 0, 1120.0), [{"name": "remote-tailscale-1", "restarts": 1}]
        )

    def test_attention_item_for_loop(self):
        self.tick(0, 0, 1000.0)
        self.tick(5, 0, 1060.0)
        orig = cr.list_loops
        cr.list_loops = lambda now=None: orig(now=1100.0)
        try:
            items = attention._assemble_items("remote-server", 1100.0)
        finally:
            cr.list_loops = orig
        item = [i for i in items if i["kind"] == "container-restart-loop"]
        self.assertEqual(len(item), 1)
        self.assertEqual(item[0]["title"], "remote-tailscale-1 keeps restarting")
        self.assertIn("5 times in the last hour", item[0]["detail"])
        self.assertEqual(
            item[0]["action"]["href"], "api/system/containers/tailscale/logs"
        )
        self.assertFalse(item[0]["action"]["destructive"])


if __name__ == "__main__":
    unittest.main()

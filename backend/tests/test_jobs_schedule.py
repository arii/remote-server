"""Tests for jobs action schedules (issue #655).

Scheduling any non-destructive jobs action on the shared tick:
store + endpoints + claim-advance + fire-through-launch. Subprocess
is never touched: firing tests stub _launch, repo validation tests
stub _repo_target.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.api.routers import jobs as jobs_mod


def _registry_snapshot():
    reg = jobs_mod.registry
    with reg._lock:
        return (dict(reg.runs), reg.active_id, reg._seq)


def _registry_restore(snap):
    runs, active_id, seq = snap
    reg = jobs_mod.registry
    with reg._lock:
        reg.runs.clear()
        reg.runs.update(runs)
        reg.active_id = active_id
        reg._seq = seq


class ActionScheduleStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sched.json")
        self.old = os.environ.get("BUTTONS_JOBS_SCHEDULE_PATH")
        os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = self.path
        self.reg = _registry_snapshot()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BUTTONS_JOBS_SCHEDULE_PATH", None)
        else:
            os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = self.old
        _registry_restore(self.reg)
        self.tmp.cleanup()

    def _create(self, **kw):
        body = {"action": "logs", "repo": "remote-server",
                "cadence": "daily"}
        body.update(kw)
        with patch("backend.api.routers.issues._ensure_schedule_ticker") \
                as tick:
            out = jobs_mod.create_action_schedule(
                jobs_mod.ActionScheduleRequest(**body))
        self.assertEqual(tick.call_count, 1)
        return out["schedule"]

    def test_create_list_delete_roundtrip(self):
        from fastapi import HTTPException

        before = time.time()
        sched = self._create()
        self.assertRegex(sched["id"], r"^[0-9a-f]{12}$")
        self.assertEqual(
            (sched["action"], sched["repo"], sched["cadence"]),
            ("logs", "remote-server", "daily"))
        self.assertEqual(sched["td_argv"], [])
        self.assertIsNone(sched["last_run"])
        self.assertEqual(sched["last_result"], {})
        self.assertGreaterEqual(sched["next_run"], before + 24 * 3600)
        listed = jobs_mod.list_action_schedules()["schedules"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], sched["id"])
        self.assertEqual(listed[0]["next_run"], sched["next_run"])
        out = jobs_mod.delete_action_schedule(sched["id"])
        self.assertEqual(out, {"ok": True})
        self.assertEqual(
            jobs_mod.list_action_schedules()["schedules"], [])
        with self.assertRaises(HTTPException) as ctx:
            jobs_mod.delete_action_schedule(sched["id"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_create_rejects_bad_cadence(self):
        from fastapi import HTTPException

        for cadence in ("hourly", "off", ""):
            with self.assertRaises(HTTPException, msg=cadence) as ctx:
                self._create(cadence=cadence)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("daily", ctx.exception.detail)

    def test_create_rejects_destructive(self):
        from fastapi import HTTPException

        for action in ("deploy-buttons", "recreate-code",
                       "create-plan", "merge-approved"):
            with self.assertRaises(HTTPException, msg=action) as ctx:
                self._create(action=action)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("destructive", ctx.exception.detail)
            self.assertIn("manually", ctx.exception.detail)

    def test_create_rejects_unknown_action(self):
        from fastapi import HTTPException

        # "recreate" (terminal restart) is rejected as unknown:
        # the console must not offer it while sessions cannot
        # survive it (#444).
        for action in ("nuke", "recreate"):
            with self.assertRaises(HTTPException, msg=action) as ctx:
                self._create(action=action)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail, "unknown action")

    def test_create_run_td_roundtrip(self):
        argv = ["td", "context-warm"]
        with patch.object(jobs_mod, "_repo_target",
                          return_value=("arii/remote-server", "/x")):
            sched = self._create(action="run-td", td_argv=argv)
        self.assertEqual(sched["td_argv"], argv)
        listed = jobs_mod.list_action_schedules()["schedules"]
        self.assertEqual(listed[0]["td_argv"], argv)

    def test_create_run_td_rejects_bad_argv(self):
        from fastapi import HTTPException

        with patch.object(jobs_mod, "_repo_target",
                          return_value=("arii/remote-server", "/x")):
            with self.assertRaises(HTTPException) as ctx:
                self._create(action="run-td", td_argv=[])
            self.assertEqual(ctx.exception.status_code, 400)
            with self.assertRaises(HTTPException) as ctx:
                self._create(action="run-td",
                             td_argv=["td", "rm", "-rf", "/"])
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail, "bad td command")
            with self.assertRaises(HTTPException) as ctx:
                self._create(action="run-td",
                             td_argv=["td", "context-warm x"])
            self.assertEqual(ctx.exception.status_code, 400)

    def test_create_rejects_td_argv_with_plain_action(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self._create(action="logs", td_argv=["td", "doctor"])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("run-td", ctx.exception.detail)

    def test_create_validates_repo_for_repo_scoped(self):
        from fastapi import HTTPException

        with patch.object(jobs_mod, "_repo_target",
                          side_effect=ValueError("unknown repo")):
            with self.assertRaises(HTTPException) as ctx:
                self._create(action="preflight", repo="nope")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail, "unknown repo")

    def test_due_schedule_fires_through_launch(self):
        sched = self._create()
        with jobs_mod.action_schedule_lock:
            state = jobs_mod.load_action_schedules()
            state[sched["id"]]["next_run"] = time.time() - 1
            jobs_mod.save_action_schedules(state)
        launches = []
        with patch.object(
                jobs_mod, "_launch",
                side_effect=lambda rid, a, r, td_argv=None: launches.append(
                    (rid, a, r, td_argv))):
            fired = jobs_mod.fire_due_action_schedules()
        self.assertEqual(fired, 1)
        self.assertEqual(len(launches), 1)
        rid, action, repo, argv = launches[0]
        self.assertEqual((action, repo, argv), ("logs", "remote-server", None))
        with jobs_mod.registry._lock:
            run = jobs_mod.registry.runs.get(rid)
        self.assertIsNotNone(run)
        self.assertEqual(run["action"], "logs")
        jobs_mod.registry.finish_run(rid, 0)
        listed = jobs_mod.list_action_schedules()["schedules"]
        self.assertEqual(len(listed), 1)
        row = listed[0]
        self.assertGreater(row["next_run"], time.time())
        self.assertIsNotNone(row["last_run"])
        self.assertTrue(row["last_result"]["ok"])
        self.assertEqual(row["last_result"]["run_id"], rid)
        # Claimed in advance: a second fire does not double-fire.
        with patch.object(jobs_mod, "_launch") as launcher:
            self.assertEqual(jobs_mod.fire_due_action_schedules(), 0)
        launcher.assert_not_called()

    def test_fire_run_td_replays_argv(self):
        argv = ["td", "context-warm"]
        with patch.object(jobs_mod, "_repo_target",
                          return_value=("arii/remote-server", "/x")):
            sched = self._create(action="run-td", td_argv=argv)
            with jobs_mod.action_schedule_lock:
                state = jobs_mod.load_action_schedules()
                state[sched["id"]]["next_run"] = time.time() - 1
                jobs_mod.save_action_schedules(state)
            launches = []
            with patch.object(
                    jobs_mod, "_launch",
                    side_effect=lambda rid, a, r, td_argv=None:
                    launches.append((rid, a, r, td_argv))):
                self.assertEqual(jobs_mod.fire_due_action_schedules(), 1)
        self.assertEqual(len(launches), 1)
        rid, action, repo, fired_argv = launches[0]
        self.assertEqual(action, "run-td")
        self.assertEqual(fired_argv, argv)
        with jobs_mod.registry._lock:
            run = jobs_mod.registry.runs.get(rid)
        self.assertEqual(run["td_argv"], argv)
        jobs_mod.registry.finish_run(rid, 0)

    def test_fire_records_single_flight_conflict(self):
        sched = self._create()
        with jobs_mod.action_schedule_lock:
            state = jobs_mod.load_action_schedules()
            state[sched["id"]]["next_run"] = time.time() - 1
            jobs_mod.save_action_schedules(state)
        blocker, err = jobs_mod.registry.start_run("logs")
        self.assertIsNone(err)
        try:
            with patch.object(jobs_mod, "_launch") as launcher:
                fired = jobs_mod.fire_due_action_schedules()
            self.assertEqual(fired, 1)
            launcher.assert_not_called()
        finally:
            jobs_mod.registry.finish_run(blocker, 0)
        row = jobs_mod.list_action_schedules()["schedules"][0]
        self.assertFalse(row["last_result"]["ok"])
        self.assertIn("already in progress",
                      row["last_result"]["error"])
        # The claim still advanced: no retry storm next tick.
        self.assertGreater(row["next_run"], time.time())

    def test_fire_launch_failure_releases_single_flight(self):
        sched = self._create()
        with jobs_mod.action_schedule_lock:
            state = jobs_mod.load_action_schedules()
            state[sched["id"]]["next_run"] = time.time() - 1
            jobs_mod.save_action_schedules(state)
        with patch.object(jobs_mod, "_launch",
                          side_effect=RuntimeError("spawn down")):
            self.assertEqual(jobs_mod.fire_due_action_schedules(), 1)
        self.assertIsNone(jobs_mod.registry.active_id)
        row = jobs_mod.list_action_schedules()["schedules"][0]
        self.assertFalse(row["last_result"]["ok"])
        self.assertIn("spawn down", row["last_result"]["error"])

    def test_claim_advances_before_firing(self):
        sched = self._create()
        past = time.time() - 10
        with jobs_mod.action_schedule_lock:
            state = jobs_mod.load_action_schedules()
            state[sched["id"]]["next_run"] = past
            jobs_mod.save_action_schedules(state)
        claimed = jobs_mod._claim_due_action_schedules(time.time())
        self.assertEqual(len(claimed), 1)
        sid, snap = claimed[0]
        self.assertEqual(sid, sched["id"])
        state = jobs_mod.load_action_schedules()
        self.assertGreater(state[sid]["next_run"], time.time())
        self.assertIsNotNone(state[sid]["last_run"])
        # Second claim finds nothing due: no double-fire.
        self.assertEqual(
            jobs_mod._claim_due_action_schedules(time.time()), [])

    def test_fire_never_raises(self):
        sched = self._create()
        with jobs_mod.action_schedule_lock:
            state = jobs_mod.load_action_schedules()
            state[sched["id"]]["next_run"] = time.time() - 1
            jobs_mod.save_action_schedules(state)
        with patch.object(jobs_mod, "_claim_due_action_schedules",
                          side_effect=RuntimeError("store down")):
            self.assertEqual(jobs_mod.fire_due_action_schedules(), 0)
        with patch.object(jobs_mod, "_fire_action_schedule",
                          side_effect=RuntimeError("launch down")):
            self.assertEqual(jobs_mod.fire_due_action_schedules(), 0)


if __name__ == "__main__":
    unittest.main()

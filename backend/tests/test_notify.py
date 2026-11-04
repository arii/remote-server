"""Tests for the notify port (stdlib unittest + fastapi TestClient)."""

import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import notify as notify_router
    from backend import notify as N

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body,
                                hashlib.sha256).hexdigest()


def _make_client():
    app = FastAPI()
    app.include_router(notify_router.router)
    return TestClient(app)


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class HmacTableTest(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("WEBHOOK_SECRET")
        os.environ["WEBHOOK_SECRET"] = "s3cret"
        self.client = _make_client()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("WEBHOOK_SECRET", None)
        else:
            os.environ["WEBHOOK_SECRET"] = self.old

    def _post(self, body, sig=None, event="push"):
        headers = {"X-GitHub-Event": event,
                   "Content-Type": "application/json"}
        if sig is not None:
            headers["X-Hub-Signature-256"] = sig
        return self.client.post("/api/webhook", content=body,
                                headers=headers)

    def test_valid_signature_accepted(self):
        body = json.dumps({"repository": {"full_name": "arii/unknown-repo"},
                           "ref": "refs/heads/main"}).encode()
        r = self._post(body, _sig("s3cret", body))
        self.assertIn(r.status_code, (200, 202))
        self.assertEqual(r.json(), {"ignored": "arii/unknown-repo"})

    def test_forged_signature_rejected(self):
        body = json.dumps({"repository": {"full_name": "arii/x"},
                           "ref": "refs/heads/main"}).encode()
        r = self._post(body, _sig("wrong", body))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"error": "bad signature"})

    def test_missing_signature_rejected(self):
        body = b"{}"
        r = self._post(body, None)
        self.assertEqual(r.status_code, 401)

    def test_malformed_prefix_rejected(self):
        body = b"{}"
        r = self._post(body, "md5=deadbeef")
        self.assertEqual(r.status_code, 401)

    def test_missing_secret_fail_closed(self):
        os.environ.pop("WEBHOOK_SECRET", None)
        body = json.dumps({"repository": {"full_name": "arii/x"}}).encode()
        # Even a "valid" signature against any secret must fail closed.
        r = self._post(body, _sig("s3cret", body))
        self.assertEqual(r.status_code, 503)
        self.assertIn("WEBHOOK_SECRET", r.json()["error"])

    def test_empty_secret_fail_closed(self):
        os.environ["WEBHOOK_SECRET"] = ""
        r = self._post(b"{}", _sig("", b"{}"))
        self.assertEqual(r.status_code, 503)

    def test_bad_json_non_ping(self):
        r = self._post(b"not-json{{{", _sig("s3cret", b"not-json{{{"))
        self.assertEqual(r.status_code, 400)

    def test_ping_pongs(self):
        body = json.dumps(
            {"repository": {"full_name": "arii/nope"}}).encode()
        r = self._post(body, _sig("s3cret", body), event="ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "msg": "pong"})

    def test_non_push_ignored(self):
        body = json.dumps({}).encode()
        r = self._post(body, _sig("s3cret", body), event="issues")
        self.assertEqual(r.json(), {"ignored": "issues"})

    def test_non_main_skipped(self):
        # Signature valid, repo resolution aside: unknown repo hits the
        # allowlist check first; use a direct unit check for ref gating
        # via a monkeypatched resolver.
        orig = notify_router._resolve_repo_path
        notify_router._resolve_repo_path = lambda *_: "/tmp"
        try:
            body = json.dumps(
                {"repository": {"full_name": "arii/x"},
                 "ref": "refs/heads/feature"}).encode()
            r = self._post(body, _sig("s3cret", body))
            self.assertEqual(r.json(), {"skipped": "not main"})
        finally:
            notify_router._resolve_repo_path = orig


@unittest.skipUnless(HAS_CLIENT, "notify router not importable")
class ResolveRepoPathTest(unittest.TestCase):
    def test_stack_checkout_resolves(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            stack = os.path.join(tmp, "remote-server")
            os.mkdir(stack)
            old = os.environ.get("BUTTONS_STACK")
            os.environ["BUTTONS_STACK"] = stack
            try:
                self.assertEqual(
                    notify_router._resolve_repo_path(
                        "arii/remote-server"),
                    stack)
            finally:
                if old is None:
                    del os.environ["BUTTONS_STACK"]
                else:
                    os.environ["BUTTONS_STACK"] = old

    def test_unknown_repo_is_none(self):
        self.assertIsNone(
            notify_router._resolve_repo_path(
                "arii/definitely-not-a-repo-xyz"))


class WebhookWorkerTest(unittest.TestCase):
    def test_crash_releases_pending_and_records_state(self):
        orig_pull = N.webhook_pull
        N.webhook_pull = lambda path: (_ for _ in ()).throw(
            RuntimeError("boom"))
        N.webhook_pending.add("arii/x")
        appended = []
        orig_append = N.ledger.append
        N.ledger.append = appended.append
        try:
            N.webhook_worker("arii/x", "/tmp")
        finally:
            N.webhook_pull = orig_pull
            N.ledger.append = orig_append
        self.assertNotIn("arii/x", N.webhook_pending)
        state = N.webhook_state.get("arii/x", {})
        self.assertFalse(state.get("ok"))
        self.assertIn("boom", state.get("detail", ""))
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["kind"], "webhook")
        self.assertFalse(appended[0]["ok"])

    def test_success_appends_ledger(self):
        orig_pull = N.webhook_pull
        N.webhook_pull = lambda path: (True, "up to date", False)
        appended = []
        orig_append = N.ledger.append
        N.ledger.append = appended.append
        try:
            N.webhook_worker("arii/x", "/tmp")
        finally:
            N.webhook_pull = orig_pull
            N.ledger.append = orig_append
        self.assertEqual(len(appended), 1)
        self.assertTrue(appended[0]["ok"])
        self.assertEqual(appended[0]["repo"], "arii/x")


class WebhookPullSkipTest(unittest.TestCase):
    def _pull(self, script):
        calls = []
        it = iter(script)

        def fake_run(argv, **kw):
            calls.append(argv)
            code, out, err = next(it)
            return SimpleNamespace(returncode=code, stdout=out,
                                   stderr=err)

        orig = subprocess.run
        subprocess.run = fake_run
        try:
            return N.webhook_pull("/tmp"), calls
        finally:
            subprocess.run = orig

    def test_not_ancestor_skips_quietly(self):
        (ok, detail, advanced), calls = self._pull([
            (0, "aaa\n", ""),   # rev-parse HEAD
            (0, "", ""),        # fetch
            (1, "", ""),        # merge-base: not ff-able
        ])
        self.assertTrue(ok)
        self.assertEqual(detail, "skipped: tree not ff-able")
        self.assertFalse(advanced)
        self.assertEqual(len(calls), 3)  # merge never attempted

    def test_dirty_tree_skips_quietly(self):
        (ok, detail, _), calls = self._pull([
            (0, "aaa\n", ""),
            (0, "", ""),
            (0, "", ""),        # merge-base: ff-able
            (0, " M x\n", ""),  # status: dirty
        ])
        self.assertTrue(ok)
        self.assertEqual(detail, "skipped: tree dirty")
        self.assertEqual(len(calls), 4)

    def test_clean_ffable_merges(self):
        (ok, detail, advanced), calls = self._pull([
            (0, "aaa\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "Already up to date.\n", ""),
            (0, "aaa\n", ""),
        ])
        self.assertTrue(ok)
        self.assertEqual(detail, "Already up to date.")
        self.assertFalse(advanced)
        self.assertTrue(any("merge" in c for c in calls))


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class NotificationsEndpointsTest(unittest.TestCase):
    def setUp(self):
        for k in ("NTFY_TOPIC", "NOTIFICATION_WEBHOOK_URL",
                  "DIGEST_INTERVAL_HOURS", "NOTIFY_EVENTS"):
            os.environ.pop(k, None)
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._old_state_path = N.NOTIFIER_STATE_PATH
        N.NOTIFIER_STATE_PATH = os.path.join(self._tmp, "state.json")
        self.client = _make_client()

    def tearDown(self):
        N.NOTIFIER_STATE_PATH = self._old_state_path

    def test_status_defaults(self):
        r = self.client.get("/api/notifications/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ntfy_configured"])
        self.assertFalse(body["webhook_configured"])
        self.assertEqual(body["digest_interval_hours"], 12.0)
        self.assertEqual(body["enabled_events"],
                         ["ci_fail", "jules", "conflict"])
        self.assertIn("last_digest", body)

    def test_status_misconfig_surfaces(self):
        os.environ["DIGEST_INTERVAL_HOURS"] = "bogus"
        r = self.client.get("/api/notifications/status")
        self.assertEqual(r.json()["digest_interval_hours"], 12.0)
        os.environ["NTFY_TOPIC"] = "my-topic"
        r = self.client.get("/api/notifications/status")
        self.assertTrue(r.json()["ntfy_configured"])
        self.assertEqual(r.json()["topic"], "my-topic")
        os.environ["NOTIFY_EVENTS"] = "ci_fail, conflict"
        r = self.client.get("/api/notifications/status")
        self.assertEqual(r.json()["enabled_events"],
                         ["ci_fail", "conflict"])

    def test_test_endpoint_unconfigured(self):
        r = self.client.post("/api/notifications/test")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "results": {}})

    def test_trigger_digest_records_last_sent(self):
        before = N.load_notifier_state().get("last_digest", 0)
        r = self.client.post("/api/notifications/trigger-digest")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("digest", body)
        # The BriefingView run-now control (#615) derives its result
        # line from per-channel results, never from ok.
        self.assertIn("results", body)
        self.assertIsInstance(body["results"], dict)
        after = N.load_notifier_state().get("last_digest", 0)
        self.assertGreaterEqual(after, before)
        self.assertGreater(after, 0)
        # Display path: status reflects the recorded last_digest.
        r2 = self.client.get("/api/notifications/status")
        self.assertEqual(r2.json()["last_digest"], after)


class DigestLogicTest(unittest.TestCase):
    def test_clear_message(self):
        msg = N.build_digest_text(summaries=[], total_repos=3)
        self.assertIn("3 repos clear", msg)

    def test_summary_format(self):
        msg = N.build_digest_text(
            summaries=[("arii/a", 2, 1, 0)], total_repos=2)
        self.assertIn("arii/a: 2 PRs, 1 failing CI, 0 conflicts", msg)
        self.assertIn("Summary across 2 repos", msg)

    def test_digest_interval_validation(self):
        os.environ["DIGEST_INTERVAL_HOURS"] = "not-a-number"
        try:
            self.assertEqual(N.digest_interval_hours(), 12.0)
        finally:
            os.environ.pop("DIGEST_INTERVAL_HOURS", None)

    def test_ci_conflict_detection_no_jules(self):
        sent = []

        def fetcher():
            return {"arii/a": {
                "failing_runs": [{"name": "ci", "branch": "main",
                                  "failed": "boom"}],
                "prs": [{"number": 7, "title": "fix",
                         "conflicted": True}],
            }}

        def sender(**kw):
            sent.append(kw)
            return {}

        import tempfile
        tmp = tempfile.mkdtemp()
        old_path = N.NOTIFIER_STATE_PATH
        N.NOTIFIER_STATE_PATH = os.path.join(tmp, "state.json")
        os.environ["NOTIFY_EVENTS"] = "ci_fail,conflict"
        try:
            state = N.check_notifications_and_digest(
                fetcher=fetcher, sender=sender, now=1000.0)
            kinds = sorted(s["event_type"] for s in sent)
            self.assertEqual(kinds, ["ci_fail", "conflict"])
            self.assertNotIn("jules", kinds)
            # Second run: no new transitions -> only digest logic may fire;
            # digest scheduled far future so nothing new.
            os.environ["DIGEST_INTERVAL_HOURS"] = "100000"
            sent.clear()
            N.check_notifications_and_digest(
                fetcher=fetcher, sender=sender, now=1001.0)
            self.assertEqual(sent, [])
        finally:
            os.environ.pop("NOTIFY_EVENTS", None)
            os.environ.pop("DIGEST_INTERVAL_HOURS", None)
            N.NOTIFIER_STATE_PATH = old_path


class DigestScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "digest_schedule.json")
        os.environ["BUTTONS_DIGEST_SCHEDULE_PATH"] = self.path

    def tearDown(self):
        os.environ.pop("BUTTONS_DIGEST_SCHEDULE_PATH", None)

    def test_set_daily_weekly_off(self):
        import time

        before = time.time()
        daily = N.set_digest_cadence("daily", now=before)
        self.assertEqual(daily["cadence"], "daily")
        self.assertGreaterEqual(daily["next_run"], before + 86300)
        self.assertLessEqual(daily["next_run"], before + 86500)
        weekly = N.set_digest_cadence("weekly", now=before)
        self.assertGreaterEqual(
            weekly["next_run"], before + 7 * 86400 - 100)
        off = N.set_digest_cadence("off", now=before)
        self.assertEqual(
            (off["cadence"], off["next_run"]), ("off", None))

    def test_rejects_bad_cadence(self):
        with self.assertRaises(ValueError):
            N.set_digest_cadence("hourly")

    def test_due_fires_and_stamps_success(self):
        N.set_digest_cadence("daily", now=1000.0)
        with mock.patch.object(
                N, "send_notification",
                return_value={"ntfy": {"ok": True, "status": 200}}
        ) as sender:
            fired = N.fire_due_digest(now=1000.0 + 86401)
        self.assertTrue(fired)
        sender.assert_called_once()
        title = sender.call_args[0][0]
        self.assertIn("Scheduled", title)
        state = N.digest_schedule_status()
        self.assertGreater(state["next_run"], 1000.0 + 86401)
        self.assertEqual(state["last_sent"], 1000.0 + 86401)
        self.assertTrue(state["last_result"]["ok"])

    def test_not_due_and_off_skip_send(self):
        N.set_digest_cadence("daily", now=1000.0)
        with mock.patch.object(
                N, "send_notification",
                return_value={"ntfy": {"ok": True}}) as sender:
            self.assertFalse(N.fire_due_digest(now=1000.0 + 100))
            self.assertEqual(sender.call_count, 0)
        N.set_digest_cadence("off", now=1000.0)
        with mock.patch.object(
                N, "send_notification",
                return_value={"ntfy": {"ok": True}}) as sender:
            self.assertFalse(N.fire_due_digest(now=1000.0 + 10**7))
            self.assertEqual(sender.call_count, 0)

    def test_failed_send_stamps_failure_not_sent(self):
        N.set_digest_cadence("daily", now=1000.0)
        with mock.patch.object(
                N, "send_notification",
                return_value={"ntfy": {"ok": False,
                                      "error": "boom"}}):
            fired = N.fire_due_digest(now=1000.0 + 86401)
        self.assertTrue(fired)
        state = N.digest_schedule_status()
        self.assertIsNone(state["last_sent"])
        self.assertFalse(state["last_result"]["ok"])
        self.assertIn("boom", state["last_result"]["error"])
        # Claimed run still advances: no retry spam.
        self.assertGreater(state["next_run"], 1000.0 + 86401)

    def test_unconfigured_channels_stamp_failure(self):
        N.set_digest_cadence("daily", now=1000.0)
        with mock.patch.object(N, "send_notification",
                               return_value={}):
            self.assertTrue(N.fire_due_digest(now=1000.0 + 86401))
        state = N.digest_schedule_status()
        self.assertIsNone(state["last_sent"])
        self.assertFalse(state["last_result"]["ok"])

    def test_fire_never_raises(self):
        N.set_digest_cadence("daily", now=1000.0)
        with mock.patch.object(
                N, "send_notification",
                side_effect=RuntimeError("down")):
            self.assertTrue(N.fire_due_digest(now=1000.0 + 86401))
        state = N.digest_schedule_status()
        self.assertIsNone(state["last_sent"])
        self.assertFalse(state["last_result"]["ok"])


@unittest.skipUnless(HAS_CLIENT, "notify router not importable")
class DigestScheduleEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "digest_schedule.json")
        os.environ["BUTTONS_DIGEST_SCHEDULE_PATH"] = self.path
        self.client = _make_client()

    def tearDown(self):
        os.environ.pop("BUTTONS_DIGEST_SCHEDULE_PATH", None)

    def test_get_default_off_shape(self):
        r = self.client.get("/api/notifications/digest-schedule")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(
            (body["cadence"], body["next_run"], body["last_sent"]),
            ("off", None, None))
        self.assertEqual(body["last_result"], {})

    def test_post_sets_and_get_reflects(self):
        r = self.client.post(
            "/api/notifications/digest-schedule",
            json={"cadence": "weekly"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cadence"], "weekly")
        self.assertIsNotNone(r.json()["next_run"])
        r2 = self.client.get("/api/notifications/digest-schedule")
        self.assertEqual(r2.json()["cadence"], "weekly")

    def test_post_rejects_bad_cadence(self):
        r = self.client.post(
            "/api/notifications/digest-schedule",
            json={"cadence": "hourly"})
        self.assertEqual(r.status_code, 400)


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class NotificationPrefsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        import time as time_mod

        self.time = time_mod
        self.tmp = tempfile.mkdtemp()
        self._old_state = N.NOTIFIER_STATE_PATH
        N.NOTIFIER_STATE_PATH = os.path.join(self.tmp, "state.json")
        self._old_sched = os.environ.get("BUTTONS_DIGEST_SCHEDULE_PATH")
        os.environ["BUTTONS_DIGEST_SCHEDULE_PATH"] = os.path.join(
            self.tmp, "sched.json")
        self.client = _make_client()

    def tearDown(self):
        N.NOTIFIER_STATE_PATH = self._old_state
        if self._old_sched is None:
            os.environ.pop("BUTTONS_DIGEST_SCHEDULE_PATH", None)
        else:
            os.environ["BUTTONS_DIGEST_SCHEDULE_PATH"] = self._old_sched

    def _prefs(self):
        r = self.client.get("/api/notifications/preferences")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _set_prefs(self, body):
        return self.client.post("/api/notifications/preferences",
                               json=body)

    def test_defaults(self):
        body = self._prefs()
        self.assertEqual(body["muted_events"], [])
        self.assertIsNone(body["quiet_hours"])
        self.assertEqual(body["events"], ["ci_fail", "conflict", "digest"])
        self.assertIsNone(body["held_until"])

    def test_mute_suppresses_and_restores(self):
        r = self._set_prefs({"muted_events": ["ci_fail"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["muted_events"], ["ci_fail"])
        res = N.send_notification("t", "m", event_type="ci_fail")
        self.assertEqual(res, {"suppressed": "event 'ci_fail' muted"})
        # Other classes unaffected.
        self.assertNotIn("suppressed", N.send_notification(
            "t", "m", event_type="conflict"))
        r = self._set_prefs({"muted_events": []})
        self.assertEqual(r.json()["muted_events"], [])
        self.assertNotIn("suppressed", N.send_notification(
            "t", "m", event_type="ci_fail"))

    def test_unknown_event_rejected(self):
        r = self._set_prefs({"muted_events": ["bogus"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("bogus", r.json()["detail"])
        self.assertEqual(self._prefs()["muted_events"], [])

    def test_unknown_key_rejected(self):
        r = self._set_prefs({"nope": 1})
        self.assertEqual(r.status_code, 422)

    def test_quiet_round_trip_and_clear(self):
        r = self._set_prefs(
            {"quiet_hours": {"start": "22:00", "end": "07:00"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["quiet_hours"],
                         {"start": "22:00", "end": "07:00"})
        r = self._set_prefs({"quiet_hours": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["quiet_hours"])

    def test_quiet_validation(self):
        for bad in ({"start": "22:00", "end": "22:00"},
                    {"start": "9pm", "end": "07:00"},
                    {"start": "24:00", "end": "07:00"}):
            r = self._set_prefs({"quiet_hours": bad})
            self.assertEqual(r.status_code, 400, bad)
        # Wrong shape entirely: pydantic rejects before validation.
        r = self._set_prefs({"quiet_hours": "22:00-07:00"})
        self.assertEqual(r.status_code, 422)
        self.assertIsNone(self._prefs()["quiet_hours"])

    def _quiet_covering_now(self):
        now = self.time.time()
        fmt = lambda ts: self.time.strftime("%H:%M",  # noqa: E731
                                            self.time.localtime(ts))
        return {"start": fmt(now - 3600), "end": fmt(now + 3600)}

    def test_quiet_gate_and_test_bypass(self):
        self._set_prefs({"quiet_hours": self._quiet_covering_now()})
        res = N.send_notification("t", "m", event_type="ci_fail")
        self.assertIn("suppressed", res)
        self.assertIn("quiet hours", res["suppressed"])
        # Explicit taps bypass quiet...
        res = N.send_notification("t", "m", event_type="test",
                                  bypass_quiet=True)
        self.assertNotIn("suppressed", res)
        r = self.client.post("/api/notifications/test")
        self.assertNotIn("suppressed", r.json()["results"])
        # ...but mute wins over tap.
        self._set_prefs({"muted_events": ["digest"]})
        res = N.send_notification("t", "m", event_type="digest",
                                  bypass_quiet=True)
        self.assertIn("suppressed", res)

    def test_overnight_edges(self):
        import datetime

        day = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        at = lambda h, m: (  # noqa: E731
            day + datetime.timedelta(hours=h, minutes=m)).timestamp()
        q = {"start": "22:00", "end": "07:00"}
        for h, m, want in ((21, 59, False), (22, 0, True),
                           (0, 0, True), (6, 59, True),
                           (7, 0, False), (12, 0, False)):
            self.assertEqual(N.in_quiet_hours(at(h, m), q), want,
                             (h, m))

    def test_digest_hold_and_single_fire(self):
        sched_path = os.environ["BUTTONS_DIGEST_SCHEDULE_PATH"]
        now = self.time.time()
        with open(sched_path, "w") as f:
            json.dump({"cadence": "daily", "next_run": now - 10,
                       "last_sent": None, "last_result": {},
                       "held_until": None}, f)
        self._set_prefs({"quiet_hours": self._quiet_covering_now()})
        calls = []
        orig = N.send_notification
        N.send_notification = lambda *a, **k: calls.append(k) or {
            "ntfy": {"ok": True}}
        try:
            self.assertFalse(N.fire_due_digest(now))
            self.assertEqual(calls, [])
            held = N.digest_schedule_status()["held_until"]
            self.assertGreater(held, now)
            self.assertEqual(self._prefs()["held_until"], held)
            # Past the window: exactly one fire, hold cleared.
            self._set_prefs({"quiet_hours": None})
            self.assertTrue(N.fire_due_digest(now + 5))
            self.assertEqual(len(calls), 1)
            self.assertIsNone(
                N.digest_schedule_status()["held_until"])
            self.assertIsNone(self._prefs()["held_until"])
        finally:
            N.send_notification = orig

    def test_trigger_skips_stamp_when_muted(self):
        self._set_prefs({"muted_events": ["digest"]})
        r = self.client.post("/api/notifications/trigger-digest")
        self.assertEqual(r.status_code, 200)
        self.assertIn("suppressed", r.json()["results"])
        self.assertEqual(N.load_notifier_state().get("last_digest", 0), 0)
        self._set_prefs({"muted_events": []})
        r = self.client.post("/api/notifications/trigger-digest")
        self.assertNotIn("suppressed", r.json()["results"])
        self.assertGreater(
            N.load_notifier_state().get("last_digest", 0), 0)

    def test_legacy_digest_holds_in_quiet(self):
        sent = []

        def sender(**kw):
            sent.append(kw)
            return {}

        self._set_prefs({"quiet_hours": self._quiet_covering_now()})
        state = N.check_notifications_and_digest(
            fetcher=lambda: {}, sender=sender, now=self.time.time())
        self.assertEqual(sent, [])
        self.assertEqual(state.get("last_digest", 0), 0)
        self._set_prefs({"quiet_hours": None})
        state = N.check_notifications_and_digest(
            fetcher=lambda: {}, sender=sender, now=self.time.time())
        self.assertEqual(len(sent), 1)
        self.assertGreater(state.get("last_digest", 0), 0)

    def test_suppressed_failure_text(self):
        self.assertEqual(
            N._digest_failure_text({"suppressed": "quiet hours"}, ""),
            "quiet hours")


class BudgetWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_state = N.NOTIFIER_STATE_PATH
        N.NOTIFIER_STATE_PATH = os.path.join(self.tmp, "state.json")
        self.sent = []
        self.spend = (None, 0.0)
        cap_spend = mock.patch.object(
            N, "_budget_cap_spend", side_effect=lambda: self.spend)
        cap_spend.start()
        self.addCleanup(cap_spend.stop)

    def tearDown(self):
        N.NOTIFIER_STATE_PATH = self._old_state

    def _sender(self, result=None):
        def send(title, message, **kw):
            self.sent.append(
                {"title": title, "message": message, **kw})
            if result is None:
                return {"ntfy": {"ok": True, "status": 200}}
            return result() if callable(result) else result
        return send

    def _alert(self):
        return N.load_notifier_state().get("last_budget_alert")

    def test_band_boundaries(self):
        self.assertIsNone(N._budget_band(None, 999.0))
        self.assertIsNone(N._budget_band(10.0, 7.999))
        self.assertEqual(N._budget_band(10.0, 8.0), "warning")
        self.assertEqual(N._budget_band(10.0, 9.999), "warning")
        self.assertEqual(N._budget_band(10.0, 10.0), "blocked")
        self.assertEqual(N._budget_band(10.0, 12.0), "blocked")
        self.assertEqual(N._budget_band(0.0, 0.0), "blocked")

    def test_warn_crossing_fires_once(self):
        self.spend = (10.0, 8.0)
        self.assertTrue(N.fire_budget_watch(
            now=1000.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 1)
        push = self.sent[0]
        self.assertIn("80%", push["title"])
        self.assertIn("$8.00", push["message"])
        self.assertIn("$10.00", push["message"])
        self.assertIn("$2.00", push["message"])
        self.assertEqual(push["priority"], "default")
        self.assertEqual(push["tags"], ["warning", "budget"])
        self.assertEqual(push["event_type"], "budget")
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1000.0})

    def test_blocked_crossing_fires_once(self):
        self.spend = (10.0, 10.0)
        self.assertTrue(N.fire_budget_watch(
            now=1000.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 1)
        push = self.sent[0]
        self.assertIn("blocked", push["title"])
        self.assertIn("next metered call will refuse",
                      push["message"])
        self.assertEqual(push["priority"], "high")
        self.assertEqual(self._alert(),
                         {"band": "blocked", "at": 1000.0})

    def test_warn_then_blocked_fires_both(self):
        self.spend = (10.0, 8.0)
        N.fire_budget_watch(now=1000.0, sender=self._sender())
        self.spend = (10.0, 10.0)
        N.fire_budget_watch(now=1060.0, sender=self._sender())
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self._alert(),
                         {"band": "blocked", "at": 1060.0})

    def test_no_repeat_while_in_band(self):
        self.spend = (10.0, 8.5)
        self.assertTrue(N.fire_budget_watch(
            now=1000.0, sender=self._sender()))
        for tick in (1060.0, 1120.0):
            self.assertFalse(N.fire_budget_watch(
                now=tick, sender=self._sender()))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1000.0})

    def test_rearm_on_cap_raise_then_recross(self):
        self.spend = (10.0, 8.0)
        N.fire_budget_watch(now=1000.0, sender=self._sender())
        # Cap raised: spend drops below every threshold, silent
        # re-arm (stamp cleared).
        self.spend = (20.0, 8.0)
        self.assertFalse(N.fire_budget_watch(
            now=1060.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self._alert())
        # Re-crossing fires again.
        self.spend = (20.0, 16.0)
        self.assertTrue(N.fire_budget_watch(
            now=1120.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1120.0})

    def test_downward_move_silently_rearms(self):
        self.spend = (10.0, 10.0)
        N.fire_budget_watch(now=1000.0, sender=self._sender())
        # Cap raised into the warning band: no push (drops never
        # push), stored band steps down so a re-crossing fires.
        self.spend = (12.0, 10.0)
        self.assertFalse(N.fire_budget_watch(
            now=1060.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1060.0})
        self.spend = (12.0, 12.0)
        self.assertTrue(N.fire_budget_watch(
            now=1120.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self._alert(),
                         {"band": "blocked", "at": 1120.0})

    def test_uncapped_silence(self):
        self.spend = (None, 999.0)
        self.assertFalse(N.fire_budget_watch(
            now=1000.0, sender=self._sender()))
        self.assertEqual(self.sent, [])
        self.assertIsNone(self._alert())

    def test_below_threshold_silence(self):
        self.spend = (10.0, 1.0)
        self.assertFalse(N.fire_budget_watch(
            now=1000.0, sender=self._sender()))
        self.assertEqual(self.sent, [])
        self.assertIsNone(self._alert())

    def test_sender_exception_never_dies_and_retries(self):
        self.spend = (10.0, 8.0)

        def boom(*a, **k):
            raise RuntimeError("ntfy down")

        self.assertTrue(N.fire_budget_watch(now=1000.0, sender=boom))
        self.assertIsNone(self._alert())
        # Next tick retries and stamps on success.
        self.assertTrue(N.fire_budget_watch(
            now=1060.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1060.0})

    def test_fire_never_raises_on_broken_state(self):
        self.spend = (10.0, 8.0)
        with mock.patch.object(
                N, "load_notifier_state",
                side_effect=RuntimeError("disk gone")):
            self.assertFalse(N.fire_budget_watch(
                now=1000.0, sender=self._sender()))
        self.assertEqual(self.sent, [])

    def test_suppressed_send_never_stamps_then_fires(self):
        self.spend = (10.0, 8.0)
        self.assertTrue(N.fire_budget_watch(
            now=1000.0,
            sender=self._sender({"suppressed": "quiet hours"})))
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self._alert())
        # Past quiet hours the same band fires (held, not dropped).
        self.assertTrue(N.fire_budget_watch(
            now=1060.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1060.0})

    def test_failed_send_retries_next_tick(self):
        self.spend = (10.0, 8.0)
        self.assertTrue(N.fire_budget_watch(
            now=1000.0,
            sender=self._sender({"ntfy": {"ok": False,
                                          "error": "boom"}})))
        self.assertIsNone(self._alert())
        self.assertTrue(N.fire_budget_watch(
            now=1060.0, sender=self._sender()))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1060.0})

    def test_partial_channel_ok_stamps(self):
        self.spend = (10.0, 8.0)
        self.assertTrue(N.fire_budget_watch(
            now=1000.0,
            sender=self._sender({"ntfy": {"ok": True, "status": 200},
                                 "webhook": {"ok": False,
                                             "error": "down"}})))
        self.assertEqual(self._alert(),
                         {"band": "warning", "at": 1000.0})

    @unittest.skipUnless(HAS_CLIENT, "notify router not importable")
    def test_status_shows_budget_alert(self):
        client = _make_client()
        body = client.get("/api/notifications/status").json()
        self.assertIsNone(body["budget_alert"])
        self.spend = (10.0, 8.0)
        N.fire_budget_watch(now=1000.0, sender=self._sender())
        body = client.get("/api/notifications/status").json()
        self.assertEqual(body["budget_alert"],
                         {"band": "warning", "at": 1000.0})


if __name__ == "__main__":
    unittest.main()

"""Tests for the notify port (stdlib unittest + fastapi TestClient)."""

import hashlib
import hmac
import json
import os
import subprocess
import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()

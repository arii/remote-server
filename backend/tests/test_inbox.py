"""Tests for the aggregated inbox feed (stdlib unittest)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.routers import inbox


def _pr(number, **kw):
    row = {"number": number, "title": f"PR {number}",
           "isDraft": False, "reviewDecision": "APPROVED",
           "ciStatus": "passing", "mergeable": "MERGEABLE"}
    row.update(kw)
    return row


def _item(iid="a1"):
    return {"id": iid, "kind": "failing-run", "title": "t",
            "detail": "d", "repo": "remote-server", "severity": "crit",
            "action": {"label": "l", "method": "GET", "href": "h",
                       "destructive": False, "intent": ""},
            "created_at": 1.0}


def _run(rid, action="merge-approved", done=False, exit_code=None,
         started=1.0, seq=0):
    return {"id": rid, "action": action, "done": done,
            "exit_code": exit_code, "started_at": started, "seq": seq}


class MergeBlockReasonTest(unittest.TestCase):
    def test_ready(self):
        self.assertIsNone(inbox.merge_block_reason(_pr(1)))

    def test_draft(self):
        self.assertEqual(
            inbox.merge_block_reason(_pr(1, isDraft=True)), "is a draft")

    def test_unapproved(self):
        reason = inbox.merge_block_reason(
            _pr(1, reviewDecision="CHANGES_REQUESTED"))
        self.assertIn("CHANGES_REQUESTED", reason)
        self.assertIn("unapproved",
                      inbox.merge_block_reason(_pr(1, reviewDecision="")))

    def test_ci(self):
        self.assertIn("failing",
                      inbox.merge_block_reason(_pr(1, ciStatus="failing")))

    def test_mergeable(self):
        self.assertIn("CONFLICTED", inbox.merge_block_reason(
            _pr(1, mergeable="CONFLICTED")))


class MergeReadinessFixtureTest(unittest.TestCase):
    """Cross-language pin (#553): the shared fixture table that
    frontend/src/App.test.tsx asserts against with exact equality.
    Any semantic change must update the fixture and BOTH predicates."""

    @classmethod
    def setUpClass(cls):
        fixture = (Path(__file__).resolve().parents[2]
                   / "frontend" / "src" / "mergeReadiness.fixtures.json")
        with open(fixture, encoding="utf-8") as fh:
            cls.cases = json.load(fh)["cases"]

    def test_fixture_cases(self):
        self.assertGreater(len(self.cases), 0)
        for case in self.cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(inbox.merge_block_reason(case["row"]),
                                 case["expected"])


class InboxShapeTest(unittest.TestCase):
    def setUp(self):
        inbox._INBOX_CACHE.clear()

    def tearDown(self):
        inbox._INBOX_CACHE.clear()

    def _patch(self, **kw):
        orig = {}
        for k, v in kw.items():
            orig[k] = getattr(inbox, k)
            setattr(inbox, k, v)
        return orig

    def _restore(self, orig):
        for k, v in orig.items():
            setattr(inbox, k, v)

    def test_shape(self):
        prs = [_pr(1), _pr(2), _pr(3), _pr(4),
               _pr(5, isDraft=True), _pr(6, ciStatus="failing")]
        runs = ([_run("active1", done=False, started=10.0, seq=9)]
                + [_run(f"r{i}", done=True, exit_code=0,
                        started=float(i), seq=i)
                   for i in range(6)])
        orig = self._patch(
            list_attention_items=lambda repo: [_item()],
            list_open_prs=lambda repo: prs,
            list_runs=lambda: runs,
        )
        try:
            body = inbox.inbox("remote-server")
        finally:
            self._restore(orig)
        self.assertEqual(body["repo"], "remote-server")
        self.assertIn("updated_at", body)
        self.assertEqual(body["attention"], [_item()])
        self.assertEqual(body["merge_ready"]["count"], 4)
        top = body["merge_ready"]["top"]
        self.assertEqual([t["number"] for t in top], [1, 2, 3])
        self.assertEqual(top[0]["title"], "PR 1")
        self.assertEqual(body["runs"]["active"],
                         {"id": "active1", "action": "merge-approved",
                          "started": 10.0})
        recent = body["runs"]["recent"]
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent[0],
                         {"id": "r0", "action": "merge-approved",
                          "exit": 0})

    def test_all_clear(self):
        orig = self._patch(
            list_attention_items=lambda repo: [],
            list_open_prs=lambda repo: [],
            list_runs=lambda: [],
        )
        try:
            body = inbox.inbox("remote-server")
        finally:
            self._restore(orig)
        self.assertEqual(body["attention"], [])
        self.assertEqual(body["merge_ready"], {"count": 0, "top": []})
        self.assertEqual(
            body["runs"], {"active": None, "recent": []})

    def test_bad_repo(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            inbox.inbox("a/b/c")
        self.assertEqual(ctx.exception.status_code, 400)


class RollupTest(unittest.TestCase):
    def setUp(self):
        inbox._INBOX_CACHE.clear()

    def tearDown(self):
        inbox._INBOX_CACHE.clear()

    def test_all_aggregates_per_repo_items_deduped(self):
        def items(repo):
            shared = dict(_item("job-shared"), repo="other")
            mine = dict(_item(f"run-{repo}"), repo=repo,
                        title=f"wf {repo}")
            return [mine, shared]

        orig = {k: getattr(inbox, k) for k in
                ("list_repo_names", "list_attention_items",
                 "list_open_prs", "list_runs")}
        inbox.list_repo_names = lambda: ["remote-server", "other"]
        inbox.list_attention_items = items
        inbox.list_open_prs = lambda repo: (_ for _ in ()).throw(
            AssertionError("rollup must not read PRs"))
        inbox.list_runs = lambda: []
        try:
            body = inbox.inbox("all")
        finally:
            for k, v in orig.items():
                setattr(inbox, k, v)
        self.assertEqual(body["repo"], "all")
        got = [(i["id"], i["repo"]) for i in body["attention"]]
        self.assertEqual(got, [("run-remote-server", "remote-server"),
                               ("job-shared", "other"),
                               ("run-other", "other")])
        self.assertEqual(body["merge_ready"], {"count": 0, "top": []})

    def test_all_empty(self):
        orig = {k: getattr(inbox, k) for k in
                ("list_repo_names", "list_attention_items",
                 "list_runs")}
        inbox.list_repo_names = lambda: []
        inbox.list_attention_items = lambda repo: []
        inbox.list_runs = lambda: []
        try:
            body = inbox.inbox("all")
        finally:
            for k, v in orig.items():
                setattr(inbox, k, v)
        self.assertEqual(body["attention"], [])
        self.assertEqual(
            body["runs"], {"active": None, "recent": []})


class InboxCacheTest(unittest.TestCase):
    """SWR cache for the inbox feed (#888): TTL hits, stale
    revalidate, mutation invalidation, per-repo isolation."""

    def setUp(self):
        inbox._INBOX_CACHE.clear()
        self._orig = {k: getattr(inbox, k) for k in
                      ("list_repo_names", "list_attention_items",
                       "list_open_prs", "list_runs")}
        self.calls = []

        def items(repo):
            self.calls.append(("attention", repo))
            return [dict(_item(f"run-{repo}"), repo=repo,
                         title=f"wf {repo}")]

        def prs(repo):
            self.calls.append(("prs", repo))
            return [_pr(1)]

        inbox.list_attention_items = items
        inbox.list_open_prs = prs
        inbox.list_runs = lambda: []
        inbox.list_repo_names = lambda: ["remote-server", "boomtick"]

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(inbox, k, v)
        inbox._INBOX_CACHE.clear()

    def _seam_calls(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def _age_entry(self, key, age):
        _, data = inbox._INBOX_CACHE._entries[key]
        aged = time.time() - age
        inbox._INBOX_CACHE._entries[key] = (aged, data)
        return aged

    def test_second_read_serves_cached_without_seams(self):
        first = inbox.inbox("remote-server")
        self.assertFalse(first["cached"])
        self.assertFalse(first["stale"])
        self.assertGreater(first["generated_at"], 0)
        # The stored entry itself carries no markers (copies do).
        self.assertNotIn("cached",
                         inbox._INBOX_CACHE.peek("remote-server"))
        n = len(self.calls)
        start = time.monotonic()
        second = inbox.inbox("remote-server")
        elapsed = time.monotonic() - start
        self.assertTrue(second["cached"])
        self.assertFalse(second["stale"])
        self.assertEqual(second["generated_at"],
                         first["generated_at"])
        self.assertEqual(len(self.calls), n)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(second["attention"], first["attention"])
        # Markers are additive: the model accepts the payload.
        inbox.InboxFeed(**second)

    def test_stale_revalidates_and_refreshes_entry(self):
        first = inbox.inbox("remote-server")
        old_ts = self._age_entry("remote-server",
                                 inbox.INBOX_CACHE_TTL + 1)
        with patch("backend.api.cache._spawn_daemon",
                   side_effect=lambda t, a: t(*a)):
            body = inbox.inbox("remote-server")
        self.assertTrue(body["cached"])
        self.assertTrue(body["stale"])
        self.assertEqual(body["generated_at"], old_ts)
        # The inline refresh stored a fresh entry and cleared the
        # in-flight flag.
        ts, _ = inbox._INBOX_CACHE._entries["remote-server"]
        self.assertGreater(ts, old_ts)
        self.assertEqual(inbox._INBOX_CACHE._refreshing, set())
        self.assertEqual(len(self._seam_calls("attention")), 2)
        self.assertEqual(body["attention"], first["attention"])

    def test_past_max_stale_recomputes_synchronously(self):
        inbox.inbox("remote-server")
        self._age_entry("remote-server",
                        inbox.INBOX_CACHE_MAX_STALE + 1)
        inbox.list_attention_items = lambda repo: [
            dict(_item("fresh-1"), repo=repo)]
        body = inbox.inbox("remote-server")
        self.assertFalse(body["cached"])
        self.assertFalse(body["stale"])
        self.assertEqual(
            [i["id"] for i in body["attention"]], ["fresh-1"])

    def test_refresh_failure_keeps_stale(self):
        first = inbox.inbox("remote-server")
        old_ts = self._age_entry("remote-server",
                                 inbox.INBOX_CACHE_TTL + 1)

        def boom(repo):
            raise RuntimeError("gh down")

        inbox.list_attention_items = boom
        with patch("backend.api.cache._spawn_daemon",
                   side_effect=lambda t, a: t(*a)):
            body = inbox.inbox("remote-server")
        self.assertTrue(body["cached"])
        self.assertTrue(body["stale"])
        self.assertEqual(body["attention"], first["attention"])
        ts, _ = inbox._INBOX_CACHE._entries["remote-server"]
        self.assertEqual(ts, old_ts)
        self.assertEqual(inbox._INBOX_CACHE._refreshing, set())

    def test_per_repo_isolation(self):
        a = inbox.inbox("remote-server")
        b = inbox.inbox("boomtick")
        self.assertEqual([i["id"] for i in a["attention"]],
                         ["run-remote-server"])
        self.assertEqual([i["id"] for i in b["attention"]],
                         ["run-boomtick"])
        # A repeat remote-server read serves its own entry — never
        # boomtick rows.
        a2 = inbox.inbox("remote-server")
        self.assertTrue(a2["cached"])
        self.assertEqual([i["id"] for i in a2["attention"]],
                         ["run-remote-server"])

    def test_rollup_composes_cached_per_repo_entries(self):
        inbox.inbox("remote-server")
        inbox.inbox("boomtick")
        n_prs = len(self._seam_calls("prs"))

        def boom(repo):
            raise AssertionError(
                "rollup must compose cached entries, not fan out")

        inbox.list_attention_items = boom
        body = inbox.inbox("all")
        self.assertFalse(body["cached"])
        got = [(i["id"], i["repo"]) for i in body["attention"]]
        self.assertEqual(got, [("run-remote-server", "remote-server"),
                               ("run-boomtick", "boomtick")])
        self.assertEqual(body["merge_ready"], {"count": 0, "top": []})
        # No per-repo build ran (those would pay the PR fan-out).
        self.assertEqual(len(self._seam_calls("prs")), n_prs)

    def test_rollup_caches_under_all_key(self):
        first = inbox.inbox("all")
        self.assertFalse(first["cached"])
        n = len(self._seam_calls("attention"))
        self.assertEqual(n, 2)
        second = inbox.inbox("all")
        self.assertTrue(second["cached"])
        self.assertEqual(len(self._seam_calls("attention")), n)
        self.assertEqual(second["attention"], first["attention"])

    def test_invalidate_helpers(self):
        inbox.inbox("remote-server")
        inbox.inbox("boomtick")
        inbox.inbox("all")
        # One repo drops its entry plus the rollup — never a peer.
        inbox.invalidate_inbox_cache("arii/remote-server")
        self.assertIsNone(inbox._INBOX_CACHE.peek("remote-server"))
        self.assertIsNone(inbox._INBOX_CACHE.peek("all"))
        self.assertIsNotNone(inbox._INBOX_CACHE.peek("boomtick"))
        inbox.invalidate_inbox_cache(None)
        self.assertIsNone(inbox._INBOX_CACHE.peek("boomtick"))
        # Unknown keys and blanks are a silent no-op.
        inbox.invalidate_inbox_cache("nope")
        inbox.invalidate_inbox_cache("  ")

    def test_pr_action_invalidates_inbox(self):
        from backend.api.routers import pulls as pulls_mod

        inbox.inbox("remote-server")
        self.assertIsNotNone(
            inbox._INBOX_CACHE.peek("remote-server"))
        with patch.object(
                pulls_mod, "_pr_action",
                return_value={"ok": True, "output": "merged"}):
            pulls_mod.pr_action(pulls_mod.PrActionRequest(
                repo="remote-server", pr_number=7, action="approve",
                intent="pr-approve", confirmed=True))
        self.assertIsNone(inbox._INBOX_CACHE.peek("remote-server"))
        self.assertFalse(inbox.inbox("remote-server")["cached"])

    def test_defer_and_restore_invalidate_inbox(self):
        from backend.api.routers import deferred as deferred_mod
        from backend.api.routers.deferred import DeferredRequest

        tmp = tempfile.mkdtemp()
        old = os.environ.get("BUTTONS_DEFERRED_PATH")
        os.environ["BUTTONS_DEFERRED_PATH"] = os.path.join(
            tmp, "deferred.json")
        try:
            inbox.inbox("remote-server")
            deferred_mod.defer_row(DeferredRequest(
                repo="remote-server", kind="pr", number=7))
            self.assertIsNone(
                inbox._INBOX_CACHE.peek("remote-server"))
            inbox.inbox("remote-server")
            deferred_mod.restore_row(DeferredRequest(
                repo="remote-server", kind="pr", number=7))
            self.assertIsNone(
                inbox._INBOX_CACHE.peek("remote-server"))
        finally:
            if old is None:
                os.environ.pop("BUTTONS_DEFERRED_PATH", None)
            else:
                os.environ["BUTTONS_DEFERRED_PATH"] = old

    def test_webhook_pull_invalidates_on_success_only(self):
        from backend import notify as N

        repo = "arii/remote-server"
        orig_pull, orig_append = N.webhook_pull, N.ledger.append
        N.ledger.append = lambda row: None
        try:
            inbox.inbox("remote-server")
            inbox.inbox("all")
            N.webhook_pull = lambda path: (
                True, "up to date", False, None)
            N.webhook_worker(repo, "/tmp")
            self.assertIsNone(
                inbox._INBOX_CACHE.peek("remote-server"))
            self.assertIsNone(inbox._INBOX_CACHE.peek("all"))
            # A failed pull leaves the checkout unchanged, so the
            # cache stands.
            inbox.inbox("remote-server")
            N.webhook_pull = lambda path: (
                False, "boom", False, None)
            N.webhook_worker(repo, "/tmp")
            self.assertIsNotNone(
                inbox._INBOX_CACHE.peek("remote-server"))
        finally:
            N.webhook_pull = orig_pull
            N.ledger.append = orig_append
            N.webhook_state.pop(repo, None)
            N.webhook_pending.discard(repo)


if __name__ == "__main__":
    unittest.main()

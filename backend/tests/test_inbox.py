"""Tests for the aggregated inbox feed (stdlib unittest)."""

import unittest

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


class InboxShapeTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

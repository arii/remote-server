"""Tests for the deferred router (tmp-file persistence seam).

Covers add/list/remove, bad-input 400s, and a persistence round-trip
through BUTTONS_DEFERRED_PATH.
"""

import json
import os
import tempfile
import unittest

from fastapi import HTTPException

from backend.api.routers import deferred as deferred_mod
from backend.api.routers.deferred import DeferredRequest


class DeferredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "deferred.json")
        self.old = os.environ.get("BUTTONS_DEFERRED_PATH")
        os.environ["BUTTONS_DEFERRED_PATH"] = self.path

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BUTTONS_DEFERRED_PATH", None)
        else:
            os.environ["BUTTONS_DEFERRED_PATH"] = self.old
        self.tmp.cleanup()

    def test_empty_list(self):
        out = deferred_mod.list_deferred(repo="remote-server")
        self.assertEqual(out, {"deferred": []})

    def test_add_list_remove(self):
        out = deferred_mod.defer_row(DeferredRequest(
            repo="remote-server", kind="pr", number=7))
        self.assertEqual(len(out["deferred"]), 1)
        row = out["deferred"][0]
        self.assertEqual((row["kind"], row["number"]), ("pr", 7))
        self.assertGreater(row["deferred_at"], 0)

        out = deferred_mod.defer_row(DeferredRequest(
            repo="remote-server", kind="issue", number=3))
        self.assertEqual(len(out["deferred"]), 2)

        # Re-deferring the same row is a no-op (no duplicate).
        out = deferred_mod.defer_row(DeferredRequest(
            repo="remote-server", kind="pr", number=7))
        self.assertEqual(len(out["deferred"]), 2)

        listed = deferred_mod.list_deferred(repo="remote-server")
        self.assertEqual(
            {(r["kind"], r["number"]) for r in listed["deferred"]},
            {("pr", 7), ("issue", 3)})

        out = deferred_mod.restore_row(DeferredRequest(
            repo="remote-server", kind="pr", number=7))
        self.assertEqual(
            [(r["kind"], r["number"]) for r in out["deferred"]],
            [("issue", 3)])

        # Restoring a missing row is a no-op, not an error.
        out = deferred_mod.restore_row(DeferredRequest(
            repo="remote-server", kind="pr", number=7))
        self.assertEqual(len(out["deferred"]), 1)

    def test_repos_are_isolated(self):
        deferred_mod.defer_row(DeferredRequest(
            repo="remote-server", kind="pr", number=7))
        out = deferred_mod.list_deferred(repo="other-repo")
        self.assertEqual(out, {"deferred": []})

    def test_bad_input_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            deferred_mod.list_deferred(repo="nope/../evil")
        self.assertEqual(ctx.exception.status_code, 400)
        for bad in ({"repo": "remote-server", "kind": "pr",
                     "number": True},
                    {"repo": "remote-server", "kind": "pr",
                     "number": 0},
                    {"repo": "remote-server", "kind": "banana",
                     "number": 7},
                    {"repo": "nope/../evil", "kind": "pr",
                     "number": 7}):
            for route in (deferred_mod.defer_row,
                          deferred_mod.restore_row):
                with self.assertRaises(HTTPException) as ctx:
                    route(DeferredRequest(**bad))
                self.assertEqual(ctx.exception.status_code, 400)

    def test_persistence_round_trip(self):
        deferred_mod.defer_row(DeferredRequest(
            repo="remote-server", kind="pr", number=7))
        with open(self.path, encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["remote-server"][0]["number"], 7)
        # A fresh read sees the same rows (no in-memory cache).
        listed = deferred_mod.list_deferred(repo="remote-server")
        self.assertEqual(len(listed["deferred"]), 1)

    def test_corrupt_file_reads_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json {{{")
        out = deferred_mod.list_deferred(repo="remote-server")
        self.assertEqual(out, {"deferred": []})


if __name__ == "__main__":
    unittest.main()

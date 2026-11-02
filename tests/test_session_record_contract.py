"""Validate session-record fixtures against docs/session-record-contract.md."""
import json
import os
import unittest

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sessions")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def check_record(r, detail=False):
    assert isinstance(r["file"], str) and r["file"].endswith(".jsonl"), r
    assert "/" not in r["file"], r
    assert isinstance(r["mtime"] if not detail else r.get("mtime", 0), (int, float))
    assert isinstance(r["cwd"], str), r
    assert isinstance(r["repo"], str), r
    if not detail:
        assert isinstance(r["title"], str) and r["title"], r
    src = r.get("source", "pi-file")
    assert src in ("pi-file", "background", "ledger"), r
    if src == "background":
        assert r["id"].startswith("bg-")
        assert r["kind"] in ("ask", "suggest")
        assert isinstance(r["done"], bool)
    elif src == "ledger":
        assert r["id"].startswith("ledger-")
        assert r["kind"] == "ledger"
        assert r["done"] is True


class TestSessionRecordContract(unittest.TestCase):
    def test_list_rows(self):
        data = load("list.json")
        self.assertIn("sessions", data)
        self.assertEqual(len(data["sessions"]), 3)
        for r in data["sessions"]:
            check_record(r)

    def test_detail(self):
        d = load("detail.json")
        self.assertIn("turns", d)
        check_record({"file": d["file"], "mtime": 0, "cwd": d["cwd"],
                      "repo": d["repo"], "title": "t", "id": d["id"],
                      "kind": "ask", "source": "background", "done": True})
        for t in d["turns"]:
            self.assertIn(t["role"], ("user", "assistant"))
            self.assertIsInstance(t["text"], str)

    def test_empty(self):
        self.assertEqual(load("empty.json"), {"sessions": []})

    def test_error(self):
        e = load("error.json")
        self.assertIsInstance(e["error"], str)


if __name__ == "__main__":
    unittest.main()

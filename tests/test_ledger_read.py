import io
import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

from scripts import ledger_read


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class ReadLedgerTests(unittest.TestCase):
    def setUp(self):
        handle, self.token_path = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(os.unlink, self.token_path)
        with open(self.token_path, "w") as token:
            json.dump(
                {
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "refresh_token": "rtoken",
                    "token_uri": "https://oauth.example/token",
                },
                token,
            )
        self.requests = []

    def urlopen(self, replies):
        def fake(request, timeout):
            self.requests.append(request)
            self.assertGreater(timeout, 0)
            return Response(replies.pop(0))

        return mock.patch.object(ledger_read.urllib.request, "urlopen", fake)

    def test_refreshes_then_exports_the_sheet_as_csv(self):
        csv_text = (
            "Timestamp,Agent / Source,Status,Link\n1/2/2026 3:04:05,BoomTick,Act,u\n"
        )
        replies = [json.dumps({"access_token": "atoken"}).encode(), csv_text.encode()]
        with self.urlopen(replies):
            rows = ledger_read.read_ledger("sheet/id", self.token_path)
        self.assertEqual(
            rows,
            [
                {
                    "Timestamp": "1/2/2026 3:04:05",
                    "Agent / Source": "BoomTick",
                    "Status": "Act",
                    "Link": "u",
                }
            ],
        )
        refresh, export = self.requests
        self.assertEqual(refresh.full_url, "https://oauth.example/token")
        self.assertEqual(
            urllib.parse.parse_qs(refresh.data.decode()),
            {
                "client_id": ["cid"],
                "client_secret": ["csecret"],
                "refresh_token": ["rtoken"],
                "grant_type": ["refresh_token"],
            },
        )
        self.assertEqual(
            export.full_url,
            "https://www.googleapis.com/drive/v3/files/sheet%2Fid/export?mimeType=text/csv",
        )
        self.assertEqual(export.get_header("Authorization"), "Bearer atoken")

    def test_refresh_without_access_token_raises(self):
        with self.urlopen([b"{}"]):
            with self.assertRaises(RuntimeError):
                ledger_read.read_ledger("sheet", self.token_path)
        self.assertEqual(len(self.requests), 1)

    def test_fold_latest_per_key(self):
        rows = [
            {"Link": "url1", "Item / Note": "note1"},
            {"Link": "", "Item / Note": "task-123"},
            {"Link": "", "Item / Note": "some note"},
            {"Link": "url1", "Item / Note": "note2"},
        ]
        # Expected:
        # url1 -> note2 (latest)
        # task-123 -> task-123 (extracted)
        # some note -> some note (fallback)
        folded = ledger_read.fold_latest_per_key(rows)

        # folded is a list of rows, need to find the right one
        def get_row_by_key(key):
            for r in folded:
                link = r.get("Link", "").strip()
                if link == key:
                    return r
                note = r.get("Item / Note", "").strip()
                m = __import__("re").search(r"task-[a-zA-Z0-9_-]+", note)
                extracted = m.group(0) if m else note
                if extracted == key:
                    return r
            return None

        self.assertEqual(get_row_by_key("url1")["Item / Note"], "note2")
        self.assertEqual(get_row_by_key("task-123")["Item / Note"], "task-123")
        self.assertEqual(get_row_by_key("some note")["Item / Note"], "some note")


if __name__ == "__main__":
    unittest.main()

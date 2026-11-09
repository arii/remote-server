#!/usr/bin/env python3
"""Tests for the agent-initiated co-browse handoff tool (#1097).

Pure payload shape + injected-post tool tests; no network, no LLM,
no browser needed.
"""

import importlib.util
import json
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    "cobrowse_handoff",
    os.path.join("deep-agent", "cobrowse_handoff.py"),
)
ch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ch)


class HandoffPayloadTest(unittest.TestCase):
    def test_interrupt_shaped_payload(self):
        payload = ch.handoff_payload(
            "thread-1", "login wall", "https://x/login", "https://console/cobrowse"
        )
        self.assertEqual(payload["event"], "browser_handoff_requested")
        self.assertEqual(payload["thread_id"], "thread-1")
        self.assertEqual(payload["reason"], "login wall")
        self.assertEqual(payload["current_url"], "https://x/login")
        self.assertEqual(payload["live_view_url"], "https://console/cobrowse")
        self.assertIn("Hand Back", payload["instructions"])
        json.dumps(payload)  # must stay JSON-serializable

    def test_blank_live_url_falls_back_to_public_env(self):
        prev = os.environ.get("BUTTONS_PUBLIC_URL")
        os.environ["BUTTONS_PUBLIC_URL"] = "https://console.example"
        try:
            self.assertEqual(
                ch.live_view_url_for(""), "https://console.example/buttons/cobrowse"
            )
        finally:
            if prev is None:
                os.environ.pop("BUTTONS_PUBLIC_URL", None)
            else:
                os.environ["BUTTONS_PUBLIC_URL"] = prev

    def test_blank_live_url_without_public_is_empty(self):
        prev = os.environ.pop("BUTTONS_PUBLIC_URL", None)
        try:
            self.assertEqual(ch.live_view_url_for(""), "")
        finally:
            if prev is not None:
                os.environ["BUTTONS_PUBLIC_URL"] = prev


class HandoffToolTest(unittest.TestCase):
    def test_posts_to_backend_and_returns_payload(self):
        calls = []

        def fake_post(api_base, reason, current_url):
            calls.append((api_base, reason, current_url))
            return {"state": "PAUSED_BY_USER"}

        out = ch.request_human_cobrowse_handoff_impl(
            "thread-1", "mfa prompt", "https://x/mfa", post=fake_post
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/api/agent"))
        self.assertEqual(calls[0][1], "mfa prompt")
        payload = json.loads(out)
        self.assertEqual(payload["event"], "browser_handoff_requested")
        self.assertEqual(payload["thread_id"], "thread-1")

    def test_missing_thread_or_reason_rejected(self):
        self.assertTrue(
            ch.request_human_cobrowse_handoff_impl(
                "", "mfa", post=lambda *a: {}
            ).startswith("REJECTED:")
        )
        self.assertTrue(
            ch.request_human_cobrowse_handoff_impl(
                "t", "  ", post=lambda *a: {}
            ).startswith("REJECTED:")
        )

    def test_backend_error_surfaced_not_raised(self):
        def boom(*args):
            raise ConnectionError("down")

        out = ch.request_human_cobrowse_handoff_impl("t", "captcha", post=boom)
        self.assertTrue(out.startswith("handoff unavailable:"))


class BrowserApiBaseTest(unittest.TestCase):
    def test_strips_agent_suffix(self):
        self.assertEqual(
            ch.browser_api_base("http://127.0.0.1:8000/api/agent"),
            "http://127.0.0.1:8000/api/browser",
        )

    def test_bare_base_gets_browser_suffix(self):
        self.assertEqual(
            ch.browser_api_base("http://buttons:8000"), "http://buttons:8000/browser"
        )

    def test_blank_falls_back_to_module_default(self):
        self.assertTrue(ch.browser_api_base("").endswith("/browser"))


class BrowserNotesTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("DEEP_AGENT_STATE_DIR")
        os.environ["DEEP_AGENT_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("DEEP_AGENT_STATE_DIR", None)
        else:
            os.environ["DEEP_AGENT_STATE_DIR"] = self.prev

    def test_empty_store_reports_none(self):
        self.assertEqual(ch.browser_notes_impl(), "no browser notes yet")
        self.assertEqual(
            ch.browser_notes_impl(site="linkedin"), "no notes for 'linkedin'"
        )

    def test_save_then_read_round_trip(self):
        self.assertEqual(
            ch.browser_notes_impl(site="linkedin", note="Login needs a human."),
            "saved note for 'linkedin'",
        )
        body = json.loads(ch.browser_notes_impl(site="linkedin"))
        self.assertEqual(body["linkedin"]["note"], "Login needs a human.")
        self.assertIn("linkedin", ch.browser_notes_impl())

    def test_save_requires_site(self):
        self.assertTrue(ch.browser_notes_impl(note="orphan").startswith("REJECTED:"))


if __name__ == "__main__":
    unittest.main()

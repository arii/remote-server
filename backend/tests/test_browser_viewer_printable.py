"""Printable Option/AltGr text is distinct from command chords."""

import asyncio
import unittest
from unittest import mock

from backend.api.routers import browser_view


class PrintableChordTest(unittest.TestCase):
    def test_option_and_altgr_printable_characters(self):
        async def check():
            for key, code, modifiers in [
                ("å", "KeyA", 1),
                ("@", "KeyQ", 1),
                ("Å", "KeyA", 9),
                ("¡", "Digit1", 1),
                (" ", "Space", 1),
            ]:
                bridge = browser_view.CDPBridge("test")
                bridge.ensure_focus = mock.AsyncMock()
                bridge.call = mock.AsyncMock(return_value={})
                await bridge.key(key, code, modifiers)
                bridge.call.assert_awaited_once_with("Input.insertText", {"text": key})
            for key, modifiers in [
                ("a", 2),
                ("a", 4),
                ("@", 3),
                ("ArrowLeft", 1),
                ("AltGraph", 3),
            ]:
                bridge.call.reset_mock()
                await bridge.key(key, key, modifiers)
                self.assertTrue(
                    all(
                        c.args[0] == "Input.dispatchKeyEvent"
                        for c in bridge.call.call_args_list
                    )
                )

        asyncio.run(check())

    def test_alt_access_keys_keep_key_events_and_modifiers(self):
        async def check():
            for key, code, modifiers, graph in [
                ("b", "KeyB", 1, False),
                ("B", "KeyB", 9, False),
                ("1", "Digit1", 1, False),
                ("/", "Slash", 1, False),
                (" ", "Space", 1, False),
                ("b", "KeyB", 3, True),
                ("@", "KeyQ", 3, False),
                ("å", "KeyA", 5, False),
                ("å", "Unknown", 1, False),
            ]:
                bridge = browser_view.CDPBridge("test")
                bridge.ensure_focus = mock.AsyncMock()
                bridge.call = mock.AsyncMock(return_value={})
                await bridge.key(key, code, modifiers, graph)
                calls = bridge.call.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertTrue(
                    all(c.args[0] == "Input.dispatchKeyEvent" for c in calls)
                )
                self.assertEqual(calls[0].args[1]["type"], "keyDown")
                self.assertEqual(calls[0].args[1]["modifiers"], modifiers)
                self.assertEqual(calls[0].args[1]["text"], "")
                self.assertEqual(calls[0].args[1]["unmodifiedText"], key)

        asyncio.run(check())

    def test_genuine_altgraph_text_and_nonprintable_keys(self):
        async def check():
            bridge = browser_view.CDPBridge("test")
            bridge.ensure_focus = mock.AsyncMock()
            bridge.call = mock.AsyncMock(return_value={})
            await bridge.key("@", "KeyQ", 3, True)
            bridge.call.assert_awaited_once_with("Input.insertText", {"text": "@"})
            for key in ("", "\n", "\x00"):
                self.assertFalse(browser_view._is_text_key(key, "KeyA", 1, False))

        asyncio.run(check())

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

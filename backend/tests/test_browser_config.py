import os
import unittest
from pathlib import Path
from unittest import mock

from backend.shared.cli import repo_root
from backend.browser_config import data_path, grid_url


class BrowserConfigTest(unittest.TestCase):
    def test_data_root_and_relative_overrides(self):
        with mock.patch.dict(
            os.environ, {"BROWSER_DATA_DIR": "data/test-browser"}, clear=True
        ):
            root = Path(repo_root()) / "data/test-browser"
            self.assertEqual(data_path("BROWSER_SKILLS_DIR", "skills"), root / "skills")
            with mock.patch.dict(
                os.environ, {"BROWSER_SKILLS_DIR": "data/other-skills"}
            ):
                self.assertEqual(
                    data_path("BROWSER_SKILLS_DIR", "skills"),
                    Path(repo_root()) / "data/other-skills",
                )

    def test_grid_default_and_invalid_bases(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(grid_url(), "http://127.0.0.1:4444")
        for value in (
            "",
            "ftp://localhost:4444",
            "http://",
            "https://grid/?x=1",
            "https://grid/#x",
        ):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"BROWSER_GRID_URL": value}),
            ):
                with self.assertRaisesRegex(ValueError, "BROWSER_GRID_URL"):
                    grid_url()

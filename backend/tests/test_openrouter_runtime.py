import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ledger
import model_router
import runs


class OpenRouterSkipVisibilityTest(unittest.TestCase):
    def test_missing_keys_are_visible_with_logging_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "ledger.jsonl")
            with (
                mock.patch.object(ledger, "LEDGER", path),
                mock.patch.object(runs, "LEDGER", path),
                mock.patch.object(model_router, "ollama_reachable", return_value=False),
                mock.patch.object(model_router.logger, "disabled", True),
            ):
                for key in (None, "", "  "):
                    env = {"NEBIUS_API_KEY": "synthetic-nebius"}
                    if key is not None:
                        env["OPENROUTER_API_KEY"] = key
                    with self.subTest(key=key):
                        output = io.StringIO()
                        with (
                            mock.patch.dict(os.environ, env, clear=True),
                            contextlib.redirect_stderr(output),
                        ):
                            spec = model_router.resolve_llm()
                        self.assertEqual(spec["provider"], "nebius")
                        self.assertIn("OpenRouter skipped", output.getvalue())

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import coder
from backend.shared import ledger
import observe

try:
    import deepagents  # noqa: F401
    import langchain_core  # noqa: F401

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False


class Stop(Exception):
    pass


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestCoderModelTier(unittest.TestCase):
    def run_tier(self, tier, env):
        overrides = []
        starts = []

        def build(override=None):
            overrides.append(override)
            name = override or "default"
            return object(), {"provider": "twt", "model": name}

        def run_start(_self, script, thread, model):
            starts.append(model)

        clean = {"CODER_MODEL": "", "CODER_STRONG_MODEL": "", **env}
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.dict("os.environ", clean),
                mock.patch.object(coder, "build_model", side_effect=build) as built,
                mock.patch.object(coder, "build_agent", side_effect=Stop),
                mock.patch.object(observe.TraceRecorder, "run_start", run_start),
                mock.patch.object(ledger, "LEDGER", str(Path(root, "l.jsonl"))),
                mock.patch.object(
                    observe,
                    "default_trace_path",
                    return_value=str(Path(root, "t.jsonl")),
                ),
                mock.patch.object(coder, "CODER_DB", str(Path(root, "c.db"))),
            ):
                try:
                    coder.run_coder(root, "brief", "sys", "t", 5, model_tier=tier)
                except Stop:
                    pass
                finally:
                    self.built = built
                return overrides, starts, built

    def test_strong_uses_strong_model_and_marks_label(self):
        env = {"CODER_MODEL": "fast-m", "CODER_STRONG_MODEL": "big-m"}
        overrides, starts, _ = self.run_tier("strong", env)
        self.assertEqual(overrides, ["big-m"])
        self.assertEqual(starts, ["twt:big-m (tier=strong)"])

    def test_fast_and_none_use_coder_model(self):
        env = {"CODER_MODEL": "fast-m", "CODER_STRONG_MODEL": "big-m"}
        for tier in ("fast", None):
            with self.subTest(tier=tier):
                overrides, starts, _ = self.run_tier(tier, env)
                self.assertEqual(overrides, ["fast-m"])
                self.assertEqual(starts, ["twt:fast-m"])

    def test_fast_and_none_default_when_unset(self):
        for tier in ("fast", None):
            with self.subTest(tier=tier):
                overrides, _, _ = self.run_tier(tier, {})
                self.assertEqual(overrides, [None])

    def test_strong_without_config_raises_and_builds_nothing(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "CODER_STRONG_MODEL"):
                    self.run_tier(
                        "strong",
                        {"CODER_MODEL": "fast-m", "CODER_STRONG_MODEL": value},
                    )
                self.built.assert_not_called()

    def test_invalid_tier_raises(self):
        with self.assertRaisesRegex(ValueError, "model_tier"):
            self.run_tier("turbo", {"CODER_STRONG_MODEL": "big-m"})


class TestTierModel(unittest.TestCase):
    def test_tier_model_resolution(self):
        with mock.patch.dict(
            "os.environ", {"CODER_MODEL": "f", "CODER_STRONG_MODEL": "s"}
        ):
            self.assertEqual(coder.tier_model("strong"), "s")
            self.assertEqual(coder.tier_model("fast"), "f")
            self.assertEqual(coder.tier_model(None), "f")
            with self.assertRaises(ValueError):
                coder.tier_model("x")


if __name__ == "__main__":
    unittest.main()

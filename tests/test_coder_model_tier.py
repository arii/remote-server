import os
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

    def test_strong_uses_strong_route_and_marks_label(self):
        env = {"CODER_MODEL": "fast-m"}
        overrides, starts, _ = self.run_tier("strong", env)
        self.assertEqual(overrides, ["route:strong"])
        self.assertEqual(starts, ["twt:route:strong (tier=strong)"])

    def test_strong_ignores_strong_model_env_pin(self):
        # No environment override path (issue #1798): a set
        # CODER_STRONG_MODEL behaves identically to an unset one.
        for env in (
            {"CODER_MODEL": "fast-m"},
            {"CODER_MODEL": "fast-m", "CODER_STRONG_MODEL": "big-m"},
            {"CODER_MODEL": "fast-m", "CODER_STRONG_MODEL": "  "},
        ):
            with self.subTest(env=env):
                overrides, starts, _ = self.run_tier("strong", env)
                self.assertEqual(overrides, ["route:strong"])
                self.assertEqual(starts, ["twt:route:strong (tier=strong)"])

    def test_fast_and_none_use_coder_model(self):
        env = {"CODER_MODEL": "fast-m"}
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

    def test_invalid_tier_raises(self):
        with self.assertRaisesRegex(ValueError, "model_tier"):
            self.run_tier("turbo", {})


class TestTierModel(unittest.TestCase):
    def test_tier_model_resolution(self):
        with mock.patch.dict("os.environ", {"CODER_MODEL": "f"}):
            self.assertEqual(coder.tier_model("strong"), "route:strong")
            self.assertEqual(coder.tier_model("fast"), "f")
            self.assertEqual(coder.tier_model(None), "f")
            with self.assertRaises(ValueError):
                coder.tier_model("x")

    def test_default_plan_fn_respects_strong_tier(self):
        built = []

        def fake_build(model_override=None):
            built.append(model_override)
            mock_model = mock.MagicMock()
            mock_model.invoke.return_value = mock.MagicMock(
                content="1. Step one\n[files: a.py]\n"
            )
            return mock_model, {"model": model_override or "default"}

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.dict(
                "os.environ",
                {
                    "EVAL_LEDGER": os.path.join(root, "ledger.jsonl"),
                },
            ),
            mock.patch.object(coder, "build_model", side_effect=fake_build),
            mock.patch.object(ledger, "record"),
        ):
            coder._default_plan_fn("task brief", "/tmp/demo", {"model_tier": "strong"})
        self.assertEqual(built, ["route:strong"])


CATALOG = [
    {"id": "nebius/big-m", "owned_by": "nebius"},
    {"id": "nvidia/z-ai/glm-5.3-flash", "owned_by": "nvidia"},
]
TWT_SPEC = {"provider": "twt", "model": "default", "base_url": "http://gw/v1"}


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestStrongTierGatewayRequest(unittest.TestCase):
    def build(self, tier, env, preflight=True):
        from backend.shared import model_router, twt_provenance

        clean = {"CODER_MODEL": "", "CODER_STRONG_MODEL": "", **env}
        with (
            mock.patch.dict("os.environ", clean, clear=True),
            mock.patch.object(model_router, "_twt_spec", return_value=dict(TWT_SPEC)),
            mock.patch.object(
                twt_provenance, "query_gateway_models", return_value=CATALOG
            ) as query,
        ):
            model, spec = coder.build_model(coder.tier_model(tier))
            body = model._get_request_payload([{"role": "user", "content": "hi"}])
        return body, spec, query

    base = {"NEBIUS_ONLY": "1", "S5_LOCAL_ONLY": "0", "OLLAMA_STRICT": "0"}

    def test_fast_keeps_default_provider_models_strong_route_sends_none(self):
        fast, _, _ = self.build("fast", self.base)
        self.assertEqual(fast["model"], "default")
        self.assertNotIn("provider_models", fast.get("extra_body") or {})
        self.assertNotIn("provider_reasoning_effort", fast.get("extra_body") or {})
        strong, spec, _ = self.build(
            "strong", dict(self.base, CODER_STRONG_MODEL="route:strong")
        )
        self.assertEqual(strong["model"], "route:strong")
        self.assertNotIn("provider_models", strong.get("extra_body") or {})
        self.assertNotIn("provider_reasoning_effort", strong.get("extra_body") or {})
        self.assertNotIn("provider_models", spec)

    def test_concrete_strong_pin_is_ignored(self):
        # No environment override path (issue #1798): even a concrete
        # CODER_STRONG_MODEL pin resolves through the gateway route and
        # sends no fast-tier selection.
        body, spec, _ = self.build(
            "strong",
            dict(self.base, CODER_MODEL="fast-m", CODER_STRONG_MODEL="nebius/big-m"),
        )
        self.assertEqual(body["model"], "route:strong")
        self.assertEqual(body.get("extra_body") or {}, {})
        self.assertNotIn("provider_models", spec)

    def test_bogus_strong_pin_does_not_preflight(self):
        # With no override path there is no pinned id to preflight: a
        # bogus CODER_STRONG_MODEL is ignored and the gateway route
        # map validation decides.
        env = dict(
            self.base, CODER_STRONG_MODEL="nebius/nope", TWT_PREFLIGHT_REQUIRED="1"
        )
        body, _, _ = self.build("strong", env)
        self.assertEqual(body["model"], "route:strong")
        good = dict(
            self.base,
            CODER_STRONG_MODEL="nebius/nope",
            TWT_PREFLIGHT_REQUIRED="1",
            TWT_STRONG_MODELS='{"nebius": "big-m"}',
        )
        body, _, query = self.build("strong", good)
        self.assertEqual(body["model"], "route:strong")
        query.assert_called_once()

    def test_preflight_route_strong_validates_gateway_strong_map(self):
        from backend.shared import twt_provenance

        env = dict(
            self.base, CODER_STRONG_MODEL="route:strong", TWT_PREFLIGHT_REQUIRED="1"
        )
        body, _, _ = self.build("strong", env)
        self.assertEqual(body["model"], "route:strong")
        good = dict(env, TWT_STRONG_MODELS='{"nebius": "big-m"}')
        self.assertEqual(self.build("strong", good)[0]["model"], "route:strong")
        for bad in (
            '{"nebius": "missing"}',
            "not json",
            "[]",
            "{}",
            '{"nebius": ""}',
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(twt_provenance.TwTPreflightError):
                    self.build("strong", dict(env, TWT_STRONG_MODELS=bad))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestStrongStartSkipsLocal(unittest.TestCase):
    def test_strong_tier_with_nebius_only_never_touches_ollama(self):
        from backend.shared import model_router

        env = {
            "NEBIUS_ONLY": "1",
            "S5_LOCAL_ONLY": "0",
            "OLLAMA_STRICT": "0",
            "CODER_MODEL": "",
            "CODER_STRONG_MODEL": "big-m",
        }
        built = []
        with (
            mock.patch.dict("os.environ", env),
            mock.patch.object(
                model_router, "ollama_reachable", side_effect=AssertionError("ollama")
            ),
            mock.patch.object(
                model_router, "_ollama_spec", side_effect=AssertionError("ollama")
            ),
            mock.patch.object(
                model_router,
                "_twt_spec",
                return_value={"provider": "twt", "model": "default"},
            ),
            mock.patch.object(
                model_router,
                "build_chat_model",
                side_effect=lambda spec, **_k: built.append(spec) or (object(), spec),
            ),
        ):
            coder.build_model(coder.tier_model("strong"))
        self.assertEqual(
            [(s["provider"], s["model"]) for s in built], [("twt", "route:strong")]
        )

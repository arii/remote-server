"""Tests for the briefing narrative lede (issue #671): narrator spec,
prompt assembly, provider call shapes, never-metered-by-default, and
SWR cache carry/patch. Stdlib unittest only."""

import json
import os
import time
import unittest
import urllib.error
from unittest import mock

from backend.api.routers import briefing as briefing_router

NARRATOR_ENV_VARS = (
    "BRIEFING_NARRATOR", "ASK_MODEL", "OLLAMA_URL",
    "NEBIUS_MODEL", "NEBIUS_BASE_URL", "NEBIUS_API_KEY",
)


class _FakeResp:
    """urlopen fake: context manager yielding a JSON payload."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class _SyncThread:
    """Thread fake: runs the target inline on start()."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


class NarratorEnvCase(unittest.TestCase):
    def setUp(self):
        self._old = {k: os.environ.get(k) for k in NARRATOR_ENV_VARS}
        for k in NARRATOR_ENV_VARS:
            os.environ.pop(k, None)
        briefing_router._BRIEFING_CACHE.clear()
        briefing_router._BRIEFING_REFRESHING.clear()

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        briefing_router._BRIEFING_CACHE.clear()
        briefing_router._BRIEFING_REFRESHING.clear()


class NarratorSpecTest(NarratorEnvCase):
    def test_default_is_local_ollama(self):
        spec = briefing_router.narrator_spec()
        self.assertEqual(spec["provider"], "ollama")
        self.assertEqual(spec["model"], "qwen3:8b")
        self.assertEqual(spec["base_url"], "http://ollama:11434")

    def test_default_honors_ask_model_env(self):
        os.environ["ASK_MODEL"] = "qwen3:14b"
        os.environ["OLLAMA_URL"] = "http://box:11434"
        spec = briefing_router.narrator_spec()
        self.assertEqual(
            spec, {"provider": "ollama", "model": "qwen3:14b",
                   "base_url": "http://box:11434"})

    def test_blank_is_default(self):
        os.environ["BRIEFING_NARRATOR"] = "   "
        self.assertEqual(
            briefing_router.narrator_spec()["provider"], "ollama")

    def test_ollama_prefix_selects_model(self):
        os.environ["BRIEFING_NARRATOR"] = "ollama:qwen2.5-coder:7b"
        spec = briefing_router.narrator_spec()
        self.assertEqual(spec["provider"], "ollama")
        self.assertEqual(spec["model"], "qwen2.5-coder:7b")

    def test_bare_ollama_uses_ask_default(self):
        os.environ["BRIEFING_NARRATOR"] = "ollama"
        spec = briefing_router.narrator_spec()
        self.assertEqual(
            (spec["provider"], spec["model"]), ("ollama", "qwen3:8b"))

    def test_prefix_case_insensitive_model_verbatim(self):
        os.environ["BRIEFING_NARRATOR"] = "Ollama:QwenX"
        spec = briefing_router.narrator_spec()
        self.assertEqual(
            (spec["provider"], spec["model"]), ("ollama", "QwenX"))

    def test_nebius_is_explicit_opt_in(self):
        os.environ["BRIEFING_NARRATOR"] = "nebius:zai-org/GLM-5.3-Flash"
        spec = briefing_router.narrator_spec()
        self.assertEqual(
            spec, {"provider": "nebius",
                   "model": "zai-org/GLM-5.3-Flash",
                   "base_url": "https://api.tokenfactory.nebius.com/v1"})

    def test_bare_nebius_uses_nebius_model_default(self):
        os.environ["BRIEFING_NARRATOR"] = "nebius"
        spec = briefing_router.narrator_spec()
        self.assertEqual(spec["provider"], "nebius")
        self.assertEqual(spec["model"], "zai-org/GLM-5.3-Flash")

    def test_off_values_disable(self):
        for val in ("off", "OFF", "none", "disabled", "0", "false", "no"):
            os.environ["BRIEFING_NARRATOR"] = val
            self.assertIsNone(briefing_router.narrator_spec(), val)

    def test_unknown_values_disable_never_raise(self):
        # No Muse assumptions: anything outside ollama:/nebius:/off
        # disables narration instead of reaching for another backend.
        for val in ("claude:sonnet", "anthropic:opus", "openai:gpt-5",
                    "mystery", "ollamaX:foo"):
            os.environ["BRIEFING_NARRATOR"] = val
            self.assertIsNone(briefing_router.narrator_spec(), val)


class NarratorPromptTest(NarratorEnvCase):
    def test_prompt_carries_briefing_shape(self):
        data = {"repo": "remote-server",
                "attention": ["#1 — CI failing"],
                "prs": [{"number": 1, "title": "t"}],
                "conflicts_lines": ["a ↔ b"]}
        prompt = briefing_router.narrator_prompt(data)
        self.assertIn("remote-server", prompt)
        self.assertIn("CI failing", prompt)
        self.assertIn("conflicts_lines", prompt)
        self.assertTrue(prompt.rstrip().endswith("Lede:"))

    def test_prompt_excludes_freshness_and_previous_prose(self):
        data = {"repo": "r", "narrative": "OLD PROSE",
                "narrative_at": 1700000000.0, "cached": True,
                "stale": False, "generated_at": 1700000000.0}
        prompt = briefing_router.narrator_prompt(data)
        self.assertNotIn("OLD PROSE", prompt)
        self.assertNotIn("narrative", prompt)
        self.assertNotIn("generated_at", prompt)

    def test_oversize_briefing_truncates(self):
        data = {"repo": "r",
                "attention": [f"item {i} " + "x" * 100
                              for i in range(200)]}
        prompt = briefing_router.narrator_prompt(data)
        self.assertLessEqual(
            len(prompt),
            briefing_router.NARRATOR_MAX_INPUT_CHARS + 100)
        self.assertIn("[truncated]", prompt)


class NarrateCallTest(NarratorEnvCase):
    def _urlopen_mock(self, payload):
        return mock.patch("urllib.request.urlopen",
                          return_value=_FakeResp(payload))

    def test_ollama_call_shape(self):
        with self._urlopen_mock({"response": "  Auth CI is red. Fix it first.  "}) as urlopen:
            prose = briefing_router.narrate_briefing({"repo": "r"})
        self.assertEqual(prose, "Auth CI is red. Fix it first.")
        req = urlopen.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/api/generate"))
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "qwen3:8b")
        self.assertEqual(body["stream"], False)
        self.assertIn("2-4 sentence", body["system"])
        self.assertIn("Briefing JSON:", body["prompt"])
        self.assertEqual(body["options"]["num_predict"], 200)
        self.assertEqual(urlopen.call_args.kwargs.get("timeout"), 90.0)

    def test_ollama_url_tolerates_generate_suffix(self):
        os.environ["OLLAMA_URL"] = "http://box:11434/api/generate"
        with self._urlopen_mock({"response": "lede"}) as urlopen:
            self.assertEqual(
                briefing_router.narrate_briefing({"repo": "r"}), "lede")
        url = urlopen.call_args.args[0].full_url
        self.assertEqual(url, "http://box:11434/api/generate")

    def test_ollama_disables_thinking(self):
        with self._urlopen_mock({"response": "lede"}) as urlopen:
            briefing_router.narrate_briefing({"repo": "r"})
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertIs(body["think"], False)

    def test_thinking_traces_are_stripped(self):
        with self._urlopen_mock(
                {"response": "<think>hmm, risks…</think>Auth CI is red."}):
            self.assertEqual(
                briefing_router.narrate_briefing({"repo": "r"}),
                "Auth CI is red.")
        with self._urlopen_mock({"response": "<think>unclosed trace"}):
            self.assertIsNone(briefing_router.narrate_briefing({"repo": "r"}))

    def test_empty_prose_is_none(self):
        with self._urlopen_mock({"response": "   \n "}):
            self.assertIsNone(briefing_router.narrate_briefing({"repo": "r"}))
        with self._urlopen_mock({}):
            self.assertIsNone(briefing_router.narrate_briefing({"repo": "r"}))

    def test_transport_failure_is_none(self):
        for exc in (urllib.error.URLError("refused"),
                    TimeoutError("slow"),
                    ConnectionError("reset")):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                self.assertIsNone(
                    briefing_router.narrate_briefing({"repo": "r"}))

    def test_output_truncates(self):
        with self._urlopen_mock({"response": "w" * 5000}):
            prose = briefing_router.narrate_briefing({"repo": "r"})
        self.assertLessEqual(
            len(prose), briefing_router.NARRATOR_MAX_OUTPUT_CHARS + 1)
        self.assertTrue(prose.endswith("…"))

    def test_default_config_never_metered(self):
        # Even with a Nebius key sitting in env, the default path
        # only talks to local Ollama — no metered traffic.
        os.environ["NEBIUS_API_KEY"] = "sekrit"
        with self._urlopen_mock({"response": "lede"}) as urlopen:
            briefing_router.narrate_briefing({"repo": "r"})
        url = urlopen.call_args.args[0].full_url
        self.assertIn("11434", url)
        self.assertNotIn("nebius", url.lower())
        req = urlopen.call_args.args[0]
        self.assertIsNone(req.get_header("Authorization"))

    def test_off_makes_no_calls(self):
        os.environ["BRIEFING_NARRATOR"] = "off"
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(briefing_router.narrate_briefing({"repo": "r"}))
        urlopen.assert_not_called()

    def test_nebius_opt_in_shape(self):
        os.environ["BRIEFING_NARRATOR"] = "nebius:zai-org/GLM-5.3-Flash"
        os.environ["NEBIUS_API_KEY"] = "sekrit"
        payload = {"choices": [{"message": {"content": "Cloud lede here."}}]}
        with self._urlopen_mock(payload) as urlopen:
            prose = briefing_router.narrate_briefing({"repo": "r"})
        self.assertEqual(prose, "Cloud lede here.")
        req = urlopen.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/chat/completions"))
        self.assertEqual(req.get_header("Authorization"), "Bearer sekrit")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "zai-org/GLM-5.3-Flash")
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertEqual(body["max_tokens"], 200)
        # No secrets in the logged-safe payload body.
        self.assertNotIn("sekrit", json.dumps(body))

    def test_nebius_without_key_makes_no_calls(self):
        os.environ["BRIEFING_NARRATOR"] = "nebius:some-model"
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(briefing_router.narrate_briefing({"repo": "r"}))
        urlopen.assert_not_called()

    def test_nebius_empty_choices_is_none(self):
        os.environ["BRIEFING_NARRATOR"] = "nebius:some-model"
        os.environ["NEBIUS_API_KEY"] = "sekrit"
        with self._urlopen_mock({"choices": []}):
            self.assertIsNone(briefing_router.narrate_briefing({"repo": "r"}))


class NarrativeCacheTest(NarratorEnvCase):
    def test_attach_sets_fresh_prose(self):
        data = {"repo": "r", "prs": []}
        with mock.patch.object(
                briefing_router, "narrate_briefing",
                return_value="Fresh lede."):
            out = briefing_router._attach_narrative(data, None)
        self.assertEqual(out["narrative"], "Fresh lede.")
        self.assertIsInstance(out["narrative_at"], float)

    def test_attach_failure_carries_previous_prose(self):
        data = {"repo": "r"}
        prev = {"narrative": "Old lede.", "narrative_at": 1700000000.0}
        with mock.patch.object(
                briefing_router, "narrate_briefing", return_value=None):
            out = briefing_router._attach_narrative(data, prev)
        self.assertEqual(out["narrative"], "Old lede.")
        self.assertEqual(out["narrative_at"], 1700000000.0)

    def test_attach_failure_without_previous_omits_block(self):
        data = {"repo": "r"}
        with mock.patch.object(
                briefing_router, "narrate_briefing", return_value=None):
            out = briefing_router._attach_narrative(data, None)
        self.assertNotIn("narrative", out)
        self.assertNotIn("narrative_at", out)

    def test_refresh_stores_fresh_prose(self):
        briefing_router._BRIEFING_REFRESHING.add("remote-server")
        with mock.patch.object(
                briefing_router, "_run_briefing",
                return_value={"repo": "remote-server"}), \
                mock.patch.object(
                    briefing_router, "narrate_briefing",
                    return_value="Fresh lede."):
            briefing_router._refresh_briefing(
                "remote-server", "arii/remote-server")
        entry = briefing_router._BRIEFING_CACHE["remote-server"][1]
        self.assertEqual(entry["narrative"], "Fresh lede.")
        self.assertIn("narrative_at", entry)

    def test_refresh_narrator_failure_carries_stale_prose(self):
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time() - 120,
            {"repo": "remote-server", "narrative": "Old lede.",
             "narrative_at": 1700000000.0})
        briefing_router._BRIEFING_REFRESHING.add("remote-server")
        with mock.patch.object(
                briefing_router, "_run_briefing",
                return_value={"repo": "remote-server"}), \
                mock.patch.object(
                    briefing_router, "narrate_briefing",
                    return_value=None):
            briefing_router._refresh_briefing(
                "remote-server", "arii/remote-server")
        entry = briefing_router._BRIEFING_CACHE["remote-server"][1]
        self.assertEqual(entry["narrative"], "Old lede.")
        self.assertEqual(entry["narrative_at"], 1700000000.0)

    def test_patch_updates_current_generation_only(self):
        ts = briefing_router._store_briefing(
            "remote-server", {"repo": "remote-server"})
        with mock.patch.object(
                briefing_router, "narrate_briefing",
                return_value="Patched lede."):
            briefing_router._patch_narrative(
                "remote-server", ts, {"repo": "remote-server"})
        entry = briefing_router._BRIEFING_CACHE["remote-server"][1]
        self.assertEqual(entry["narrative"], "Patched lede.")

    def test_patch_ignores_superseded_generation(self):
        old_ts = briefing_router._store_briefing(
            "remote-server", {"repo": "remote-server"})
        new_ts = briefing_router._store_briefing(
            "remote-server", {"repo": "remote-server",
                              "narrative": "Newer lede."})
        self.assertNotEqual(old_ts, new_ts)
        with mock.patch.object(
                briefing_router, "narrate_briefing",
                return_value="Stale prose."):
            briefing_router._patch_narrative(
                "remote-server", old_ts, {"repo": "remote-server"})
        entry = briefing_router._BRIEFING_CACHE["remote-server"][1]
        self.assertEqual(entry["narrative"], "Newer lede.")

    def test_patch_none_prose_leaves_entry(self):
        briefing_router._store_briefing(
            "remote-server", {"repo": "remote-server"})
        ts = briefing_router._BRIEFING_CACHE["remote-server"][0]
        with mock.patch.object(
                briefing_router, "narrate_briefing", return_value=None):
            briefing_router._patch_narrative(
                "remote-server", ts, {"repo": "remote-server"})
        entry = briefing_router._BRIEFING_CACHE["remote-server"][1]
        self.assertNotIn("narrative", entry)

    def test_sync_path_serves_data_and_carries_ancient_prose(self):
        os.environ["BRIEFING_NARRATOR"] = "off"
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time()
            - briefing_router.BRIEFING_CACHE_MAX_STALE - 1,
            {"repo": "remote-server", "narrative": "Old lede.",
             "narrative_at": 1700000000.0})
        with mock.patch.object(
                briefing_router, "_run_briefing",
                return_value={"repo": "remote-server",
                              "prs": []}) as run_mock:
            body = briefing_router.get_briefing(
                "remote-server", "arii/remote-server")
        self.assertEqual(run_mock.call_count, 1)
        self.assertFalse(body["cached"])
        self.assertEqual(body["narrative"], "Old lede.")
        self.assertEqual(body["narrative_at"], 1700000000.0)

    def test_sync_path_patches_prose_behind_cold_read(self):
        fake_threading = mock.Mock()
        fake_threading.Thread = _SyncThread
        with mock.patch.object(
                briefing_router, "_run_briefing",
                return_value={"repo": "remote-server", "prs": []}), \
                mock.patch.object(
                    briefing_router, "narrate_briefing",
                    return_value="Cold lede.") as narrate_mock, \
                mock.patch.object(briefing_router, "threading",
                                  fake_threading):
            body = briefing_router.get_briefing(
                "remote-server", "arii/remote-server")
        # Cold read serves data instantly (no prose yet on FIRST read —
        # the inline thread fake only models the ordering)...
        self.assertEqual(narrate_mock.call_count, 1)
        # ...and the now-cached entry carries prose for later reads.
        entry = briefing_router._BRIEFING_CACHE["remote-server"][1]
        self.assertEqual(entry["narrative"], "Cold lede.")
        reread = briefing_router.get_briefing(
            "remote-server", "arii/remote-server")
        self.assertTrue(reread["cached"])
        self.assertFalse(reread["stale"])
        self.assertEqual(reread["narrative"], "Cold lede.")
        self.assertEqual(body["repo"], "remote-server")

    def test_sync_path_spawns_no_thread_when_off(self):
        os.environ["BRIEFING_NARRATOR"] = "off"
        with mock.patch.object(
                briefing_router, "_run_briefing",
                return_value={"repo": "remote-server"}), \
                mock.patch.object(
                    briefing_router.threading, "Thread") as thread_mock:
            briefing_router.get_briefing(
                "remote-server", "arii/remote-server")
        thread_mock.assert_not_called()


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    HAS_CLIENT = True
except ImportError:  # pragma: no cover
    HAS_CLIENT = False


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class NarrativeEndpointTest(NarratorEnvCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(briefing_router.router)
        self.client = TestClient(app)

    def test_fixture_prose_passes_through_model(self):
        import tempfile

        os.environ["BRIEFING_NARRATOR"] = "off"
        path = os.path.join(tempfile.mkdtemp(), "briefing.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"repo": "other", "prs": [],
                       "narrative": "Fixture lede.",
                       "narrative_at": 1700000000.0}, fh)
        os.environ["BUTTONS_FIXTURE_BRIEFING"] = path
        try:
            resp = self.client.get(
                "/api/briefing", params={"repo": "remote-server"})
        finally:
            os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["narrative"], "Fixture lede.")
        self.assertEqual(body["narrative_at"], 1700000000.0)

    def test_cached_prose_serves_within_ttl(self):
        os.environ["BRIEFING_NARRATOR"] = "off"
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time(),
            {"repo": "remote-server", "prs": [],
             "narrative": "Cached lede.",
             "narrative_at": 1700000000.0})
        resp = self.client.get(
            "/api/briefing", params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["cached"])
        self.assertEqual(body["narrative"], "Cached lede.")
        self.assertEqual(body["narrative_at"], 1700000000.0)


if __name__ == "__main__":
    unittest.main()

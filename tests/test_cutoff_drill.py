#!/usr/bin/env python3
"""Cutoff fire-drill: static proof the console survives without Muse.

Offline companion to scripts/cutoff-drill.sh (issue #800): every
surface the drill exercises must also hold here, with no docker,
no Ollama, and no network. A failure names a hidden Muse
dependency in a default provider chain, skill content, or eval gate —
file a follow-up per red surface; do not fix it in the drill PR.

Deliberately static (imports + source scans): live probes live in
the shell drill, which can mask PATH and reach the stack. The
--proof scrub/strictness contract (issue #889) is covered here by
source scan plus hermetic router behavior; the live proof runs in
CI (cutoff-proof.yml) against a local Ollama service.
"""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "deep-agent"))

from backend.exec import agent_turn  # noqa: E402

import model_router  # noqa: E402

MUSE_RE = re.compile(r"muse", re.I)

# The only muse-bearing module in the exercised surfaces is the
# provider allowlist itself; everything else must be muse-free.
# (.muse/ grep exclusions are directory names, not dependencies.)
MUSE_FREE_MODULES = [
    "backend/briefing_summary.py",
    "backend/api/routers/briefing.py",
    "deep-agent/model_router.py",
    "deep-agent/ask_investigator.py",
    "deep-agent/deep_chat.py",
    "deep-agent/eval_lib.py",
    "deep-agent/eval_ask.py",
    "deep-agent/eval_chat.py",
]


def muse_hits(path):
    """(lineno, line) pairs mentioning muse outside .muse/ paths."""
    hits = []
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), 1):
        if MUSE_RE.search(line) and ".muse" not in line.lower():
            hits.append((n, line.strip()[:120]))
    return hits


class TestCutoffDrill(unittest.TestCase):
    def test_agy_argv_has_no_muse(self):
        sid = "00000000-0000-0000-0000-000000000000"
        first = agent_turn.turn_argv("agy", "/srv/remote-server",
                                     sid, "hi", False)
        followup = agent_turn.turn_argv("agy", "/srv/remote-server",
                                        sid, "hi", True, "cid-1")
        for argv in (first, followup):
            joined = " ".join(argv).lower()
            self.assertNotIn("muse", joined, argv)
        self.assertIn("agy", first)

    def test_cutoff_flip_selects_agy(self):
        with mock.patch.dict(os.environ,
                             {"AGENT_DEFAULT_PROVIDER": "agy"}):
            self.assertEqual(agent_turn.ordered_providers()[0], "agy")
        with mock.patch.dict(os.environ,
                             {"AGENT_DEFAULT_PROVIDER": "muse"}):
            self.assertEqual(agent_turn.ordered_providers()[0],
                             "muse")

    def test_router_rejects_muse_prefer(self):
        for bad in ("muse", "Muse", "MUSE"):
            with self.assertRaises(ValueError, msg=bad):
                model_router.resolve_llm(prefer=bad)
        with self.assertRaises(ValueError):
            model_router.chat_kwargs({"provider": "muse"})

    def test_narrator_default_is_ollama(self):
        from backend.api.routers.briefing import (narrator_prompt,
                                                  narrator_spec)
        with mock.patch.dict(os.environ, {}, clear=True):
            spec = narrator_spec()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["provider"], "ollama")
        prompt = narrator_prompt({"repo": "x", "attention": [],
                                  "narrative": "stale prose"})
        self.assertIn("Briefing JSON", prompt)
        self.assertNotIn("stale prose", prompt)

    def test_eval_gates_load_masked(self):
        import eval_ask
        import eval_chat  # noqa: F401
        import eval_lib
        self.assertTrue(eval_ask.PROMPTS)
        self.assertTrue(eval_lib.resolve_exe("deep-agent"))

    def test_surfaces_source_has_no_muse(self):
        problems = []
        for mod in MUSE_FREE_MODULES:
            for n, line in muse_hits(mod):
                problems.append(f"{mod}:{n}: {line}")
        self.assertEqual(problems, [])

    def test_drill_proof_mode_scrubs_credentials(self):
        text = (REPO_ROOT / "scripts" / "cutoff-drill.sh").read_text(
            encoding="utf-8")
        self.assertIn("--proof", text)
        for var in ("NEBIUS_API_KEY", "GEMINI_API_KEY"):
            self.assertIn(var, text, var)
        self.assertIn("CUTOFF_SCRUB_VARS", text)
        self.assertIn("still set after scrub", text)
        self.assertIn("OLLAMA_STRICT=1", text)

    def test_keyless_router_refuses_cloud_fallback(self):
        dead = {"OLLAMA_URL": "http://127.0.0.1:9",
                "OLLAMA_STRICT": "1"}
        with mock.patch.dict(os.environ, dead, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                model_router.resolve_llm()
        self.assertIn("OLLAMA_STRICT=1 refuses", str(ctx.exception))
        with mock.patch.dict(os.environ,
                              {"OLLAMA_URL": "http://127.0.0.1:9"},
                              clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                model_router.resolve_llm()
        self.assertIn("NEBIUS_API_KEY", str(ctx.exception))

    def test_proof_workflow_wires_drill(self):
        path = (REPO_ROOT / ".github" / "workflows"
                / "cutoff-proof.yml")
        self.assertTrue(path.is_file(), "cutoff-proof.yml missing")
        text = path.read_text(encoding="utf-8")
        self.assertIn("cutoff-drill.sh --proof", text)
        self.assertIn("env -u NEBIUS_API_KEY", text)
        self.assertIn("ollama/ollama:", text)
        self.assertIn("deep-agent/**", text)

    def test_skill_content_has_no_muse(self):
        self.assertFalse((REPO_ROOT / "deep-agent" / "agent_skills").exists(),
                         "agent_skills/ unified into deep-agent/skills/ (#1103)")
        skills = REPO_ROOT / "deep-agent" / "skills"
        problems = []
        for path in sorted(skills.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if MUSE_RE.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    problems.append(f"{rel}:{n}: {line.strip()[:120]}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()

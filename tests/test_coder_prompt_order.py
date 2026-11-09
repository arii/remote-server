"""Regression tests for #1217 claim 1: static-head coder prompts.

``build_system`` used to prepend dynamic spawner context before the
static ``CODER_RULES`` and ``_default_plan_fn`` prepended dynamic
skills before the static instruction, so the shared prefix across
tasks was ~7 chars and any provider-side prefix cache missed every
turn. The fix puts the static head first and the dynamic tail after
the separator (pure reorder, zero added tokens).

The shared-prefix probe re-derives the implementer's fixed probe
(/tmp/probe/measure.py, uncommitted): same two-task fixture, same
shared-prefix metric, same content-preservation asserts.
"""

import unittest
from unittest import mock

import coder

WORKTREE = "/tmp/wt"
CTX_A = {"skills": "SKILLS-A: prefer small diffs.", "errors": []}
CTX_B = {
    "skills": "SKILLS-B: unrelated skill text here.",
    "errors": ["first attempt failed"],
}
BRIEF = "Fix calc.add so it returns the sum of its arguments"
SEPARATOR = "\n\n---\n\n"


def _rules(worktree=WORKTREE):
    return coder.CODER_RULES.format(worktree=worktree)


def _old_build_system(worktree, context):
    """Pre-fix order, re-derived for the probe (dynamic head)."""
    rules = _rules(worktree)
    if context and context.strip():
        return context.strip() + SEPARATOR + rules
    return rules


class _FakeModel:
    def __init__(self):
        self.seen = None

    def invoke(self, prompt):
        self.seen = prompt
        return "1. Fix calc [files: calc.py]"


def _plan_prompt(context):
    fake = _FakeModel()
    with mock.patch.object(coder, "build_model", return_value=(fake, {})):
        coder._default_plan_fn(BRIEF, WORKTREE, dict(context))
    return fake.seen


def _shared_prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class TestBuildSystemOrder(unittest.TestCase):
    def test_static_rules_head_first_dynamic_context_trails(self):
        out = coder.build_system(WORKTREE, CTX_A["skills"])
        self.assertTrue(
            out.startswith(_rules()), "static rules must lead for prefix caching"
        )
        self.assertTrue(out.endswith(CTX_A["skills"].strip()))
        self.assertIn(SEPARATOR, out)

    def test_content_preserved(self):
        out = coder.build_system(WORKTREE, CTX_A["skills"])
        self.assertIn(CTX_A["skills"], out)
        self.assertIn(_rules(), out)

    def test_empty_context_returns_rules_only(self):
        for context in ("", "   ", None):
            self.assertEqual(coder.build_system(WORKTREE, context), _rules())


class TestSharedPrefixGrowth(unittest.TestCase):
    """Fixed-probe metric: the cross-task shared prefix must grow from
    a few chars (dynamic head) to the whole static block (static head),
    with total length unchanged (pure reorder)."""

    def test_build_system_prefix_grows_without_new_tokens(self):
        new_a = coder.build_system(WORKTREE, CTX_A["skills"])
        new_b = coder.build_system(WORKTREE, CTX_B["skills"])
        old_a = _old_build_system(WORKTREE, CTX_A["skills"])
        old_b = _old_build_system(WORKTREE, CTX_B["skills"])
        new_prefix = _shared_prefix(new_a, new_b)
        old_prefix = _shared_prefix(old_a, old_b)
        self.assertGreaterEqual(new_prefix, len(_rules()))
        self.assertLessEqual(old_prefix, 32)
        self.assertGreater(new_prefix, 10 * old_prefix)
        self.assertEqual(len(new_a), len(old_a))
        self.assertEqual(len(new_b), len(old_b))

    def test_plan_prompt_prefix_grows_without_new_tokens(self):
        new_a = _plan_prompt(CTX_A)
        new_b = _plan_prompt(CTX_B)
        new_prefix = _shared_prefix(new_a, new_b)
        self.assertGreater(
            new_prefix, 200, "static instruction head must survive new tasks"
        )
        self.assertTrue(
            new_a.startswith("Propose an implementation plan for this brief. ")
        )
        # Content preservation: same dynamic parts, same static parts.
        for prompt, ctx in ((new_a, CTX_A), (new_b, CTX_B)):
            self.assertIn("Propose an implementation plan", prompt)
            self.assertIn(BRIEF, prompt)
            self.assertIn(ctx["skills"], prompt)


class TestDefaultPlanFnOrder(unittest.TestCase):
    def test_static_instruction_leads_dynamic_skills_trail(self):
        prompt = _plan_prompt(CTX_A)
        head = "Propose an implementation plan for this brief."
        self.assertTrue(prompt.startswith(head))
        self.assertLess(prompt.index(head), prompt.index(CTX_A["skills"]))

    def test_no_skills_still_static_first(self):
        prompt = _plan_prompt({"skills": "", "errors": []})
        self.assertTrue(
            prompt.startswith("Propose an implementation plan for this brief. ")
        )
        self.assertNotIn("Skills:", prompt)
        self.assertIn(BRIEF, prompt)


class TestUnchangedPromptPaths(unittest.TestCase):
    """Claim-1 scope pins: agent_turn already appends and
    _default_implement_fn is already static-first; neither changed."""

    def test_agent_turn_appends_guardrail(self):
        from backend.exec import agent_turn

        out = agent_turn.guard_prompt("do the thing")
        self.assertTrue(out.startswith("do the thing"))

    def test_agent_turn_spec_prompts_keep_prompt_head(self):
        from backend.exec import agent_turn

        prompt = "write a design spec for the queue"
        out = agent_turn.spec_prompt(prompt)
        self.assertTrue(out.startswith(prompt))

    def test_implement_fn_already_static_first(self):
        fake = _FakeModel()

        def fake_build(*args, **kwargs):
            return fake, {}

        with mock.patch.object(coder, "build_model", fake_build):
            coder._default_implement_fn(
                BRIEF, "1. Fix calc [files: calc.py]", WORKTREE, []
            )
        self.assertTrue(fake.seen.startswith("Implement this plan"))


if __name__ == "__main__":
    unittest.main()

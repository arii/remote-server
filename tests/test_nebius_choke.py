"""Guard the Nebius spend-cap choke point (#526).

Every metered Nebius construction must flow through
model_router.resolve_llm (cap checked) + chat_kwargs. A ChatOpenAI
call with a literal model= keyword is a direct construction that
bypasses the cap. AST-only: no agent deps needed.
"""
import ast
import glob
import os
import unittest

DEEP_AGENT = "deep-agent"


def _chat_constructions(path):
    """(model_kw_lines, resolve_lines, chat_lines) for one file."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    model_kw, resolve, chat = [], [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name == "ChatOpenAI" and any(
                k.arg == "model" for k in node.keywords):
            model_kw.append(node.lineno)
        if name == "resolve_llm":
            resolve.append(node.lineno)
        if name == "ChatOpenAI":
            chat.append(node.lineno)
    return model_kw, resolve, chat


class TestNebiusChoke(unittest.TestCase):
    def test_no_direct_nebius_construction(self):
        violators = {}
        for path in sorted(glob.glob(os.path.join(DEEP_AGENT, "*.py"))):
            model_kw, _, _ = _chat_constructions(path)
            if model_kw:
                violators[os.path.basename(path)] = model_kw
        self.assertEqual(violators, {})

    def test_resolve_conflicts_checks_cap_first(self):
        _, resolve, chat = _chat_constructions(
            os.path.join(DEEP_AGENT, "resolve_conflicts.py"))
        self.assertTrue(resolve)
        self.assertTrue(chat)
        self.assertLess(min(resolve), min(chat))

    def test_triage_fanout_checks_cap_first(self):
        _, resolve, chat = _chat_constructions(
            os.path.join(DEEP_AGENT, "triage_fanout.py"))
        self.assertTrue(resolve)
        self.assertTrue(chat)
        self.assertLess(min(resolve), min(chat))

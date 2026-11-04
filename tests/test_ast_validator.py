import unittest
from unittest.mock import patch
import shutil
import sys
import os

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core_cli.ast_validator import validate_source_syntax, _check_bracket_balance
from core_cli.ast_validator import _ts_lib_dir


def _ts_toolchain_available():
    return bool(shutil.which("node")) and _ts_lib_dir() is not None


class TestAstValidator(unittest.TestCase):
    def test_valid_python_syntax(self):
        code = "def foo(x):\n    return x + 1\n"
        res = validate_source_syntax("test.py", code)
        self.assertTrue(res["valid"])
        self.assertIsNone(res["error"])
        self.assertIsNone(res["line"])

    def test_invalid_python_syntax(self):
        code = "def foo(:\n  pass"
        res = validate_source_syntax("test.py", code)
        self.assertFalse(res["valid"])
        self.assertIsNotNone(res["error"])
        self.assertIn("SyntaxError", res["error"])
        self.assertEqual(res["line"], 1)

    def test_invalid_python_indentation(self):
        code = "def foo():\npass\n"
        res = validate_source_syntax("module/sample.py", code)
        self.assertFalse(res["valid"])
        self.assertIsNotNone(res["error"])
        self.assertEqual(res["line"], 2)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not available")
    def test_valid_js_syntax_with_node(self):
        code = "const x = 1;\nfunction add(a, b) {\n  return a + b;\n}\n"
        res = validate_source_syntax("src/index.js", code)
        self.assertTrue(res["valid"])
        self.assertIsNone(res["error"])
        self.assertIsNone(res["line"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is not available")
    def test_invalid_js_syntax_with_node(self):
        code = "const x = ;\n"
        res = validate_source_syntax("src/index.js", code)
        self.assertFalse(res["valid"])
        self.assertIsNotNone(res["error"])
        self.assertEqual(res["line"], 1)

    def test_js_syntax_fallback_when_node_unavailable(self):
        with patch("core_cli.ast_validator.shutil.which", return_value=None):
            # Valid brackets
            valid_code = "function test() { const arr = [1, 2, (3 + 4)]; return arr; }"
            res_valid = validate_source_syntax("app.js", valid_code)
            self.assertTrue(res_valid["valid"])
            self.assertIsNone(res_valid["error"])
            self.assertIsNone(res_valid["line"])

            # Invalid bracket balance
            invalid_code = "function test() {\n  const x = [1, 2;\n  return x;\n}"
            res_invalid = validate_source_syntax("app.js", invalid_code)
            self.assertFalse(res_invalid["valid"])
            self.assertIsNotNone(res_invalid["error"])
            self.assertIsNotNone(res_invalid["line"])

    def test_bracket_balance_heuristics(self):
        # Mismatched bracket
        ok, err, line = _check_bracket_balance("const x = [1, 2);")
        self.assertFalse(ok)
        self.assertIn("Mismatched", err)
        self.assertEqual(line, 1)

        # Unclosed brace
        ok, err, line = _check_bracket_balance("function test() {\n  return 1;\n")
        self.assertFalse(ok)
        self.assertIn("Unclosed", err)
        self.assertEqual(line, 1)

        # Ignore brackets inside strings and comments
        code_with_strings = (
            "// unclosed { in comment\n"
            "/* unclosed [ in block comment */\n"
            "const s1 = '{';\n"
            "const s2 = \"(\";\n"
            "const s3 = `[`;\n"
            "function ok() { return true; }\n"
        )
        ok, err, line = _check_bracket_balance(code_with_strings)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIsNone(line)

    def test_jsx_tsx_handling(self):
        # Bracket-fallback contract (toolchain absent): real-toolchain
        # cases live in the transpile tests below.
        with patch("core_cli.ast_validator.shutil.which", return_value=None):
            valid_tsx = "const Comp = () => {\n  return <div><span>Hello</span></div>;\n};\n"
            res = validate_source_syntax("component.tsx", valid_tsx)
            self.assertTrue(res["valid"])
            self.assertIsNone(res["error"])
            self.assertIsNone(res["line"])

            invalid_tsx = "const Comp = () => {\n  return <div><span>Hello</span></div>;\n"
            res_invalid = validate_source_syntax("component.tsx", invalid_tsx)
            self.assertFalse(res_invalid["valid"])
            self.assertIsNotNone(res_invalid["error"])
            self.assertEqual(res_invalid["line"], 1)

    @unittest.skipUnless(_ts_toolchain_available(),
                         "node + typescript lib unavailable")
    def test_ts_rejects_bad_annotation(self):
        # Issue #488 probe: bracket-balanced but syntactically invalid.
        res = validate_source_syntax("a.ts", "const x: = 3;\n")
        self.assertFalse(res["valid"])
        self.assertIsNotNone(res["error"])
        self.assertIsNone(res.get("degraded"))

    @unittest.skipUnless(_ts_toolchain_available(),
                         "node + typescript lib unavailable")
    def test_ts_rejects_unbalanced_paren_in_template(self):
        # Issue #488 probe: template bodies are skipped wholesale by
        # the bracket heuristic, so this false-passes without tsc.
        res = validate_source_syntax(
            "a.ts", "const s = `value: ${(1 + 2};\n")
        self.assertFalse(res["valid"])
        self.assertIsNotNone(res["error"])

    @unittest.skipUnless(_ts_toolchain_available(),
                         "node + typescript lib unavailable")
    def test_tsx_valid_through_transpile(self):
        code = ("const Comp = (): unknown => {\n"
                "  return <div><span>Hello</span></div>;\n"
                "};\n")
        res = validate_source_syntax("component.tsx", code)
        self.assertTrue(res["valid"])
        self.assertIsNone(res.get("degraded"))

    @unittest.skipUnless(_ts_toolchain_available(),
                         "node + typescript lib unavailable")
    def test_ts_ignores_unresolved_imports(self):
        # Transpile is syntax-only: missing modules must not fail the
        # gate (files validate standalone, outside node_modules).
        res = validate_source_syntax(
            "a.ts", "import { x } from './nonexistent';\n"
                    "export const y: number = x;\n")
        self.assertTrue(res["valid"])

    def test_fallback_labels_degraded(self):
        with patch("core_cli.ast_validator.shutil.which", return_value=None):
            res = validate_source_syntax("a.ts", "const x = 1;\n")
            self.assertTrue(res["valid"])
            self.assertIsNotNone(res.get("degraded"))
            res_js = validate_source_syntax("a.js", "const x = 1;\n")
            self.assertTrue(res_js["valid"])
            self.assertIsNotNone(res_js.get("degraded"))

    def test_unsupported_file_types(self):
        # Unsupported file types (md, html, sh, json, etc.) should return valid: True
        res_md = validate_source_syntax("README.md", "# Some invalid { [ syntax")
        self.assertTrue(res_md["valid"])
        self.assertIsNone(res_md["error"])
        self.assertIsNone(res_md["line"])

        res_sh = validate_source_syntax("deploy.sh", "echo 'hello'")
        self.assertTrue(res_sh["valid"])
        self.assertIsNone(res_sh["error"])
        self.assertIsNone(res_sh["line"])


if __name__ == "__main__":
    unittest.main()

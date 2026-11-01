import unittest
from unittest.mock import patch
import shutil
import sys
import os

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.ast_validator import validate_source_syntax, _check_bracket_balance


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
        with patch("scripts.ast_validator.shutil.which", return_value=None):
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

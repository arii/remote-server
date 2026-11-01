#!/usr/bin/env python3
import unittest
from buttons import context_filter
from buttons import server


class TestDiffPreFiltering(unittest.TestCase):
    def test_exclude_lockfiles(self):
        diff = """diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
index 1234567..89abcdef 100644
--- a/pnpm-lock.yaml
+++ b/pnpm-lock.yaml
@@ -1,5 +1,5 @@
-foo: 1.0.0
+foo: 1.0.1
diff --git a/src/index.ts b/src/index.ts
index abcdef1..2345678 100644
--- a/src/index.ts
+++ b/src/index.ts
@@ -1,3 +1,3 @@
-console.log("old");
+console.log("new");
"""
        filtered = context_filter.filter_diff(diff)
        self.assertIn("[diff pre-filter: excluded generated/binary/lockfile 'pnpm-lock.yaml']", filtered)
        self.assertNotIn("foo: 1.0.1", filtered)
        self.assertIn("console.log(\"new\");", filtered)

    def test_exclude_svg_and_minified_assets(self):
        diff = """diff --git a/logo.svg b/logo.svg
new file mode 100644
--- /dev/null
+++ b/logo.svg
@@ -0,0 +1,5 @@
+<svg viewBox="0 0 100 100"></svg>
diff --git a/dist/bundle.min.js b/dist/bundle.min.js
index 111..222 100644
--- a/dist/bundle.min.js
+++ b/dist/bundle.min.js
@@ -1 +1 @@
-var a=1;
+var a=2;
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-x = 1
+x = 2
"""
        filtered = context_filter.filter_diff(diff)
        self.assertIn("excluded generated/binary/lockfile 'logo.svg'", filtered)
        self.assertIn("excluded generated/binary/lockfile 'dist/bundle.min.js'", filtered)
        self.assertNotIn("<svg", filtered)
        self.assertNotIn("var a=2", filtered)
        self.assertIn("+x = 2", filtered)

    def test_truncate_large_file_diff(self):
        # Create a diff with 300 lines in a single file
        lines = [
            "diff --git a/big.py b/big.py\n",
            "--- a/big.py\n",
            "+++ b/big.py\n",
            "@@ -1,300 +1,300 @@\n",
        ]
        for i in range(300):
            lines.append(f"+line {i}\n")
        diff = "".join(lines)

        filtered = context_filter.filter_diff(diff, max_file_lines=200)
        self.assertIn("[diff pre-filter: truncated 101 lines from 'big.py']", filtered)
        self.assertIn("+line 0", filtered)
        self.assertIn("+line 198", filtered)
        self.assertNotIn("+line 199", filtered)


class TestCILogExtraction(unittest.TestCase):
    def test_extract_tracebacks_and_assertions(self):
        log = "\n".join([
            f"Step line {i}: normal setup output..." for i in range(200)
        ]) + """
Traceback (most recent call last):
  File "test_runner.py", line 42, in test_fn
    assert result == 42
AssertionError: assert 0 == 42
Process completed with exit code 1.
"""
        extracted = context_filter.extract_ci_error_traces(log, max_lines=50)
        self.assertIn("Traceback (most recent call last):", extracted)
        self.assertIn("AssertionError: assert 0 == 42", extracted)
        self.assertIn("Process completed with exit code 1", extracted)
        self.assertNotIn("Step line 10:", extracted)

    def test_extract_panics(self):
        log = "\n".join([
            f"Build line {i}: compiling crate..." for i in range(250)
        ]) + """
thread 'main' panicked at 'assertion failed: `(left == right)`
  left: `1`,
 right: `2`', src/lib.rs:88:9
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
exit code 101
"""
        extracted = context_filter.extract_ci_error_traces(log, max_lines=50)
        self.assertIn("panicked at", extracted)
        self.assertIn("src/lib.rs:88:9", extracted)
        self.assertNotIn("Build line 10:", extracted)


class TestContextBudgetEnforcement(unittest.TestCase):
    def test_enforce_budget_under_limit(self):
        short = "This is a brief context message."
        self.assertEqual(context_filter.enforce_context_budget(short, max_tokens=100), short)

    def test_enforce_budget_over_limit(self):
        long_text = "A" * 20000  # 20k chars is ~5k tokens
        budgeted = context_filter.enforce_context_budget(long_text, max_tokens=1000)  # max 4000 chars
        self.assertLessEqual(len(budgeted), 4200)
        self.assertIn("[context pre-filter: truncated context to enforce 1000 token budget]", budgeted)

    def test_suggest_context_budget_integration(self):
        brief = {
            "repo": "tech-dancer",
            "attention": [f"Attention item {i}" for i in range(30)],
            "prs": [
                {"number": i, "title": f"PR title {i}", "ci": "passing", "merge": "clean", "conflicted": False, "review": "APPROVED"}
                for i in range(50)
            ],
            "failing_runs": [
                {"name": f"Workflow {i}", "branch": "main", "failed": [{"job": "build", "steps": ["test"]}]}
                for i in range(10)
            ],
            "jules": {"items": [f"Session {i}" for i in range(10)]}
        }
        ctx = server._suggest_context(brief)
        self.assertIsInstance(ctx, str)
        # Should stay well within 3k tokens (~12k chars)
        self.assertLessEqual(len(ctx), 14000)


class TestContextFilterHelpersAndIntegration(unittest.TestCase):
    def test_estimate_tokens_accuracy(self):
        self.assertEqual(context_filter.estimate_tokens(""), 0)
        self.assertEqual(context_filter.estimate_tokens("hello"), 2)
        text_100_chars = "x" * 100
        # 100 chars // 4 = 25 tokens
        self.assertEqual(context_filter.estimate_tokens(text_100_chars), 25)

    def test_is_excluded_path(self):
        self.assertTrue(context_filter.is_excluded_path("pnpm-lock.yaml"))
        self.assertTrue(context_filter.is_excluded_path("src/vendor/app.min.js"))
        self.assertTrue(context_filter.is_excluded_path("assets/icon.svg"))
        self.assertTrue(context_filter.is_excluded_path("buttons/__pycache__/server.cpython-312.pyc"))
        self.assertFalse(context_filter.is_excluded_path("src/index.ts"))
        self.assertFalse(context_filter.is_excluded_path("buttons/server.py"))

    def test_suggest_context_with_ci_traces_and_diff(self):
        brief = {
            "repo": "remote-server",
            "attention": ["PR #42 needs attention"],
            "prs": [{
                "number": 42,
                "title": "Fix something",
                "ci": "failing",
                "merge": "clean",
                "conflicted": False,
                "review": "PENDING",
                "diff": """diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
--- a/pnpm-lock.yaml
+++ b/pnpm-lock.yaml
@@ -1 +1 @@
-old: 1.0.0
+new: 1.0.1
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print(1)
+print(2)
"""
            }],
            "failing_runs": [{
                "name": "CI",
                "branch": "main",
                "failed": [{"job": "test", "steps": ["run pytest"]}],
                "log": "\n".join([f"setup line {i}" for i in range(100)]) + "\nAssertionError: expected True but got False\nexit 1\n"
            }],
        }
        ctx = server._suggest_context(brief)
        self.assertIn("diff summary:", ctx)
        self.assertIn("excluded generated/binary/lockfile 'pnpm-lock.yaml'", ctx)
        self.assertIn("+print(2)", ctx)
        self.assertIn("CI error trace:", ctx)
        self.assertIn("AssertionError: expected True but got False", ctx)
        self.assertNotIn("setup line 10\n", ctx)


if __name__ == "__main__":
    unittest.main()

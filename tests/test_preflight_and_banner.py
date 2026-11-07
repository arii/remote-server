#!/usr/bin/env python3
"""Tests for preflight_digest.py and error banner extraction logic."""

import json
import os
import shutil
import subprocess
import sys
import unittest

import backend.preflight_digest as pfd


def _load_preflight_script():
    """Import scripts/verify-co-browsing-preflight.py by path.

    The filename is not importable, so load it as a module the same
    way a subprocess would — no side effects (main-guarded).
    """
    import importlib.util

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "scripts",
                        "verify-co-browsing-preflight.py")
    spec = importlib.util.spec_from_file_location(
        "verify_co_browsing_preflight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _full_js_toolchain_available():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tsc = os.path.join(root, "frontend", "node_modules", ".bin", "tsc")
    return (
        shutil.which("node") is not None
        and shutil.which("pnpm") is not None
        and shutil.which("fallow") is not None
        and os.path.isfile(tsc)
    )


class TestPreflightDigest(unittest.TestCase):
    def run_digest(self, payload):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run(
            [sys.executable, "backend/preflight_digest.py"],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc.returncode, proc.stdout

    def test_all_passing(self):
        data = {
            "doctor": {"status": "ok"},
            "typecheck": {"status": "pass"},
            "lint": {"status": "clean"},
        }
        code, out = self.run_digest(data)
        self.assertEqual(code, 0)
        self.assertIn("✓ Doctor: OK", out)
        self.assertIn("✓ Typecheck: OK", out)
        self.assertIn("✓ Lint: OK", out)
        self.assertIn("preflight: 3 passed, 0 failed", out)

    def test_multi_status_checklist(self):
        # doctor: ok, typecheck: skipped, lint: warnings
        data = {
            "doctor": {"status": "ok"},
            "typecheck": {"status": "skipped"},
            "lint": {"status": "warnings", "warnings": 2},
        }
        code, out = self.run_digest(data)
        self.assertEqual(code, 0)
        self.assertIn("✓ Doctor: OK", out)
        self.assertIn("⊘ Typecheck: Skipped", out)
        self.assertIn("⚠ Lint: Warnings — 2 warnings", out)
        self.assertIn("preflight: 1 passed, 1 warnings, 1 skipped, 0 failed", out)

    def test_failure_causes_exit_1(self):
        data = {
            "doctor": {"status": "ok"},
            "typecheck": {
                "status": "failed",
                "stderr": "TypeScript compiler error: TS2304",
            },
        }
        code, out = self.run_digest(data)
        self.assertEqual(code, 1)
        self.assertIn("✓ Doctor: OK", out)
        self.assertIn("✗ Typecheck: FAIL — TypeScript compiler error: TS2304", out)
        self.assertIn("preflight: 1 passed, 1 failed", out)

    def test_unrecognized_json_fails_closed(self):
        # Zero classifiable checks: nonzero exit + banner + raw (#489).
        raw = '{"some_random_key": 123}'
        code, out = self.run_digest(raw)
        self.assertEqual(code, 2)
        self.assertIn(pfd.UNRECOGNIZED_BANNER, out)
        self.assertIn(raw.strip(), out)

    def test_invalid_json_fails_closed(self):
        # Non-JSON stdin: nonzero exit + banner + raw passthrough (#489).
        raw = "Not valid json\nJust plain text"
        code, out = self.run_digest(raw)
        self.assertEqual(code, 2)
        self.assertIn(pfd.UNRECOGNIZED_BANNER, out)
        self.assertIn("Not valid json", out)

    def test_empty_stdin_fails_closed(self):
        code, out = self.run_digest("")
        self.assertEqual(code, 2)
        self.assertIn(pfd.UNRECOGNIZED_BANNER, out)

    def test_unrecognized_banner_is_extractable_failure(self):
        # The banner must carry the ✗/FAIL marks the buttons
        # error-line extraction looks for, so the loud failure
        # surfaces instead of a raw JSON dump.
        code, out = self.run_digest('{"drifted": true}')
        self.assertNotEqual(code, 0)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("✗"))
        self.assertIn("FAIL", first)

    @unittest.skipUnless(
        _full_js_toolchain_available(),
        "needs full JS toolchain (node, pnpm, tsc, fallow)",
    )
    def test_package_scripts_and_fallow(self):
        # Verify npm run type-check passes
        proc_tc = subprocess.run(
            ["npm", "run", "type-check"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc_tc.returncode, 0, f"npm run type-check failed: {proc_tc.stderr}")

        # Verify npm run lint passes
        proc_lint = subprocess.run(
            ["npm", "run", "lint"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc_lint.returncode, 0, f"npm run lint failed: {proc_lint.stderr}")

        # Verify fallow audit runs successfully
        proc_fallow = subprocess.run(
            ["pnpm", "run", "audit:fallow"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc_fallow.returncode, 0, f"pnpm run audit:fallow failed: {proc_fallow.stderr}")


@unittest.skipUnless(shutil.which("node"), "Node.js is not available")
class TestBannerExtraction(unittest.TestCase):
    def extract_error_line_js(self, output_text):
        node_code = f"""
const outEl = {{ textContent: {json.dumps(output_text)} }};

function lastErrorLines() {{
  const rawText = outEl.textContent || "";
  const lines = rawText.split("\\n").map(l => {{
    const i = l.lastIndexOf("\\r");
    return (i >= 0 ? l.slice(i + 1) : l).trim();
  }}).filter(l => l && !l.startsWith("$ ") &&
    !/^step failed with exit code \\d+; aborting$/.test(l));
  if (!lines.length) return "";

  // 1. Try extracting structured message/error from a JSON block in the output
  const jsonStart = rawText.indexOf("{{");
  const jsonEnd = rawText.lastIndexOf("}}");
  if (jsonStart >= 0 && jsonEnd > jsonStart) {{
    try {{
      const obj = JSON.parse(rawText.slice(jsonStart, jsonEnd + 1));
      function findMsg(node) {{
        if (!node) return null;
        if (typeof node === "string") return node;
        if (typeof node.message === "string" && node.message.trim()) return node.message.trim();
        if (typeof node.error === "string" && node.error.trim()) return node.error.trim();
        if (node.error && typeof node.error === "object") return findMsg(node.error);
        if (typeof node.detail === "string" && node.detail.trim()) return node.detail.trim();
        return null;
      }}
      const msg = findMsg(obj);
      if (msg) return "\\n" + msg.split("\\n")[0].slice(0, 220);
    }} catch (e) {{}}
  }}

  // 2. Try regex extraction of JSON "message" or "error" lines
  for (let i = lines.length - 1; i >= 0; i--) {{
    const l = lines[i];
    const m = l.match(/"(?:message|error|detail)"\\s*:\\s*"([^"\\\\]*(?:\\\\.[^"\\\\]*)*)"/i);
    if (m) {{
      try {{
        const val = JSON.parse('"' + m[1] + '"');
        if (val && typeof val === "string") return "\\n" + val.split("\\n")[0].slice(0, 220);
      }} catch (e) {{
        return "\\n" + m[1].split("\\n")[0].slice(0, 220);
      }}
    }}
  }}

  // 3. Preflight check failures or compact checklist failure line (e.g. ✗ CheckName: FAIL)
  for (let i = lines.length - 1; i >= 0; i--) {{
    const l = lines[i];
    if (l.startsWith("✗") || /:\\s*FAIL\\b/i.test(l)) {{
      return "\\n" + l.slice(0, 220);
    }}
  }}

  // 4. Traceback or error-like line
  const isErr = l =>
    /"(message|error)"\\s*:|traceback|error|failed|exception/i.test(l);
  let pick = -1;
  for (let i = lines.length - 1; i >= 0; i--) {{
    if (isErr(lines[i])) {{ pick = i; break; }}
  }}
  const line = pick >= 0 ? lines[pick] : lines[lines.length - 1];
  return "\\n" + line.slice(0, 220);
}}

process.stdout.write(lastErrorLines());
"""
        proc = subprocess.run(
            ["node", "-e", node_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc.stdout.strip()

    def test_json_error_extraction(self):
        output = """$ td --json preflight
{
  "status": "error",
  "error": {
    "code": "MISSING_CONFIG",
    "message": "project_config.json could not be found"
  }
}
step failed with exit code 1; aborting
"""
        extracted = self.extract_error_line_js(output)
        self.assertEqual(extracted, "project_config.json could not be found")

    def test_preflight_checklist_failure_extraction(self):
        output = """$ set -o pipefail; td --json preflight | python3 backend/preflight_digest.py
✓ Doctor: OK
⊘ Typecheck: Skipped
✗ Lint: FAIL — 3 errors
preflight: 1 passed, 1 skipped, 1 failed
step failed with exit code 1; aborting
"""
        extracted = self.extract_error_line_js(output)
        self.assertEqual(extracted, "✗ Lint: FAIL — 3 errors")

    def test_traceback_extraction(self):
        output = """Traceback (most recent call last):
  File "main.py", line 10, in <module>
    run_action()
RuntimeError: connection refused to redis
step failed with exit code 1; aborting
"""
        extracted = self.extract_error_line_js(output)
        self.assertIn("RuntimeError: connection refused to redis", extracted)


class TestGridChromeArgs(unittest.TestCase):
    def test_shm_flag_present(self):
        """Grid Chrome caps must carry --disable-dev-shm-usage (#1104).

        /dev/shm exhaustion SIGKILLs renderers; the flag is the same
        hardening browser_view.py already applies. Single-sourced on
        GRID_CHROME_ARGS so the caps and the pin cannot drift apart.
        """
        mod = _load_preflight_script()
        args = list(mod.GRID_CHROME_ARGS)
        self.assertIn("--user-data-dir=/data/shared-browser/agent",
                      args)
        self.assertIn("--enable-features=WebMCP", args)
        self.assertIn("--disable-dev-shm-usage", args)


if __name__ == "__main__":
    unittest.main()

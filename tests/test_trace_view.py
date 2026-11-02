"""trace_view.py + runs.py tests. Stdlib-only, no venv, no model calls.
Run: python3 tests/test_trace_view.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

DEEP_AGENT = os.path.join(os.path.dirname(__file__), "..", "deep-agent")
FIXTURES = os.path.join(DEEP_AGENT, "tests", "fixtures")
TRACE_VIEW = os.path.join(DEEP_AGENT, "trace_view.py")

sys.path.insert(0, DEEP_AGENT)

import runs
import trace_view


def write_trace(d, name, lines):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


EVENTS = [
    {"type": "run_start", "script": "s.py", "thread": "t1",
     "model": "m", "ts": 1000.0},
    {"type": "model_request", "n_messages": 2, "ts": 1000.1},
    {"type": "model_response",
     "usage": {"input_tokens": 1, "output_tokens": 2,
               "total_tokens": 3}, "ts": 1000.2},
    {"type": "tool_call", "name": "probe", "args_preview": "x" * 200,
     "duration_s": 0.5, "ok": True, "result_preview": "y" * 200,
     "ts": 1000.3},
    {"type": "tool_call", "name": "bad", "args_preview": "",
     "duration_s": 0.1, "ok": False, "error": "boom", "ts": 1000.4},
    {"type": "weird_future_event", "ts": 1000.5},
    {"type": "run_end", "duration_s": 1.5, "ok": True, "ts": 1001.5},
]


class TestParse(unittest.TestCase):
    def test_malformed_skipped_with_count(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_trace(d, "t.jsonl", [
                json.dumps(EVENTS[0]),
                "not json {",
                "[1, 2]",
                json.dumps({"no": "type"}),
                "",
                json.dumps(EVENTS[-1]),
            ])
            events, malformed = trace_view.parse_trace(path)
            self.assertEqual(len(events), 2)
            self.assertEqual(malformed, 3)

    def test_blank_lines_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_trace(d, "t.jsonl", ["", "   ", ""])
            events, malformed = trace_view.parse_trace(path)
            self.assertEqual((events, malformed), ([], 0))


class TestText(unittest.TestCase):
    def test_caps_widths_with_marker(self):
        text = trace_view.render_text(EVENTS, 0, "lbl")
        for line in text.splitlines():
            self.assertLessEqual(len(line), trace_view.TEXT_WIDTH,
                                 f"too long: {line!r}")
        self.assertIn(trace_view.TRUNC, text)

    def test_covers_shapes_and_summary(self):
        text = trace_view.render_text(EVENTS, 2, "lbl")
        for needle in ("START s.py", "thread=t1", "LLM req", "LLM resp",
                       "in=1 out=2 tot=3", "probe 0.5s", "ERR bad",
                       "err=boom", "? weird_future_event", "END ok=True",
                       "tools ok=1 err=1", "skipped 2 malformed lines"):
            self.assertIn(needle, text)

    def test_golden_fixture_transcript(self):
        trace = os.path.join(FIXTURES, "demo_run.jsonl")
        golden = os.path.join(FIXTURES, "demo_run.txt")
        events, malformed = trace_view.parse_trace(trace)
        self.assertEqual(malformed, 1)
        got = trace_view.render_text(events, malformed, "demo_run")
        with open(golden, encoding="utf-8") as f:
            self.assertEqual(got, f.read())


class TestHtml(unittest.TestCase):
    def test_contains_names_durations_tokens(self):
        page = trace_view.render_html(EVENTS, 1, "lbl")
        for needle in ("probe", "0.5s", "bad", "boom", "in=1",
                       "out=2", "tot=3", "START", "END",
                       "skipped 1 malformed line", "viewport",
                       "max-width", "prefers-color-scheme"):
            self.assertIn(needle, page)
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_escapes_previews(self):
        evil = dict(EVENTS[3], args_preview="<b>&amp;</b>",
                    result_preview="</pre><script>")
        page = trace_view.render_html([evil], 0, "<lbl>")
        self.assertNotIn("<b>", page)
        self.assertNotIn("</pre><script>", page)
        self.assertIn("&lt;lbl&gt;", page)

    def test_well_formed_tags(self):
        from html.parser import HTMLParser

        class Checker(HTMLParser):
            VOID = {"meta", "br", "hr", "img", "link", "input"}

            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack = []
                self.errors = []

            def handle_starttag(self, tag, attrs):
                if tag not in self.VOID:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
                else:
                    self.errors.append(tag)

        page = trace_view.render_html(EVENTS, 0, "lbl")
        checker = Checker()
        checker.feed(page)
        checker.close()
        self.assertEqual(checker.errors, [])
        self.assertEqual(checker.stack, [])


class TestRuns(unittest.TestCase):
    def _seed(self, d):
        traces = os.path.join(d, "traces")
        os.makedirs(traces)
        old = os.path.join(traces, "s_t1_20260101T000000.jsonl")
        new = os.path.join(traces, "s_t1_20260201T000000.jsonl")
        for p in (old, new):
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"type": "run_start", "script": "s.py",
                     "thread": "t1", "model": "m", "ts": 1.0}) + "\n")
        os.utime(old, (1, 1))
        os.utime(new, (2, 2))
        ledger = os.path.join(d, "ledger.jsonl")
        with open(ledger, "w", encoding="utf-8") as f:
            f.write(json.dumps(
                {"thread_id": "t9",
                 "extra": {"trace": new}}) + "\n")
        return traces, ledger, old, new

    def test_resolve_paths_stems_threads_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            traces, ledger, _old, new = self._seed(d)
            self.assertEqual(runs.resolve_run(new, traces), new)
            self.assertEqual(
                runs.resolve_run(os.path.basename(new), traces), new)
            self.assertEqual(
                runs.resolve_run(os.path.basename(new)[:-6], traces), new)
            self.assertEqual(runs.resolve_run("s_t1", traces), new)
            self.assertEqual(runs.resolve_run("t1", traces), new)
            self.assertEqual(
                runs.resolve_run("t9", traces, ledger), new)
            self.assertIsNone(runs.resolve_run("nope", traces, ledger))
            self.assertIsNone(runs.resolve_run("", traces, ledger))

    def test_list_traces_newest_first(self):
        with tempfile.TemporaryDirectory() as d:
            traces, _ledger, _old, new = self._seed(d)
            found = runs.list_traces(traces)
            self.assertEqual([r["trace_path"] for r in found],
                             [new, os.path.join(
                                 traces, "s_t1_20260101T000000.jsonl")])
            self.assertEqual(found[0]["thread"], "t1")
            self.assertEqual(len(runs.list_traces(traces, limit=1)), 1)

    def test_resolve_run_id_from_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            traces, _ledger, _old, new = self._seed(d)
            ledger = os.path.join(d, "runs.jsonl")
            with open(ledger, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"thread_id": "t1",
                     "extra": {"trace": new}}) + "\n")
            self.assertEqual(
                runs.resolve_run("run-0", traces, ledger), new)
            self.assertIsNone(
                runs.resolve_run("run-9", traces, ledger))

    def test_missing_dirs_never_raise(self):
        self.assertEqual(runs.list_traces("/nonexistent-xyz"), [])
        self.assertIsNone(runs.resolve_run(
            "x", "/nonexistent-xyz", "/nonexistent-xyz/l.jsonl"))


class TestCli(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, TRACE_VIEW, *args],
            capture_output=True, text=True, timeout=60)

    def test_missing_file_sorry_nonzero(self):
        proc = self._run("/nonexistent-xyz.jsonl")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Sorry,", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_text_stdout_and_html_file(self):
        trace = os.path.join(FIXTURES, "demo_run.jsonl")
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.html")
            proc = self._run(trace, "--html", out, "--text")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("trace demo_run", proc.stdout)
            with open(out, encoding="utf-8") as f:
                page = f.read()
            self.assertIn("<!doctype html>", page)
            self.assertGreater(len(page.encode("utf-8")), 1024)

    def test_default_is_text(self):
        trace = os.path.join(FIXTURES, "demo_run.jsonl")
        proc = self._run(trace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("trace demo_run", proc.stdout)


if __name__ == "__main__":
    unittest.main()

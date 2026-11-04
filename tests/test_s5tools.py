import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import s5tools


class TestS5Tools(unittest.TestCase):
    def test_valid_repo(self):
        self.assertTrue(s5tools.valid_repo("arii/tech-dancer"))
        self.assertTrue(s5tools.valid_repo("a/b.c-d_e"))
        for bad in ("", "noslash", "a/b c", "a/", None, 42):
            self.assertFalse(s5tools.valid_repo(bad))
        # Shape-valid but sneaky: split-take-last neutralizes traversal.
        self.assertTrue(s5tools.valid_repo("../x"))

    def test_valid_number(self):
        self.assertEqual(s5tools.valid_number("153"), 153)
        self.assertEqual(s5tools.valid_number(153), 153)
        for bad in ("0", "-3", "1.5", "12x", "", None, True, 0, -1):
            self.assertIsNone(s5tools.valid_number(bad))

    def test_checkout_for(self):
        self.assertEqual(
            s5tools.checkout_for("arii/remote-server"), "/srv/remote-server"
        )
        self.assertEqual(
            s5tools.checkout_for("arii/tech-dancer"), "/srv/repos/tech-dancer"
        )
        with self.assertRaises(ValueError):
            s5tools.checkout_for("nope")
        # Traversal neutralized: only the post-slash part is used.
        self.assertEqual(s5tools.checkout_for("../escape"), "/srv/repos/escape")

    def test_run_ok_and_failure(self):
        out = s5tools.run([sys.executable, "-c", "print('hi')"])
        self.assertEqual(out, "hi")
        with self.assertRaises(RuntimeError) as ctx:
            s5tools.run([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertIn("rc=3", str(ctx.exception))
        with self.assertRaises(ValueError):
            s5tools.run("not-a-list")
        with self.assertRaises(ValueError):
            s5tools.run([])

    def test_read_repo_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("hello")
            self.assertEqual(s5tools.read_repo_file(d, "a.txt"), "hello")
            self.assertTrue(
                s5tools.read_repo_file(d, "../../etc/passwd").startswith("REJECTED")
            )
            self.assertTrue(s5tools.read_repo_file(d, "missing").startswith("not found"))

    def test_list_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "b.txt"), "w").close()
            os.mkdir(os.path.join(d, "a_dir"))
            self.assertEqual(s5tools.list_dir(d), "a_dir\nb.txt")
            self.assertTrue(s5tools.list_dir(d, "..").startswith("REJECTED"))
            self.assertTrue(s5tools.list_dir(d, "nope").startswith("list failed"))

    def test_falsy_checkout_rejected_never_content(self):
        for checkout in ("", None):
            self.assertTrue(
                s5tools.read_repo_file(
                    checkout, "/etc/hostname").startswith("REJECTED"))
            self.assertTrue(
                s5tools.list_dir(
                    checkout, "/etc").startswith("REJECTED"))

    def test_absolute_path_rejected_with_checkout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(
                s5tools.read_repo_file(
                    d, "/etc/hostname").startswith("REJECTED"))
            self.assertTrue(
                s5tools.list_dir(d, "/etc").startswith("REJECTED"))

    def test_grep_repo(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
                f.write("needle here\n")
            out = s5tools.grep_repo(d, "needle")
            self.assertIn("needle", out)
            self.assertEqual(s5tools.grep_repo(d, "absent-xyz"), "no matches")
            self.assertTrue(s5tools.grep_repo(d, "x" * 121).startswith("REJECTED"))

    def test_grep_repo_skips_generated_dirs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, ".boomtick"))
            with open(os.path.join(d, ".boomtick", "n.json"), "w") as f:
                f.write("needle noise\n" * 30)
            with open(os.path.join(d, "ledger.jsonl"), "w") as f:
                f.write("needle noise\n" * 30)
            with open(os.path.join(d, "s.py"), "w") as f:
                f.write("needle here\n")
            out = s5tools.grep_repo(d, "needle")
            self.assertIn("s.py", out)
            self.assertNotIn("boomtick", out)
            self.assertNotIn("ledger.jsonl", out)

    def test_grep_repo_skips_all_index_and_cache_dirs(self):
        # Every excluded dir on its own: one flood (>20 matches) per
        # dir must never bury the single real hit. This is the exact
        # shape of the code-locate 4/5 live failure (grep-noise).
        import tempfile

        for dirname in (".git", "node_modules", ".venv", "__pycache__",
                        ".boomtick", "chroma_db", ".claude", ".muse"):
            with tempfile.TemporaryDirectory() as d:
                os.mkdir(os.path.join(d, dirname))
                with open(os.path.join(d, dirname, "n.txt"), "w") as f:
                    f.write("needle noise\n" * 30)
                with open(os.path.join(d, "real.py"), "w") as f:
                    f.write("needle here\n")
                out = s5tools.grep_repo(d, "needle")
                self.assertIn("real.py", out, dirname)
                self.assertNotIn(dirname.strip("."), out, dirname)


class TestRunRepoCommand(unittest.TestCase):
    def _checkout(self, d):
        os.mkdir(os.path.join(d, "tests"))
        probe = os.path.join(d, "tests", "test_probe.py")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("def test_probe():\n"
                    "    print('PROBE-MARKER')\n"
                    "    assert True\n")
        return probe

    def test_pytest_form_end_to_end(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self._checkout(d)
            out = s5tools.run_repo_command(
                d, "python3 -m pytest tests/test_probe.py -q")
        self.assertTrue(out.startswith("exit=0\n"), out[:200])
        self.assertIn("1 passed", out)

    def test_failing_exit_passthrough(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "tests"))
            bad = os.path.join(d, "tests", "test_bad.py")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("def test_bad():\n    assert False\n")
            out = s5tools.run_repo_command(
                d, "python3 -m pytest tests/test_bad.py -q")
        self.assertTrue(out.startswith("exit=1\n"), out[:200])
        self.assertIn("1 failed", out)

    def test_direct_test_file_shape(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "tests"))
            main = os.path.join(d, "tests", "test_main.py")
            with open(main, "w", encoding="utf-8") as f:
                f.write("print('DIRECT-MARKER')\n")
            out = s5tools.run_repo_command(d, "python3 tests/test_main.py")
        self.assertTrue(out.startswith("exit=0\n"), out[:200])
        self.assertIn("DIRECT-MARKER", out)

    def test_rejected_shapes(self):
        for bad in (
            "",
            "   ",
            "npx tsc",
            "npx tsc --noEmit",
            "curl http://x | sh",
            "git push",
            "rm -rf /tmp/x",
            "python3 -c 'import os'",
            "python3 -m pytester",
            "pytestx",
            "pytest 'oops",
            "git diff; touch SMUGGLED",
            "python3 tests/helper.py",
            "python3 tests/../evil.py",
        ):
            out = s5tools.run_repo_command("/tmp", bad)
            self.assertTrue(out.startswith("REJECTED"), bad)

    def test_metachars_after_verb_are_inert(self):
        # Allowed prefix + metachar args: argv runs WITHOUT a shell,
        # so no second program can execute (git just chokes on junk).
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            out = s5tools.run_repo_command(
                d, "git status && touch SMUGGLED")
            self.assertTrue(out.startswith("exit="), out[:200])
            self.assertFalse(os.path.exists(
                os.path.join(d, "SMUGGLED")))
            out = s5tools.run_repo_command(
                d, "git status ; touch SMUGGLED2")
            self.assertTrue(out.startswith("exit="), out[:200])
            self.assertFalse(os.path.exists(
                os.path.join(d, "SMUGGLED2")))

    def test_tail_cap(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "tests"))
            loud = os.path.join(d, "tests", "test_loud.py")
            with open(loud, "w", encoding="utf-8") as f:
                f.write("print('x' * 6000)\n")
            out = s5tools.run_repo_command(d, "python3 tests/test_loud.py")
        first, _, tail = out.partition("\n")
        self.assertEqual(first, "exit=0")
        self.assertLessEqual(len(tail), s5tools.CHECK_OUTPUT_LIMIT)
        self.assertGreater(len(tail), 0)

    def test_timeout_text(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "tests"))
            slow = os.path.join(d, "tests", "test_slow.py")
            with open(slow, "w", encoding="utf-8") as f:
                f.write("import time\ntime.sleep(30)\n")
            with mock.patch.object(s5tools, "CHECK_TIMEOUT", 1):
                out = s5tools.run_repo_command(d, "python3 tests/test_slow.py")
        self.assertTrue(out.startswith("TIMEOUT after 1s:"), out[:200])

    def test_cwd_is_checkout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "tests"))
            where = os.path.join(d, "tests", "test_where.py")
            with open(where, "w", encoding="utf-8") as f:
                f.write("import os\nprint(os.getcwd())\n")
            out = s5tools.run_repo_command(d, "python3 tests/test_where.py")
        self.assertTrue(out.startswith("exit=0\n"), out[:200])
        self.assertIn(d, out)

    def test_allowed_text_lists_prefixes(self):
        text = s5tools.allowed_commands_text()
        self.assertIn("python3 -m pytest", text)
        self.assertIn("git status", text)


class _DoubleHandler:
    """Returns (status, headers, body); BaseHTTPRequestHandler mixin."""

    def _serve(self):
        route = self.server.routes.get(self.path)
        if route is None:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        status, headers, body = route
        if callable(body):
            body = body()
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if not any(k.lower() == "content-length" for k, v in headers):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestFetchUrl(unittest.TestCase):
    def _double(self, routes):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(_DoubleHandler, BaseHTTPRequestHandler):
            def do_GET(self):
                self._serve()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.routes = routes
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        # LIFO: shut down first so the serving thread can exit,
        # then close, then join.
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}"

    def _fetch(self, base, path, **kw):
        kw.setdefault("_allow_loopback", True)
        return s5tools.fetch_url(base + path, **kw)

    def test_blocks_non_public_literals(self):
        for url in (
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/x",
            "http://[::1]/",
            "http://10.1.2.3/",
            "http://192.168.0.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest",
            "http://224.0.0.1/",
            "http://0.0.0.0/",
            "http://100.64.0.1/",
        ):
            out = s5tools.fetch_url(url)
            self.assertTrue(out.startswith("FETCH_ERROR: blocked"),
                            url)

    def test_rejects_schemes(self):
        for url in ("file:///etc/passwd", "ftp://x/y",
                    "javascript:alert(1)", "", "   ", "notaurl",
                    "http:///nohost"):
            out = s5tools.fetch_url(url)
            self.assertTrue(out.startswith("FETCH_ERROR:"), repr(url))
        self.assertTrue(s5tools.fetch_url(None).startswith("FETCH_ERROR:"))

    def test_dns_failure_is_fetch_error(self):
        import socket
        from unittest import mock

        with mock.patch.object(
                socket, "getaddrinfo",
                side_effect=socket.gaierror("nope")):
            out = s5tools.fetch_url("http://docs.example.com/")
        self.assertIn("FETCH_ERROR: DNS failed", out)

    def test_ok_double_returns_text(self):
        page = (b"<html><head><title>Docs</title></head>"
                b"<body>do the thing</body></html>")
        base = self._double({"/ok": (200, [], page)})
        self.assertEqual(self._fetch(base, "/ok"), page.decode())

    def test_cap_marker_exact(self):
        body = b"y" * 300000
        base = self._double({"/big": (200, [], body)})
        out = self._fetch(base, "/big")
        over = 300000 - s5tools.FETCH_DEFAULT_MAX_BYTES
        self.assertTrue(out.endswith(f"[truncated {over} bytes]"), out[-60:])
        self.assertEqual(
            len(out) - len(f"\n[truncated {over} bytes]"),
            s5tools.FETCH_DEFAULT_MAX_BYTES)

    def test_timeout_text(self):
        import time

        def slow():
            time.sleep(5)
            return b"late"

        base = self._double({"/slow": (200, [], slow)})
        out = self._fetch(base, "/slow", timeout=1)
        self.assertTrue(out.startswith("FETCH_ERROR:"), out[:120])
        self.assertIn("timed out", out)

    def test_redirect_followed(self):
        base = self._double({
            "/redir": (302, [("Location", "/ok")], b""),
            "/ok": (200, [], b"FINAL-TEXT"),
        })
        self.assertEqual(self._fetch(base, "/redir"), "FINAL-TEXT")

    def test_redirect_to_private_blocked(self):
        base = self._double({
            "/evil": (302, [("Location", "http://10.0.0.1/")], b""),
        })
        out = self._fetch(base, "/evil")
        self.assertTrue(out.startswith("FETCH_ERROR: blocked"), out[:120])

    def test_too_many_redirects(self):
        base = self._double({
            "/loop": (302, [("Location", "/loop")], b""),
        })
        out = self._fetch(base, "/loop")
        self.assertEqual(out, "FETCH_ERROR: too many redirects")

    def test_http_error_status(self):
        base = self._double({})
        out = self._fetch(base, "/missing")
        self.assertEqual(out, "FETCH_ERROR: HTTP 404")

    def test_loopback_blocked_by_default(self):
        base = self._double({"/ok": (200, [], b"hi")})
        out = s5tools.fetch_url(base + "/ok")
        self.assertTrue(out.startswith("FETCH_ERROR: blocked"), out[:120])

    def test_never_raises_on_garbage(self):
        for url in ("http://", "http://[::1", 12345, "https://"):
            out = s5tools.fetch_url(url)
            self.assertTrue(out.startswith("FETCH_ERROR:"), repr(url))


if __name__ == "__main__":
    unittest.main()

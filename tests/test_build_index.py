"""build_index pattern/skip/metadata/gh-pass/multi-repo tests + smoke.

Collector + metadata + gh-pass + discovery tests are stdlib-only
(build_index keeps its langchain imports inside build(); gh tests
stub the subprocess runner or inject a stub fetcher, discovery
tests stub the git runner — no network). The smoke test needs the
deep-agent venv AND a reachable Ollama (real nomic embeddings into
a temp Chroma dir — never the prod chroma_db); it skips otherwise.
Run: python3 tests/test_build_index.py (smoke needs the venv python)
"""
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import build_index

try:
    import langchain_chroma  # noqa: F401
    import langchain_core.documents  # noqa: F401
    import langchain_text_splitters  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _ollama_up():
    base = (
        os.environ.get("OLLAMA_URL") or "http://ollama:11434").rstrip("/")
    try:
        with urllib.request.urlopen(
                base + "/api/tags", timeout=5) as res:
            return res.status == 200
    except Exception:
        return False


def _write(root, relpath, content="x = 1\n"):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestCollectSourceFiles(unittest.TestCase):
    def _fixture_root(self, root):
        _write(root, "docs/guide.md", "# guide\n")
        _write(root, "AGENTS.md", "# agents\n")
        _write(root, "backend/app.py", "VALUE = 1\n")
        _write(root, "backend/tests/test_app.py", "t = 1\n")
        _write(root, "backend/__pycache__/app.py", "cached\n")
        _write(root, "deep-agent/top.py", "TOP = 1\n")
        _write(root, "deep-agent/nested/deep.py", "DEEP = 1\n")
        _write(root, "deep-agent/.venv/lib/x.py", "v = 1\n")
        _write(root, "deep-agent/traces/t.py", "t = 1\n")
        _write(root, "deep-agent/chroma_db/c.py", "c = 1\n")
        _write(root, "frontend/src/a.ts", "export const a = 1;\n")
        _write(root, "frontend/src/views/b.tsx", "export const b = 1;\n")
        _write(root, "frontend/src/c.test.ts", "test\n")
        _write(root, "frontend/src/views/d.test.tsx", "test\n")
        _write(root, "frontend/src/node_modules/pkg/f.ts", "f = 1\n")
        _write(root, "frontend/src/dist/g.ts", "g = 1\n")
        _write(root, "frontend/src/.hidden.ts", "h = 1\n")
        _write(root, "core_cli/cli.py", "CLI = 1\n")
        _write(root, "core_cli/fixtures/f.py", "f = 1\n")

    def test_docs_and_code_collected_with_tags(self):
        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            found = build_index.collect_source_files(root)
        self.assertEqual(
            found,
            [
                ("docs/guide.md", "doc", "markdown"),
                ("AGENTS.md", "doc", "markdown"),
                ("backend/app.py", "code", "python"),
                ("deep-agent/top.py", "code", "python"),
                ("frontend/src/a.ts", "code", "typescript"),
                ("frontend/src/views/b.tsx", "code", "typescript"),
                ("core_cli/cli.py", "code", "python"),
            ],
        )

    def test_excluded_dirs_never_descended(self):
        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            found = build_index.collect_source_files(root)
        rels = [rel for rel, _, _ in found]
        for rel in rels:
            parts = rel.split(os.sep)
            for skipped in build_index.SKIP_DIRS:
                self.assertNotIn(skipped, parts, rel)
            self.assertNotIn(".test.", os.path.basename(rel), rel)

    def test_missing_bases_tolerated(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "docs/only.md", "# only\n")
            found = build_index.collect_source_files(root)
        self.assertEqual(
            found, [("docs/only.md", "doc", "markdown")])

    def test_real_tree_exclusions_hold(self):
        root = os.path.join(
            os.path.dirname(__file__), "..")
        found = build_index.collect_source_files(root)
        self.assertTrue(found)
        kinds = {kind for _, kind, _ in found}
        self.assertEqual(kinds, {"doc", "code"})
        for rel, kind, language in found:
            parts = rel.split(os.sep)
            for skipped in build_index.SKIP_DIRS:
                self.assertNotIn(skipped, parts, rel)
            self.assertNotIn(".test.", os.path.basename(rel), rel)
            if kind == "code":
                self.assertIn(language, ("python", "typescript"))
            else:
                self.assertEqual(language, "markdown")
        rels = [rel for rel, _, _ in found]
        self.assertIn("backend/api/routers/jobs.py", rels)
        self.assertIn("deep-agent/build_index.py", rels)
        self.assertIn("frontend/src/views/JobsView.tsx", rels)


class TestDocMetadata(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(
            build_index.doc_metadata(
                "backend/a.py", "code", "python", "arii/remote-server"),
            {"source": "backend/a.py", "repo": "arii/remote-server",
             "kind": "code", "language": "python"},
        )
        self.assertEqual(
            build_index.doc_metadata(
                "docs/g.md", "doc", "markdown", "arii/tech-dancer"),
            {"source": "docs/g.md", "repo": "arii/tech-dancer",
             "kind": "doc", "language": "markdown"},
        )


class _FakeDocument:
    def __init__(self, page_content, metadata):
        self.page_content = page_content
        self.metadata = metadata


class _FakeSplitter:
    def __init__(self, chunk_size, chunk_overlap):
        pass

    def split_documents(self, docs):
        return list(docs)


class _FakeChroma:
    """Stand-in for langchain_chroma.Chroma (clear-path tests).

    delete_error: exception delete_collection() raises (None = ok).
    added: documents from_documents() received (None = never called).
    """

    delete_error = None
    added = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def delete_collection(self):
        if _FakeChroma.delete_error is not None:
            raise _FakeChroma.delete_error

    @classmethod
    def from_documents(
            cls, documents, embedding,
            persist_directory, collection_name):
        cls.added = list(documents)
        return cls()


_FAKE_MODULE_NAMES = (
    "langchain_chroma",
    "langchain_core",
    "langchain_core.documents",
    "langchain_text_splitters",
)


def _install_fake_langchain(testcase):
    """Install fake langchain modules; restore on testcase cleanup."""
    import types

    saved = {
        name: sys.modules.get(name) for name in _FAKE_MODULE_NAMES
    }
    chroma_mod = types.ModuleType("langchain_chroma")
    chroma_mod.Chroma = _FakeChroma
    core_mod = types.ModuleType("langchain_core")
    docs_mod = types.ModuleType("langchain_core.documents")
    docs_mod.Document = _FakeDocument
    core_mod.documents = docs_mod
    split_mod = types.ModuleType("langchain_text_splitters")
    split_mod.RecursiveCharacterTextSplitter = _FakeSplitter
    sys.modules["langchain_chroma"] = chroma_mod
    sys.modules["langchain_core"] = core_mod
    sys.modules["langchain_core.documents"] = docs_mod
    sys.modules["langchain_text_splitters"] = split_mod

    def restore():
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    testcase.addCleanup(restore)


class TestClearFailure(unittest.TestCase):
    """A failed clear fails loud; only the fresh-index signal passes.

    Stdlib-only (#542): fake langchain modules stand in for the venv
    so build() runs on system python. Triggering a real locked
    chroma_db mid-run is nondeterministic; the raising fake is the
    deterministic repro.
    """

    def setUp(self):
        _install_fake_langchain(self)
        _FakeChroma.delete_error = None
        _FakeChroma.added = None

    def _root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _write(tmp.name, "docs/guide.md", "# guide\n")
        return tmp.name

    def _build(self):
        # Explicit single-repo list: hermetic (repos=None would
        # auto-discover the real repos dir).
        root = self._root()
        return build_index.build(
            root, "/nonexistent-db", "s5-docs", object(),
            repos=[("remote-server", "arii/remote-server", root)])

    def test_lock_error_fails_loud_without_indexing(self):
        import sqlite3

        _FakeChroma.delete_error = sqlite3.OperationalError(
            "database is locked")
        with self.assertRaisesRegex(
                RuntimeError, "failed to clear collection"):
            self._build()
        self.assertIsNone(_FakeChroma.added)

    def test_permission_error_fails_loud_without_indexing(self):
        _FakeChroma.delete_error = PermissionError("denied")
        with self.assertRaisesRegex(
                RuntimeError, "refusing to index"):
            self._build()
        self.assertIsNone(_FakeChroma.added)

    def test_missing_collection_signal_still_passes(self):
        class NotFoundError(Exception):
            pass

        _FakeChroma.delete_error = NotFoundError(
            "Collection [s5-docs] does not exist")
        n_docs, counts = self._build()
        self.assertEqual(n_docs, 1)
        self.assertEqual(
            counts, {"doc": 1, "code": 0, "issue": 0, "pr": 0})
        self.assertIsNotNone(_FakeChroma.added)
        self.assertEqual(len(_FakeChroma.added), 1)

    def test_value_error_signal_still_passes(self):
        _FakeChroma.delete_error = ValueError(
            "Collection s5-docs does not exist.")
        n_docs, _ = self._build()
        self.assertEqual(n_docs, 1)
        self.assertIsNotNone(_FakeChroma.added)

    def test_main_returns_nonzero_with_stderr(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        import retrieval

        # Hermetic: main() would otherwise run the real gh pass.
        os.environ["S5_GH_INDEX"] = "0"
        self.addCleanup(os.environ.pop, "S5_GH_INDEX", None)
        _FakeChroma.delete_error = OSError("lock stuck")
        real_embeddings = retrieval.get_embeddings
        retrieval.get_embeddings = lambda: object()
        self.addCleanup(
            setattr, retrieval, "get_embeddings", real_embeddings)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = build_index.main()
        self.assertEqual(rc, 1)
        self.assertIn("failed to clear collection", err.getvalue())
        self.assertIsNone(_FakeChroma.added)


def _gh_item(number, kind="issue", state="OPEN",
             updated="2026-11-04T16:00:00Z", title="title",
             body="body", labels=(), url=None):
    base = "issues" if kind == "issue" else "pull"
    return {
        "number": number,
        "title": f"{title} {number}",
        "body": f"{body} {number}",
        "state": state,
        "labels": [{"name": name} for name in labels],
        "updatedAt": updated,
        "url": url or f"https://github.com/arii/remote-server/"
                       f"{base}/{number}",
    }


class _StubProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _StubRunner:
    """Subprocess-like stub: canned gh stdout, records argv + kwargs."""

    def __init__(self, stdout="[]", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return _StubProc(
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr)


class TestFetchGhItems(unittest.TestCase):
    """gh argv shape + MRU sort + cap. No network (stub runner)."""

    def test_issue_argv_scopes_repo_state_all_and_fields(self):
        import json

        runner = _StubRunner(stdout=json.dumps([]))
        build_index.fetch_gh_items(
            "issue", "arii/remote-server", 500, runner=runner)
        self.assertEqual(len(runner.calls), 1)
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv[:4], ["gh", "issue", "list", "-R"])
        self.assertIn("arii/remote-server", argv)
        self.assertEqual(argv[argv.index("--state") + 1], "all")
        self.assertEqual(argv[argv.index("--limit") + 1], "500")
        self.assertEqual(
            argv[argv.index("--json") + 1], build_index.GH_FIELDS)
        self.assertEqual(kwargs.get("timeout"), build_index.GH_TIMEOUT)

    def test_pr_uses_pr_subcommand(self):
        import json

        runner = _StubRunner(stdout=json.dumps([]))
        build_index.fetch_gh_items(
            "pr", "arii/remote-server", 10, runner=runner)
        argv, _ = runner.calls[0]
        self.assertEqual(argv[:3], ["gh", "pr", "list"])
        self.assertEqual(argv[argv.index("--limit") + 1], "10")

    def test_sorts_mru_first_and_caps(self):
        import json

        items = [
            _gh_item(1, updated="2026-11-01T00:00:00Z"),
            _gh_item(2, updated="2026-11-04T00:00:00Z"),
            _gh_item(3, updated="2026-11-02T00:00:00Z"),
        ]
        runner = _StubRunner(stdout=json.dumps(items))
        got = build_index.fetch_gh_items(
            "issue", "arii/remote-server", 2, runner=runner)
        self.assertEqual(
            [item["number"] for item in got], [2, 3])

    def test_missing_updated_at_sorts_last(self):
        import json

        items = [
            {"number": 9, "title": "t", "body": "b", "state": "OPEN",
             "labels": [], "url": "u"},
            _gh_item(2, updated="2026-11-04T00:00:00Z"),
        ]
        runner = _StubRunner(stdout=json.dumps(items))
        got = build_index.fetch_gh_items(
            "issue", "arii/remote-server", 5, runner=runner)
        self.assertEqual(
            [item["number"] for item in got], [2, 9])

    def test_bad_kind_raises_without_spawning(self):
        runner = _StubRunner()
        with self.assertRaises(ValueError):
            build_index.fetch_gh_items(
                "comment", "arii/remote-server", 5, runner=runner)
        self.assertEqual(runner.calls, [])

    def test_zero_limit_spawns_nothing(self):
        runner = _StubRunner()
        self.assertEqual(
            build_index.fetch_gh_items(
                "issue", "arii/remote-server", 0, runner=runner),
            [])
        self.assertEqual(runner.calls, [])

    def test_gh_failure_raises_last_stderr_line(self):
        runner = _StubRunner(
            returncode=1, stderr="first\nlast line\n")
        with self.assertRaisesRegex(RuntimeError, "last line"):
            build_index.fetch_gh_items(
                "issue", "arii/remote-server", 5, runner=runner)

    def test_empty_stdout_raises(self):
        runner = _StubRunner(stdout="  \n")
        with self.assertRaisesRegex(RuntimeError, "empty output"):
            build_index.fetch_gh_items(
                "pr", "arii/remote-server", 5, runner=runner)


class TestGhMetadata(unittest.TestCase):
    def test_metadata_exact(self):
        item = _gh_item(
            826, state="OPEN", labels=("ready", "insight"))
        self.assertEqual(
            build_index.gh_metadata(item, "issue", "arii/remote-server"),
            {
                "source": "issue#826",
                "repo": "arii/remote-server",
                "kind": "issue",
                "language": "markdown",
                "number": 826,
                "state": "OPEN",
                "url": "https://github.com/arii/remote-server/issues/826",
                "labels": "ready,insight",
            },
        )

    def test_repo_carries_full_owner_name(self):
        item = _gh_item(377, kind="issue")
        meta = build_index.gh_metadata(item, "issue", "arii/portfolio")
        self.assertEqual(meta["repo"], "arii/portfolio")

    def test_pr_metadata_kind_and_source(self):
        item = _gh_item(830, kind="pr", state="MERGED")
        meta = build_index.gh_metadata(item, "pr", "arii/remote-server")
        self.assertEqual(meta["kind"], "pr")
        self.assertEqual(meta["source"], "pr#830")
        self.assertEqual(meta["number"], 830)
        self.assertEqual(meta["state"], "MERGED")
        self.assertTrue(meta["url"].endswith("/pull/830"))
        self.assertEqual(meta["labels"], "")

    def test_labels_tolerate_missing_and_nameless(self):
        self.assertEqual(build_index.gh_labels({"labels": None}), "")
        self.assertEqual(build_index.gh_labels({}), "")
        self.assertEqual(
            build_index.gh_labels(
                {"labels": [{"name": "a"}, {}, {"name": ""}]}),
            "a")

    def test_page_content_carries_header_and_body(self):
        item = _gh_item(826, labels=("ready", "insight"))
        text = build_index.gh_page_content(item, "issue")
        self.assertIn("issue #826: title 826", text)
        self.assertIn("State: OPEN", text)
        self.assertIn("Labels: ready,insight", text)
        self.assertIn("body 826", text)

    def test_page_content_tolerates_empty_body(self):
        item = _gh_item(1, title="t", body="")
        item["body"] = None
        item["title"] = None
        text = build_index.gh_page_content(item, "pr")
        self.assertIn("pr #1:", text)
        self.assertIn("State: OPEN", text)

    def test_collect_orders_issues_before_prs(self):
        calls = []

        def stub(kind, repo, cap):
            calls.append((kind, repo, cap))
            if kind == "issue":
                return [_gh_item(826)]
            return [_gh_item(830, kind="pr")]

        docs = build_index.collect_gh_documents(
            fetcher=stub, cap=7, repo="arii/remote-server")
        self.assertEqual(calls, [("issue", "arii/remote-server", 7),
                                 ("pr", "arii/remote-server", 7)])
        self.assertEqual(
            [meta["source"] for _, meta in docs],
            ["issue#826", "pr#830"])


class TestGhBuildPass(unittest.TestCase):
    """Stub-fetcher build(): metadata attached, cap passed through,
    failure degrades to file-only, rebuild idempotent."""

    def setUp(self):
        _install_fake_langchain(self)
        _FakeChroma.delete_error = None
        _FakeChroma.added = None

    def _root(self, with_file=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if with_file:
            _write(tmp.name, "docs/guide.md", "# guide\n")
        return tmp.name

    def _build(self, root, fetcher, cap=build_index.GH_CAP):
        # Explicit single-repo list: hermetic (repos=None would
        # auto-discover the real repos dir).
        return build_index.build(
            root, "/nonexistent-db", "s5-docs", object(),
            gh_fetcher=fetcher, gh_cap=cap,
            repos=[("remote-server", "arii/remote-server", root)])

    def test_appends_gh_chunks_with_metadata(self):
        def stub(kind, repo, cap):
            if kind == "issue":
                return [_gh_item(826, labels=("ready",)),
                        _gh_item(825, state="CLOSED")]
            return [_gh_item(830, kind="pr", state="MERGED")]

        n_docs, counts = self._build(self._root(), stub)
        self.assertEqual(n_docs, 4)
        self.assertEqual(
            counts, {"doc": 1, "code": 0, "issue": 2, "pr": 1})
        metas = [doc.metadata for doc in _FakeChroma.added]
        gh_metas = [m for m in metas if m["kind"] in ("issue", "pr")]
        self.assertEqual(len(gh_metas), 3)
        by_source = {m["source"]: m for m in gh_metas}
        self.assertEqual(by_source["issue#826"]["number"], 826)
        self.assertEqual(by_source["issue#826"]["state"], "OPEN")
        self.assertEqual(
            by_source["issue#826"]["url"],
            "https://github.com/arii/remote-server/issues/826")
        self.assertEqual(by_source["issue#826"]["labels"], "ready")
        self.assertEqual(by_source["issue#825"]["state"], "CLOSED")
        self.assertEqual(by_source["pr#830"]["kind"], "pr")
        for meta in gh_metas:
            self.assertEqual(meta["repo"], "arii/remote-server")

    def test_cap_passed_through_per_kind(self):
        seen = []

        def stub(kind, repo, cap):
            seen.append((kind, cap))
            return []

        self._build(self._root(), stub, cap=11)
        self.assertEqual(seen, [("issue", 11), ("pr", 11)])

    def test_cap_zero_never_calls_fetcher(self):
        def stub(kind, repo, cap):
            raise AssertionError("fetcher must not run")

        n_docs, counts = self._build(self._root(), stub, cap=0)
        self.assertEqual(n_docs, 1)
        self.assertEqual(counts["issue"], 0)
        self.assertEqual(counts["pr"], 0)

    def test_default_build_skips_gh(self):
        real = build_index.fetch_gh_items

        def boom(*args, **kwargs):
            raise AssertionError("gh must not run")

        build_index.fetch_gh_items = boom
        try:
            root = self._root()
            n_docs, counts = build_index.build(
                root, "/nonexistent-db", "s5-docs", object(),
                repos=[("remote-server", "arii/remote-server", root)])
        finally:
            build_index.fetch_gh_items = real
        self.assertEqual(n_docs, 1)
        self.assertEqual(counts["issue"], 0)

    def test_gh_failure_warns_and_continues_file_only(self):
        import io
        from contextlib import redirect_stderr

        def stub(kind, repo, cap):
            raise RuntimeError("gh: auth failed")

        err = io.StringIO()
        with redirect_stderr(err):
            n_docs, counts = self._build(self._root(), stub)
        self.assertEqual(n_docs, 1)
        self.assertEqual(
            counts, {"doc": 1, "code": 0, "issue": 0, "pr": 0})
        self.assertIn("GitHub pass skipped", err.getvalue())
        self.assertIsNotNone(_FakeChroma.added)
        self.assertEqual(len(_FakeChroma.added), 1)

    def test_rebuild_idempotent_with_gh(self):
        def stub(kind, repo, cap):
            if kind == "issue":
                return [_gh_item(826)]
            return [_gh_item(830, kind="pr")]

        root = self._root()
        self._build(root, stub)
        first = len(_FakeChroma.added)
        self.assertGreater(first, 0)
        self._build(root, stub)
        self.assertEqual(len(_FakeChroma.added), first)

    def test_gh_only_builds_without_files(self):
        def stub(kind, repo, cap):
            if kind == "issue":
                return [_gh_item(826)]
            return []

        n_docs, counts = self._build(
            self._root(with_file=False), stub)
        self.assertEqual(n_docs, 1)
        self.assertEqual(counts["issue"], 1)

    def test_no_files_no_gh_still_raises(self):
        def stub(kind, repo, cap):
            return []

        with self.assertRaisesRegex(
                RuntimeError, "no source documents matched"):
            self._build(self._root(with_file=False), stub)
        self.assertIsNone(_FakeChroma.added)

    def test_kind_renders_in_format_hits(self):
        from types import SimpleNamespace

        import retrieval

        meta = build_index.gh_metadata(
            _gh_item(826), "issue", "arii/remote-server")
        out = retrieval.format_hits(
            [SimpleNamespace(metadata=meta, page_content="decided X")])
        self.assertIn("[arii/remote-server/issue#826]", out)
        self.assertIn("decided X", out)


class TestParseGhRemote(unittest.TestCase):
    def test_url_shapes(self):
        cases = {
            "https://github.com/arii/portfolio": "arii/portfolio",
            "https://github.com/arii/portfolio.git": "arii/portfolio",
            "git@github.com:arii/boomtick.git": "arii/boomtick",
            "ssh://git@github.com/arii/hrm": "arii/hrm",
            "  https://github.com/arii/tech-dancer.git\n":
                "arii/tech-dancer",
        }
        for url, want in cases.items():
            self.assertEqual(
                build_index.parse_gh_remote(url), want, url)

    def test_rejects(self):
        for url in ("", None, "not-a-url",
                    "https://gitlab.com/arii/portfolio",
                    "https://github.com/arii",
                    "https://github.com/arii/portfolio/extra",
                    "https://github.com/arii/portfolio/"):
            self.assertIsNone(build_index.parse_gh_remote(url), url)


class _GitProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _git_run_stub(origins):
    """subprocess.run stub: canned origin per exact checkout path."""

    def run(argv, **kwargs):
        assert argv[-3:] == ["config", "--get", "remote.origin.url"]
        path = argv[argv.index("-C") + 1]
        if path in origins:
            return _GitProc(0, origins[path])
        return _GitProc(1, "")

    return run


class TestDiscoverRepos(unittest.TestCase):
    def _repos_dir(self, tmp):
        repos = os.path.join(tmp, "repos")
        os.makedirs(repos)
        return repos

    def _checkout(self, repos, name, git="dir"):
        path = os.path.join(repos, name)
        os.makedirs(path, exist_ok=True)
        if git == "dir":
            os.makedirs(os.path.join(path, ".git"), exist_ok=True)
        elif git == "file":
            with open(os.path.join(path, ".git"), "w") as f:
                f.write("gitdir: elsewhere\n")
        return path

    def test_stack_always_first_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = os.path.join(tmp, "stack")
            os.makedirs(stack)
            repos = self._repos_dir(tmp)
            got = build_index.discover_repos(
                stack, repos_dir=repos,
                run=_git_run_stub({}))
        self.assertEqual(
            got, [("remote-server", "arii/remote-server", stack)])

    def test_discovers_git_checkouts_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = os.path.join(tmp, "stack")
            os.makedirs(stack)
            repos = self._repos_dir(tmp)
            tech = self._checkout(repos, "tech-dancer", git="file")
            port = self._checkout(repos, "portfolio")
            self._checkout(repos, "remote-server")
            self._checkout(repos, "plain-dir", git=None)
            os.makedirs(os.path.join(repos, "bad name"))
            run = _git_run_stub({
                tech: "https://github.com/arii/tech-dancer.git\n",
                port: "git@github.com:arii/portfolio.git\n",
            })
            got = build_index.discover_repos(
                stack, repos_dir=repos, run=run)
        self.assertEqual(
            got,
            [("remote-server", "arii/remote-server", stack),
             ("portfolio", "arii/portfolio", port),
             ("tech-dancer", "arii/tech-dancer", tech)])

    def test_origin_fallback_and_unparseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = os.path.join(tmp, "stack")
            os.makedirs(stack)
            repos = self._repos_dir(tmp)
            failing = self._checkout(repos, "aaa")
            foreign = self._checkout(repos, "zzz")
            run = _git_run_stub({
                foreign: "https://gitlab.com/arii/zzz.git\n",
            })
            got = build_index.discover_repos(
                stack, repos_dir=repos, run=run)
        self.assertEqual(
            got,
            [("remote-server", "arii/remote-server", stack),
             ("aaa", "arii/aaa", failing),
             ("zzz", "arii/zzz", foreign)])

    def test_missing_repos_dir_yields_stack_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = os.path.join(tmp, "stack")
            os.makedirs(stack)
            got = build_index.discover_repos(
                stack,
                repos_dir=os.path.join(tmp, "no-such-dir"),
                run=_git_run_stub({}))
        self.assertEqual(
            got, [("remote-server", "arii/remote-server", stack)])

    def test_new_checkout_joins_with_zero_config(self):
        # Auto-include: a checkout cloned into the repos dir after
        # the last rebuild is discovered on the next call.
        with tempfile.TemporaryDirectory() as tmp:
            stack = os.path.join(tmp, "stack")
            os.makedirs(stack)
            repos = self._repos_dir(tmp)
            run = _git_run_stub({})
            before = build_index.discover_repos(
                stack, repos_dir=repos, run=run)
            self.assertEqual(len(before), 1)
            fresh = self._checkout(repos, "fresh-repo")
            after = build_index.discover_repos(
                stack, repos_dir=repos, run=run)
        self.assertEqual(
            after,
            [("remote-server", "arii/remote-server", stack),
             ("fresh-repo", "arii/fresh-repo", fresh)])

    def test_get_repos_dir_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("BUTTONS_REPOS_DIR")
            os.environ["BUTTONS_REPOS_DIR"] = tmp
            try:
                self.assertEqual(build_index.get_repos_dir(), tmp)
            finally:
                if old is None:
                    del os.environ["BUTTONS_REPOS_DIR"]
                else:
                    os.environ["BUTTONS_REPOS_DIR"] = old

    def test_get_repos_dir_missing_falls_through(self):
        from unittest import mock

        env = dict(os.environ)
        env.pop("BUTTONS_REPOS_DIR", None)
        with mock.patch.object(
                os.path, "isdir", return_value=False), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                build_index.get_repos_dir(), "/home/ari/repos")


class TestCollectGenericFiles(unittest.TestCase):
    def _fixture_root(self, root):
        _write(root, "README.md", "# readme\n")
        _write(root, "AGENTS.md", "# agents\n")
        _write(root, "docs/guide.md", "# guide\n")
        _write(root, "docs/nested/deep.md", "# deep\n")
        _write(root, "docs/tests/x.md", "skipped\n")
        _write(root, "src/app.tsx", "export const a = 1;\n")
        _write(root, "src/views/b.tsx", "export const b = 1;\n")
        _write(root, "src/c.test.ts", "test\n")
        _write(root, "lib/util.js", "export const u = 1;\n")
        _write(root, "main.py", "MAIN = 1\n")
        _write(root, "src/node_modules/pkg/f.ts", "f = 1\n")
        _write(root, "dist/g.js", "g = 1\n")
        _write(root, ".hidden.md", "h = 1\n")

    def test_docs_then_code_with_languages(self):
        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            found = build_index.collect_generic_files(root)
        self.assertEqual(
            found,
            [
                ("AGENTS.md", "doc", "markdown"),
                ("README.md", "doc", "markdown"),
                ("docs/guide.md", "doc", "markdown"),
                ("docs/nested/deep.md", "doc", "markdown"),
                ("lib/util.js", "code", "javascript"),
                ("main.py", "code", "python"),
                ("src/app.tsx", "code", "typescript"),
                ("src/views/b.tsx", "code", "typescript"),
            ],
        )

    def test_excluded_dirs_never_descended(self):
        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            found = build_index.collect_generic_files(root)
        rels = [rel for rel, _, _ in found]
        self.assertTrue(rels)
        for rel in rels:
            parts = rel.split(os.sep)
            for skipped in build_index.SKIP_DIRS:
                self.assertNotIn(skipped, parts, rel)
            self.assertNotIn(".test.", os.path.basename(rel), rel)

    def test_missing_docs_tolerated(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "only.py", "ONLY = 1\n")
            found = build_index.collect_generic_files(root)
        self.assertEqual(
            found, [("only.py", "code", "python")])

    def test_empty_tree_yields_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(
                build_index.collect_generic_files(root), [])


class TestMultiRepoBuild(unittest.TestCase):
    """Explicit repos list: namespaced metadata, per-repo cap,
    per-repo warn-and-continue. Stdlib-only (fake langchain)."""

    def setUp(self):
        _install_fake_langchain(self)
        _FakeChroma.delete_error = None
        _FakeChroma.added = None

    def _stack(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _write(tmp.name, "docs/guide.md", "# guide\n")
        return tmp.name

    def _portfolio(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _write(tmp.name, "README.md", "# portfolio\n")
        _write(tmp.name, "src/app.tsx", "export const a = 1;\n")
        return tmp.name

    def _repos(self, stack, portfolio):
        return [("remote-server", "arii/remote-server", stack),
                ("portfolio", "arii/portfolio", portfolio)]

    def _build(self, repos, fetcher, cap=build_index.GH_CAP):
        return build_index.build(
            repos[0][2], "/nonexistent-db", "s5-docs", object(),
            gh_fetcher=fetcher, gh_cap=cap, repos=repos)

    def test_files_namespaced_by_repo(self):
        stack = self._stack()
        portfolio = self._portfolio()
        n_docs, counts = self._build(
            self._repos(stack, portfolio), lambda *a: [])
        self.assertEqual(n_docs, 3)
        self.assertEqual(
            counts, {"doc": 2, "code": 1, "issue": 0, "pr": 0})
        by_source = {
            doc.metadata["source"]: doc.metadata["repo"]
            for doc in _FakeChroma.added
        }
        self.assertEqual(
            by_source,
            {"docs/guide.md": "arii/remote-server",
             "README.md": "arii/portfolio",
             "src/app.tsx": "arii/portfolio"})

    def test_gh_namespaced_and_capped_per_repo(self):
        seen = []

        def stub(kind, repo, cap):
            seen.append((kind, repo, cap))
            if repo == "arii/remote-server":
                return [_gh_item(826)] if kind == "issue" else []
            if kind == "issue":
                return [_gh_item(377)]
            return [_gh_item(400, kind="pr")]

        stack = self._stack()
        portfolio = self._portfolio()
        n_docs, counts = self._build(
            self._repos(stack, portfolio), stub, cap=11)
        self.assertEqual(
            seen,
            [("issue", "arii/remote-server", 11),
             ("pr", "arii/remote-server", 11),
             ("issue", "arii/portfolio", 11),
             ("pr", "arii/portfolio", 11)])
        self.assertEqual(n_docs, 6)
        self.assertEqual(counts["issue"], 2)
        self.assertEqual(counts["pr"], 1)
        by_source = {
            doc.metadata["source"]: doc.metadata["repo"]
            for doc in _FakeChroma.added
            if doc.metadata["kind"] in ("issue", "pr")
        }
        self.assertEqual(
            by_source,
            {"issue#826": "arii/remote-server",
             "issue#377": "arii/portfolio",
             "pr#400": "arii/portfolio"})

    def test_one_repo_gh_failure_degrades_file_only(self):
        import io
        from contextlib import redirect_stderr

        def stub(kind, repo, cap):
            if repo == "arii/portfolio":
                raise RuntimeError("gh: auth failed")
            return [_gh_item(826)] if kind == "issue" else []

        stack = self._stack()
        portfolio = self._portfolio()
        err = io.StringIO()
        with redirect_stderr(err):
            n_docs, counts = self._build(
                self._repos(stack, portfolio), stub)
        self.assertEqual(n_docs, 4)
        self.assertEqual(counts["issue"], 1)
        self.assertIn(
            "arii/portfolio: GitHub pass skipped", err.getvalue())
        by_source = {
            (doc.metadata["source"], doc.metadata["kind"])
            for doc in _FakeChroma.added
        }
        # Portfolio files survive; only its gh items are missing.
        self.assertIn(("README.md", "doc"), by_source)
        self.assertIn(("src/app.tsx", "code"), by_source)
        self.assertIn(("issue#826", "issue"), by_source)

    def test_file_pass_failure_warns_and_continues(self):
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        stack = self._stack()
        portfolio = self._portfolio()
        err = io.StringIO()
        with mock.patch.object(
                build_index, "collect_generic_files",
                side_effect=RuntimeError("boom")), \
                redirect_stderr(err):
            n_docs, counts = self._build(
                self._repos(stack, portfolio), lambda *a: [])
        self.assertEqual(n_docs, 1)
        self.assertEqual(counts["doc"], 1)
        self.assertIn("arii/portfolio: file pass skipped",
                      err.getvalue())

    def test_repos_none_auto_discovers(self):
        from unittest import mock

        stack = self._stack()
        with mock.patch.object(
                build_index, "discover_repos",
                return_value=[("remote-server", "arii/remote-server",
                               stack)]) as discover:
            n_docs, _ = build_index.build(
                stack, "/nonexistent-db", "s5-docs", object())
        discover.assert_called_once_with(stack)
        self.assertEqual(n_docs, 1)

    def test_empty_repos_still_raises(self):
        with self.assertRaisesRegex(
                RuntimeError, "no source documents matched"):
            build_index.build(
                "/nonexistent-root", "/nonexistent-db", "s5-docs",
                object(), repos=[])
        self.assertIsNone(_FakeChroma.added)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
@unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
class TestBuildSmoke(unittest.TestCase):
    COLLECTION = "s5-docs-smoke-668"

    def _fixture_root(self, root):
        _write(root, "docs/guide.md",
               "# Guide\n\nRun the server with docker compose up.\n")
        _write(root, "backend/other.py",
               '"""Unrelated helper."""\n\nOTHER = 1\n')
        _write(
            root,
            "backend/compare_endpoint.py",
            '"""Unified diff of two runs outputs."""\n'
            "\n"
            "def compare_runs(a, b):\n"
            '    """Return the unified diff of two run outputs."""\n'
            "    return (a, b)\n",
        )

    def test_code_query_returns_file_path(self):
        import retrieval
        from langchain_chroma import Chroma

        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            with tempfile.TemporaryDirectory() as db_dir:
                embeddings = retrieval.get_embeddings()
                n_docs, counts = build_index.build(
                    root, db_dir, self.COLLECTION, embeddings,
                    repos=[("remote-server",
                            "arii/remote-server", root)])
                self.assertEqual(n_docs, 3)
                self.assertEqual(counts["code"], 2)
                self.assertEqual(counts["doc"], 1)
                store = Chroma(
                    persist_directory=db_dir,
                    embedding_function=embeddings,
                    collection_name=self.COLLECTION,
                )
                hits = store.similarity_search(
                    "unified diff of two run outputs", k=3)
                sources = [
                    h.metadata.get("source") for h in hits]
                self.assertIn(
                    "backend/compare_endpoint.py", sources)
                code_hits = [
                    h for h in hits
                    if h.metadata.get("source")
                    == "backend/compare_endpoint.py"
                ]
                self.assertEqual(
                    code_hits[0].metadata.get("kind"), "code")
                self.assertEqual(
                    code_hits[0].metadata.get("language"), "python")

    def test_rebuild_twice_no_duplicates(self):
        import retrieval
        from langchain_chroma import Chroma

        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            with tempfile.TemporaryDirectory() as db_dir:
                embeddings = retrieval.get_embeddings()
                only = [("remote-server", "arii/remote-server", root)]
                build_index.build(
                    root, db_dir, self.COLLECTION, embeddings,
                    repos=only)
                store = Chroma(
                    persist_directory=db_dir,
                    embedding_function=embeddings,
                    collection_name=self.COLLECTION,
                )
                first = len(store.get()["ids"])
                self.assertGreater(first, 0)
                build_index.build(
                    root, db_dir, self.COLLECTION, embeddings,
                    repos=only)
                store = Chroma(
                    persist_directory=db_dir,
                    embedding_function=embeddings,
                    collection_name=self.COLLECTION,
                )
                self.assertEqual(len(store.get()["ids"]), first)


if __name__ == "__main__":
    unittest.main()

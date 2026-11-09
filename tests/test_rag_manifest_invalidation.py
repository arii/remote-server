"""Regression tests: stale-manifest query triggers reindex (#1214).

Editing a skill/doc then querying must serve fresh chunks without a
manual reindex; a failed reindex must raise fail-closed (never serve
stale). The SkillsRegistry read path must auto-refresh on edits.

Run: PYTHONPATH=./deep-agent python3 -m unittest tests.test_rag_manifest_invalidation
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import retrieval
from skills_registry import SkillsRegistry


def make_repo(root):
    """Fake checkout with one skill doc and one docs page."""
    skills = os.path.join(root, "deep-agent", "skills")
    docs = os.path.join(root, "docs")
    os.makedirs(skills)
    os.makedirs(docs)
    skill = os.path.join(skills, "demo.md")
    page = os.path.join(docs, "guide.md")
    with open(skill, "w", encoding="utf-8") as handle:
        handle.write("# Demo v1\n")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write("# Guide v1\n")
    return skill, page


def seed_current_manifest(root):
    retrieval.write_manifest(root, retrieval.source_manifest(root))


def query_payload(hits):
    return subprocess.CompletedProcess(
        args=["td"], returncode=0, stdout=json.dumps({"hits": hits}), stderr=""
    )


FRESH_HITS = [
    {
        "content": "fresh v2 content",
        "filepath": "docs/guide.md",
        "start_line": 1,
        "score": 9.0,
    }
]


class TestManifestHelpers(unittest.TestCase):
    def test_sources_cover_skills_and_docs_sorted(self):
        with tempfile.TemporaryDirectory() as root:
            make_repo(root)
            rels = retrieval.manifest_sources(root)
        self.assertEqual(
            rels,
            [
                os.path.join("deep-agent", "skills", "demo.md"),
                os.path.join("docs", "guide.md"),
            ],
        )

    def test_missing_dirs_yield_empty_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(retrieval.manifest_sources(root), [])
            self.assertEqual(retrieval.source_manifest(root), {})
            self.assertIsNone(retrieval.read_manifest(root))

    def test_corrupt_manifest_reads_as_missing(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, retrieval.RAG_INDEX_DIRNAME))
            with open(retrieval.manifest_path(root), "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertIsNone(retrieval.read_manifest(root))

    def test_reindex_repo_success_argv(self):
        with tempfile.TemporaryDirectory() as root:
            proc = subprocess.CompletedProcess(
                args=["td"], returncode=0, stdout='{"ok": true}', stderr=""
            )
            with mock.patch("subprocess.run", return_value=proc) as run:
                self.assertTrue(retrieval.reindex_repo(root))
        argv = run.call_args[0][0]
        self.assertEqual(
            argv, ["td", "rag", "index", "--repo-root", str(root), "--json"]
        )

    def test_reindex_repo_raises_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            bad = subprocess.CompletedProcess(
                args=["td"], returncode=1, stdout="", stderr="boom"
            )
            with mock.patch("subprocess.run", return_value=bad):
                with self.assertRaises(RuntimeError):
                    retrieval.reindex_repo(root)
            with mock.patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("td", 300),
            ):
                with self.assertRaises(RuntimeError):
                    retrieval.reindex_repo(root)
            with mock.patch("subprocess.run", side_effect=OSError("no td binary")):
                with self.assertRaises(RuntimeError):
                    retrieval.reindex_repo(root)


class TestEnsureFreshIndex(unittest.TestCase):
    def test_fresh_manifest_skips_reindex(self):
        with tempfile.TemporaryDirectory() as root:
            make_repo(root)
            seed_current_manifest(root)
            with mock.patch.object(
                retrieval, "reindex_repo", return_value=True
            ) as reindex:
                self.assertFalse(retrieval.ensure_fresh_index(root))
            reindex.assert_not_called()

    def test_stale_manifest_reindexes_and_updates(self):
        with tempfile.TemporaryDirectory() as root:
            skill, _page = make_repo(root)
            seed_current_manifest(root)
            with open(skill, "w", encoding="utf-8") as handle:
                handle.write("# Demo v2 edited\n")
            with mock.patch.object(
                retrieval, "reindex_repo", return_value=True
            ) as reindex:
                self.assertTrue(retrieval.ensure_fresh_index(root))
            reindex.assert_called_once()
            self.assertEqual(
                retrieval.read_manifest(root), retrieval.source_manifest(root)
            )

    def test_missing_manifest_counts_as_stale(self):
        with tempfile.TemporaryDirectory() as root:
            make_repo(root)
            with mock.patch.object(
                retrieval, "reindex_repo", return_value=True
            ) as reindex:
                self.assertTrue(retrieval.ensure_fresh_index(root))
            reindex.assert_called_once()

    def test_failed_reindex_raises_and_keeps_stale(self):
        with tempfile.TemporaryDirectory() as root:
            skill, _page = make_repo(root)
            seed_current_manifest(root)
            stale = retrieval.read_manifest(root)
            with open(skill, "w", encoding="utf-8") as handle:
                handle.write("# Demo v2 edited\n")
            with mock.patch.object(
                retrieval,
                "reindex_repo",
                side_effect=RuntimeError("index boom"),
            ):
                with self.assertRaises(RuntimeError):
                    retrieval.ensure_fresh_index(root)
            # Manifest still records the old content: the next query
            # retries the reindex instead of trusting a stale index.
            self.assertEqual(retrieval.read_manifest(root), stale)
            self.assertNotEqual(
                retrieval.read_manifest(root), retrieval.source_manifest(root)
            )


class TestQueryTdRepoFreshness(unittest.TestCase):
    def test_stale_query_triggers_reindex_then_serves_fresh(self):
        with tempfile.TemporaryDirectory() as root:
            _skill, page = make_repo(root)
            seed_current_manifest(root)
            with open(page, "w", encoding="utf-8") as handle:
                handle.write("# Guide v2 edited\n")
            with mock.patch.object(
                retrieval, "reindex_repo", return_value=True
            ) as reindex:
                with mock.patch(
                    "subprocess.run",
                    return_value=query_payload(FRESH_HITS),
                ) as run:
                    hits = retrieval.query_td_repo("guide", root)
            reindex.assert_called_once()
            self.assertEqual(hits, FRESH_HITS)
            argv = run.call_args[0][0]
            self.assertIn("query", argv)
            self.assertNotIn("index", argv)
            self.assertEqual(
                retrieval.read_manifest(root), retrieval.source_manifest(root)
            )

    def test_fresh_query_skips_reindex(self):
        with tempfile.TemporaryDirectory() as root:
            make_repo(root)
            seed_current_manifest(root)
            with mock.patch.object(
                retrieval, "reindex_repo", return_value=True
            ) as reindex:
                with mock.patch(
                    "subprocess.run", return_value=query_payload(FRESH_HITS)
                ):
                    hits = retrieval.query_td_repo("guide", root)
            reindex.assert_not_called()
            self.assertEqual(hits, FRESH_HITS)

    def test_reindex_failure_raises_before_query(self):
        # Fail-closed: no silent stale serve; the td query is never
        # even dispatched when the freshness reindex fails.
        with tempfile.TemporaryDirectory() as root:
            _skill, page = make_repo(root)
            seed_current_manifest(root)
            with open(page, "w", encoding="utf-8") as handle:
                handle.write("# Guide v2 edited\n")
            with mock.patch.object(
                retrieval,
                "reindex_repo",
                side_effect=RuntimeError("index boom"),
            ):
                with mock.patch(
                    "subprocess.run",
                    return_value=query_payload(FRESH_HITS),
                ) as run:
                    with self.assertRaises(RuntimeError):
                        retrieval.query_td_repo("guide", root)
            run.assert_not_called()


def write_skill(tmp, name, body):
    path = os.path.join(tmp, name + ".md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "---\nname: %s\ndescription: %s skill.\n"
            "trigger_nodes:\n  - node_a\n---\n%s" % (name, name, body)
        )
    return path


class TestRegistryAutoRefresh(unittest.TestCase):
    def test_new_skill_visible_without_manual_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, "one", "# One\n")
            reg = SkillsRegistry(skills_dir=Path(tmp))
            self.assertEqual(reg.skill_names(), ["one"])
            write_skill(tmp, "two", "# Two\n")
            self.assertEqual(reg.skill_names(), ["one", "two"])
            self.assertIn("Two", reg.load_skill("two"))

    def test_edited_body_served_without_manual_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, "one", "# One v1\n")
            reg = SkillsRegistry(skills_dir=Path(tmp))
            self.assertIn("v1", reg.load_skill("one"))
            write_skill(tmp, "one", "# One v2 edited\n")
            self.assertIn("v2 edited", reg.load_skill("one"))

    def test_refresh_if_stale_true_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, "one", "# One\n")
            reg = SkillsRegistry(skills_dir=Path(tmp))
            self.assertFalse(reg.refresh_if_stale())
            write_skill(tmp, "two", "# Two\n")
            self.assertTrue(reg.refresh_if_stale())
            self.assertFalse(reg.refresh_if_stale())


if __name__ == "__main__":
    unittest.main()

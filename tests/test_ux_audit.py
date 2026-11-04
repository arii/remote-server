"""Dispatched UX audit agent tests (#850). Hermetic: stub fetches,
no network, no gh, no live model (stub model_factory instead).
Run: python3 tests/test_ux_audit.py"""
import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                               "deep-agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ux_audit


HOME = """<html lang="en"><head><title>Home</title>
<meta name="viewport" content="width=device-width">
<meta name="description" content="home"></head>
<body><nav><a href="/">Home</a><a href="/about/">About</a></nav>
<main><h1>Home</h1><img src="/i.png" alt="logo">
<form><label for="q">Search</label><input id="q" name="q"></form>
</main></body></html>"""

ABOUT = """<html><head><title>About</title></head>
<body><nav><a href="/">Home</a><a href="/about/">About</a>
<a href="/extra/">Extra</a></nav>
<h2>About</h2><img src="/team.png">
<a href="#">placeholder</a><a href="/missing">gone</a>
</body></html>"""


def stub_fetch_factory(pages):
    def fetch(url, timeout=20, max_bytes=1000):
        return pages.get(url, "FETCH_ERROR: 404")
    return fetch


class SiteRegistryTest(unittest.TestCase):
    def test_both_sites_registered(self):
        # Findings file to the site sources (#886): github.io is
        # build output deployed from portfolio, boomtick.blog is
        # served from tech-dancer.
        self.assertEqual(
            ux_audit.site_config("arii.github.io")["repo"],
            "arii/portfolio")
        self.assertEqual(
            ux_audit.site_config("boomtick.blog")["repo"],
            "arii/tech-dancer")

    def test_unknown_site_fails_closed(self):
        with self.assertRaises(ValueError):
            ux_audit.site_config("evil.example")
        with self.assertRaises(ValueError):
            ux_audit.site_config("")


class ExtractLinksTest(unittest.TestCase):
    def test_anchors_resolved_assets_skipped(self):
        links = ux_audit.extract_links(
            '<a href="/about/">a</a>'
            '<a href="https://x.example/">x</a>'
            '<img src="/i.png"><a href="/f.css">c</a>'
            '<a href="mailto:a@b.c">m</a>',
            "https://arii.github.io/")
        self.assertIn("https://arii.github.io/about/", links)
        self.assertIn("https://x.example/", links)
        self.assertNotIn("https://arii.github.io/i.png", links)
        self.assertFalse(any(l.endswith(".css") for l in links))
        self.assertFalse(any(l.startswith("mailto") for l in links))

    def test_hostile_markup_never_raises(self):
        self.assertEqual(ux_audit.extract_links("<a href", "/"), [])
        self.assertEqual(ux_audit.extract_links(None, "/"), [])

    def test_non_page_extensions_excluded(self):
        links = ux_audit.extract_links(
            '<a href="/sitemap.xml">s</a><a href="/feed.atom">f</a>'
            '<a href="/robots.txt">r</a><a href="/about/">a</a>',
            "https://boomtick.blog/")
        self.assertEqual(links, ["https://boomtick.blog/about/"])

    def test_scope_is_host_and_prefix(self):
        cfg = ux_audit.site_config("boomtick.blog")
        self.assertTrue(ux_audit.in_scope(
            "https://boomtick.blog/a?b=c", cfg))
        self.assertFalse(ux_audit.in_scope(
            "https://evil.com/", cfg))
        self.assertFalse(ux_audit.in_scope(
            "http://boomtick.blog.evil.com/", cfg))
        self.assertFalse(ux_audit.in_scope("not a url", cfg))


class CrawlTest(unittest.TestCase):
    def test_bfs_within_scope_dead_deduped(self):
        pages = {
            "https://arii.github.io/": HOME,
            "https://arii.github.io/about/": ABOUT,
        }
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=10,
            fetch=stub_fetch_factory(pages))
        routes = sorted(p["route"] for p in crawled["pages"])
        self.assertEqual(routes, ["/", "/about/", "/extra/",
                                 "/missing"])
        dead_urls = [d["url"] for d in crawled["dead"]]
        # Each dead URL appears exactly once (no crawl+sweep dupes).
        self.assertEqual(sorted(dead_urls), sorted(set(dead_urls)))
        self.assertIn("https://arii.github.io/missing", dead_urls)
        # Assets are never crawled as pages.
        self.assertFalse(any(p["route"].endswith(".png")
                             for p in crawled["pages"]))

    def test_max_pages_bounds_crawl_and_probes(self):
        pages = {"https://arii.github.io/":
                 "<a href=\"/a\">a</a><a href=\"/b\">b</a>"}
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=1,
            fetch=stub_fetch_factory(pages))
        self.assertEqual(len(crawled["pages"]), 1)
        self.assertLessEqual(len(crawled["dead"]), 1)

    def test_start_failure_yields_honest_page(self):
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=5,
            fetch=stub_fetch_factory({}))
        self.assertEqual(len(crawled["pages"]), 1)
        self.assertTrue(crawled["pages"][0]["error"])

    def test_sitemap_urls_parsed_and_filtered(self):
        xml = ('<?xml version="1.0"?><urlset>'
               '<url><loc>https://s/a/</loc></url>'
               '<url><loc>/b</loc></url>'
               '<url><loc>https://s/i.png</loc></url>'
               '<url><loc>https://s/nested.xml</loc></url>'
               '</urlset>')
        self.assertEqual(
            ux_audit.sitemap_urls(xml, "https://s/sitemap.xml"),
            ["https://s/a/", "https://s/b"])
        self.assertEqual(ux_audit.sitemap_urls("nope", "https://s/"),
                         [])
        self.assertEqual(ux_audit.sitemap_urls(None, "https://s/"),
                         [])

    def test_crawl_seeds_from_sitemap(self):
        pages = {
            "https://arii.github.io/":
                "<html><head><title>t</title></head><body></body"
                "></html>",
            "https://arii.github.io/sitemap.xml":
                "<urlset><url><loc>https://arii.github.io/seeded/"
                "</loc></url></urlset>",
            "https://arii.github.io/seeded/":
                "<html><head><title>s</title></head><body></body"
                "></html>",
        }
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=5,
            fetch=stub_fetch_factory(pages))
        routes = sorted(p["route"] for p in crawled["pages"])
        self.assertEqual(routes, ["/", "/seeded/"])

    def test_missing_sitemap_degrades_to_anchors(self):
        pages = {"https://arii.github.io/": HOME}
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=5,
            fetch=stub_fetch_factory(pages))
        routes = sorted(p["route"] for p in crawled["pages"])
        self.assertIn("/", routes)

    def test_xml_body_skipped_not_audited(self):
        pages = {
            "https://arii.github.io/":
                '<html><head><title>t</title></head><body>'
                '<a href="/feed">f</a></body></html>',
            "https://arii.github.io/feed":
                '<?xml version="1.0"?><rss></rss>',
        }
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=5,
            fetch=stub_fetch_factory(pages))
        report = ux_audit.build_report("arii.github.io", crawled)
        self.assertEqual(report["page_count"], 1)
        self.assertIn("/feed", report["routes"])
        self.assertFalse(any(f["route"] == "/feed"
                             for f in report["findings"]))

    def test_bad_args_rejected(self):
        with self.assertRaises(ValueError):
            ux_audit.crawl("nope", fetch=stub_fetch_factory({}))
        with self.assertRaises(ValueError):
            ux_audit.crawl("arii.github.io", max_pages=0,
                           fetch=stub_fetch_factory({}))
        with self.assertRaises(ValueError):
            ux_audit.crawl("arii.github.io", max_pages=999,
                           fetch=stub_fetch_factory({}))


class AuditPageTest(unittest.TestCase):
    def test_clean_page_few_findings(self):
        findings, facts = ux_audit.audit_page("/", HOME)
        self.assertIsNotNone(facts)
        titles = [f["title"] for f in findings]
        self.assertNotIn("[P1] missing viewport meta tag", titles)
        self.assertFalse(any("alt text" in t for t in titles))

    def test_slop_page_flagged(self):
        findings, facts = ux_audit.audit_page("/about/", ABOUT)
        titles = [f["title"] for f in findings]
        self.assertIn("[P1] missing viewport meta tag", titles)
        self.assertIn("[P2] <html> has no lang attribute", titles)
        self.assertIn("[P1] image without alt text", titles)
        self.assertIn("[P2] placeholder links (href='#')", titles)
        self.assertIn("[P2] no h1 on page", titles)
        self.assertIn("[P2] no <main> landmark", titles)
        for item in findings:
            self.assertIn(item["severity"],
                          ("critical", "high", "medium", "low",
                           "info"))
            self.assertIn("category", item)

    def test_unlabeled_inputs_and_dupes(self):
        html = ('<html lang="en"><head><title>t</title>'
                '<meta name="viewport" content="w"></head><body>'
                '<main><h1>t</h1>'
                '<form><input id="a"><input id="a"></form>'
                '</main></body></html>')
        findings, _ = ux_audit.audit_page("/", html)
        titles = [f["title"] for f in findings]
        self.assertIn("[P1] form inputs without labels", titles)
        self.assertIn("[P2] duplicate element ids", titles)

    def test_missing_title_is_p1(self):
        findings, _ = ux_audit.audit_page(
            "/", "<html><body><h1>x</h1></body></html>")
        self.assertIn("[P1] missing page title",
                      [f["title"] for f in findings])


class CrossPageTest(unittest.TestCase):
    def test_nav_variants_and_dead_links(self):
        entries = [("/", ux_audit.page_facts(HOME)),
                   ("/about/", ux_audit.page_facts(ABOUT))]
        dead = [{"url": "https://s/m", "from_route": "/",
                 "reason": "FETCH_ERROR: 404"}]
        findings, stats = ux_audit.synthesize_cross_page(entries,
                                                         dead)
        titles = [f["title"] for f in findings]
        self.assertIn("[P2] inconsistent navigation across pages",
                      titles)
        self.assertIn("[P1] dead in-scope link", titles)
        self.assertEqual(stats["nav_variants"], 2)
        self.assertEqual(stats["dead"], 1)

    def test_consistent_nav_is_quiet(self):
        entries = [("/", ux_audit.page_facts(HOME)),
                   ("/2", ux_audit.page_facts(HOME))]
        findings, stats = ux_audit.synthesize_cross_page(entries,
                                                         [])
        self.assertEqual(stats["nav_variants"], 1)
        self.assertFalse(any("navigation" in f["title"]
                             for f in findings))


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.entries = [("/", ux_audit.page_facts(HOME)),
                        ("/about/", ux_audit.page_facts(ABOUT))]
        self.cross = {"nav_variants": 2, "dead": 1,
                      "placeholder_routes": 1}

    def test_heuristics_scored_with_basis_na_where_dynamic(self):
        scores = ux_audit.score_heuristics(self.entries, self.cross)
        self.assertEqual(len(scores), 10)
        for num, entry in scores.items():
            self.assertIn("basis", entry)
            if num in (1, 8, 9):
                self.assertIsNone(entry["score"])
            else:
                self.assertIn(entry["score"], (0, 1, 2, 3, 4))
        # Placeholder links + nav drift cap H4 at 2.
        self.assertEqual(scores[4]["score"], 2)

    def test_heuristic_total_renormalizes(self):
        scores = ux_audit.score_heuristics(self.entries, self.cross)
        total, maximum, band = ux_audit.heuristic_total(scores)
        self.assertEqual(maximum, 28)  # 7 scored x 4
        self.assertIn(band, ("Excellent", "Good", "Acceptable",
                             "Poor", "Critical", "n/a"))

    def test_audit_dims_capped_and_based(self):
        dims = ux_audit.score_audit_dims(self.entries, self.cross)
        self.assertEqual(
            sorted(dims), sorted(ux_audit.AUDIT_DIMS))
        self.assertLessEqual(dims["Theming"]["score"], 2)
        self.assertLessEqual(dims["Responsive Design"]["score"], 3)
        # ABOUT lacks a viewport meta: responsive floors at 1.
        self.assertEqual(dims["Responsive Design"]["score"], 1)
        total, band = ux_audit.audit_total(dims)
        self.assertIn(total, range(0, 21))
        self.assertTrue(band)


class GroupingTest(unittest.TestCase):
    def test_groups_follow_fixed_order(self):
        findings = [
            {"severity": "low", "title": "t1", "route": "/",
             "viewport": "static", "evidence": "e",
             "category": "Theming"},
            {"severity": "high", "title": "a1", "route": "/",
             "viewport": "static", "evidence": "e",
             "category": "Accessibility"},
        ]
        groups = ux_audit.group_findings(findings)
        self.assertEqual([g["category"] for g in groups],
                         ["Accessibility", "Theming"])
        self.assertIn("worst: high", groups[0]["title"])

    def test_empty_yields_no_findings_group(self):
        groups = ux_audit.group_findings([])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["category"], "No findings")

    def test_guess_target_file(self):
        self.assertEqual(ux_audit.guess_target_file("/"),
                         "index.html")
        self.assertEqual(ux_audit.guess_target_file("/about/"),
                         "about/index.html")
        self.assertEqual(ux_audit.guess_target_file("/x"),
                         "x.html")
        self.assertEqual(ux_audit.guess_target_file("/a/b.html"),
                         "a/b.html")

    def test_group_spec_body_parses_as_task_spec(self):
        from core_cli.parse_task_spec import parse_task_spec
        from core_cli.verify_allowlist import validate_verify_command

        groups = ux_audit.group_findings([
            {"severity": "high", "title": "[P1] x", "route": "/a/",
             "viewport": "static", "evidence": "e",
             "category": "Accessibility"}])
        body = ux_audit.group_spec_body(
            "boomtick.blog", "arii/tech-dancer",
            "arii/tech-dancer#1", 1, groups[0])
        spec = parse_task_spec(body)
        self.assertTrue(spec["target_files"])
        self.assertTrue(validate_verify_command(
            spec["verification_command"]))


class IssueRenderTest(unittest.TestCase):
    def _report(self):
        pages = {"https://arii.github.io/": HOME,
                 "https://arii.github.io/about/": ABOUT}
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=10,
            fetch=stub_fetch_factory(pages))
        return ux_audit.build_report("arii.github.io", crawled)

    def test_render_carries_scores_groups_and_spec(self):
        from core_cli.parse_task_spec import parse_task_spec
        from core_cli.verify_allowlist import validate_verify_command

        report = self._report()
        title, body = ux_audit.render_issue("arii.github.io",
                                            report)
        self.assertIn("[ux-audit] arii.github.io", title)
        self.assertIn("Heuristics:", body)
        self.assertIn("## Findings by group", body)
        self.assertIn("### Group 1:", body)
        spec = parse_task_spec(body)
        self.assertTrue(spec["target_files"])
        self.assertTrue(validate_verify_command(
            spec["verification_command"]))

    def test_findings_severity_ranked(self):
        report = self._report()
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3,
                 "info": 4}
        ranks = [order[f["severity"]] for f in report["findings"]]
        self.assertEqual(ranks, sorted(ranks))

    def test_report_round_trips_through_state(self):
        report = self._report()
        with tempfile.TemporaryDirectory() as tmp:
            path = ux_audit.save_report(report, tmp)
            self.assertTrue(path.startswith(tmp))
            loaded = ux_audit.load_report("arii.github.io", tmp)
            self.assertEqual(loaded["site"], "arii.github.io")
            groups = ux_audit.report_groups("arii.github.io", tmp)
            self.assertTrue(groups)
            self.assertIn("body", groups[0])
            self.assertIn("Target Files", groups[0]["body"])
            self.assertIsNone(ux_audit.load_report(
                "boomtick.blog", tmp))
            self.assertIsNone(ux_audit.report_groups(
                "boomtick.blog", tmp))


class FileIssueTest(unittest.TestCase):
    def _stub_gh(self, tmp, stdout="", stderr="", exit=0):
        """Executable stub gh: records argv, captures the --body-file
        content, then plays canned output. No mocks: file_issue
        shells the real command line against this binary."""
        argv_file = os.path.join(tmp, "gh-argv")
        body_file = os.path.join(tmp, "gh-body.md")
        script = os.path.join(tmp, "gh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\n"
                f'echo "$@" > {shlex.quote(argv_file)}\n'
                "prev=\"\"\n"
                'for a in "$@"; do\n'
                '  if [ "$prev" = "--body-file" ]; then '
                f'cp "$a" {shlex.quote(body_file)}; fi\n'
                '  prev="$a"\n'
                "done\n"
                f"printf %s {shlex.quote(stdout)}\n"
                f"printf %s {shlex.quote(stderr)} >&2\n"
                f"exit {exit}\n")
        os.chmod(script, 0o755)
        return argv_file, body_file

    def _path_with(self, tmp):
        return mock.patch.dict(
            os.environ,
            {"PATH": tmp + os.pathsep + os.environ.get("PATH", "")})

    def test_success_parses_url_and_number(self):
        url = "https://github.com/arii/tech-dancer/issues/12"
        with tempfile.TemporaryDirectory() as tmp:
            self._stub_gh(tmp, stdout=url + "\n")
            with self._path_with(tmp):
                out = ux_audit.file_issue("boomtick.blog", "t", "b")
        self.assertEqual(out, {"number": 12, "url": url,
                               "repo": "arii/tech-dancer"})

    def test_argv_has_no_unsupported_flags(self):
        # Regression guard for #876: gh issue create has no --json,
        # and a fake run seam hid the bad flag from the suite.
        url = "https://github.com/arii/tech-dancer/issues/3"
        with tempfile.TemporaryDirectory() as tmp:
            argv_file, body_file = self._stub_gh(
                tmp, stdout=url + "\n")
            with self._path_with(tmp):
                ux_audit.file_issue("boomtick.blog", "t", "b")
            with open(argv_file, encoding="utf-8") as fh:
                argv = fh.read().strip().split(" ")
            self.assertEqual(
                argv[:7],
                ["issue", "create", "--repo", "arii/tech-dancer",
                 "--title", "t", "--body-file"])
            self.assertNotIn("--json", argv)
            with open(body_file, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "b")

    def test_unknown_site_rejected_before_gh(self):
        with self.assertRaises(ValueError):
            ux_audit.file_issue("nope", "t", "b")

    def test_failure_surfaces_full_output(self):
        stderr = ("unknown flag: --json\n"
                  "Usage:  gh issue create [flags]\n"
                  "  -w, --web  Open the browser to create an issue")
        with tempfile.TemporaryDirectory() as tmp:
            self._stub_gh(tmp, stderr=stderr, exit=1)
            with self._path_with(tmp):
                with self.assertRaises(RuntimeError) as ctx:
                    ux_audit.file_issue("boomtick.blog", "t", "b")
        msg = str(ctx.exception)
        for line in stderr.splitlines():
            self.assertIn(line, msg)

    def test_garbage_stdout_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._stub_gh(tmp, stdout="nothing useful here\n")
            with self._path_with(tmp):
                with self.assertRaises(RuntimeError) as ctx:
                    ux_audit.file_issue("boomtick.blog", "t", "b")
        self.assertIn("no issue URL", str(ctx.exception))

    def test_files_to_mapped_repo(self):
        url = "https://github.com/arii/portfolio/issues/3"
        with tempfile.TemporaryDirectory() as tmp:
            argv_file, _ = self._stub_gh(tmp, stdout=url + "\n")
            with self._path_with(tmp):
                out = ux_audit.file_issue("arii.github.io", "t", "b")
            with open(argv_file, encoding="utf-8") as fh:
                self.assertIn("arii/portfolio",
                              fh.read().split(" "))
        self.assertEqual(out["repo"], "arii/portfolio")


class ModelSynthesisTest(unittest.TestCase):
    def _report(self):
        pages = {"https://arii.github.io/": HOME}
        crawled = ux_audit.crawl(
            "arii.github.io", max_pages=5,
            fetch=stub_fetch_factory(pages))
        return ux_audit.build_report("arii.github.io", crawled)

    def test_stub_model_adds_without_removing(self):
        report = self._report()
        before = len(report["findings"])
        h1_before = report["heuristics"]["1"]["score"]
        reply = json.dumps({
            "findings": [
                {"severity": "P2", "title": "copy tone flat",
                 "route": "/", "evidence": "hero",
                 "category": "Implementation Integrity"},
                {"severity": "bogus", "title": "", "route": "/"},
            ],
            "heuristic_scores": {"1": 3, "99": 4, "2": "n/a"},
            "summary": "Mostly fine."})
        model = mock.Mock()
        model.invoke.return_value = mock.Mock(content=reply)

        def factory(prefer):
            return model, {"provider": "ollama", "model": "q"}

        ux_audit.synthesize_with_model(
            report, [{"route": "/"}], model_factory=factory)
        self.assertEqual(len(report["findings"]), before + 1)
        added = [f for f in report["findings"]
                 if "(model)" in f["title"]]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["severity"], "medium")
        self.assertEqual(report["heuristics"]["1"]["score"], 3)
        self.assertIsNone(report["heuristics"]["2"]["score"])
        self.assertEqual(h1_before, None)
        self.assertIn("ollama", report["model"]["label"])

    def test_ollama_failure_falls_back_to_nebius(self):
        report = self._report()
        model = mock.Mock()
        model.invoke.return_value = mock.Mock(
            content='{"findings": [], "summary": "clean"}')
        calls = []

        def factory(prefer):
            calls.append(prefer)
            if prefer == "ollama":
                raise RuntimeError("down")
            return model, {"provider": "nebius", "model": "n"}

        ux_audit.synthesize_with_model(
            report, [], model_factory=factory)
        self.assertEqual(calls, ["ollama", "nebius"])
        self.assertIn("fallback", report["model"]["label"])

    def test_no_model_raises(self):
        report = self._report()

        def factory(prefer):
            raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            ux_audit.synthesize_with_model(
                report, [], model_factory=factory)

    def test_unparseable_reply_raises(self):
        report = self._report()
        model = mock.Mock()
        model.invoke.return_value = mock.Mock(
            content="no json here at all***")

        def factory(prefer):
            return model, {"provider": "ollama", "model": "q"}

        with self.assertRaises(RuntimeError):
            ux_audit.synthesize_with_model(
                report, [], model_factory=factory)


class RubricTest(unittest.TestCase):
    def test_embedded_fallback_mentions_heuristics(self):
        self.assertIn("H1", ux_audit.EMBEDDED_RUBRIC)
        self.assertIn("Accessibility", ux_audit.EMBEDDED_RUBRIC)

    def test_load_prefers_local_skill_when_present(self):
        text = ux_audit.load_skill_rubric()
        self.assertTrue(text)
        # Either the local skill (long) or the embedded fallback.
        self.assertTrue(len(text) > 500
                        or text == ux_audit.EMBEDDED_RUBRIC)


class CliTest(unittest.TestCase):
    def test_sites_lists_registry(self):
        with mock.patch("sys.stdout") as _:
            self.assertEqual(ux_audit.main(["sites"]), 0)

    def test_audit_rejects_unknown_site(self):
        self.assertEqual(ux_audit.main(["audit", "nope"]), 2)

    def test_audit_rejects_bad_cap(self):
        self.assertEqual(
            ux_audit.main(["audit", "arii.github.io",
                           "--max-pages", "999"]), 2)

    def test_groups_without_report_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                ux_audit.main(["groups", "arii.github.io",
                               "--state-dir", tmp]), 1)

    def test_groups_rejects_unknown_site(self):
        self.assertEqual(ux_audit.main(["groups", "nope"]), 2)


if __name__ == "__main__":
    unittest.main()

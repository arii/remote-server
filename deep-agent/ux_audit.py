"""Dispatchable multi-page UX audit agent (impeccable method, #850).

Zero-Gemini path for whole-site UX audits: crawl a configured site
(link-following within scope), run a deterministic per-page
impeccable pass (heuristic + a11y/perf/theming/responsive/integrity
signals from static HTML), synthesize cross-page findings (nav
consistency, dead links, repeated slop), optionally refine with a
local model (Ollama first, Nebius fallback), and file ONE
comprehensive findings issue to the site's repo.

Audit-only: this module performs HTTP GETs and files an issue. It
never modifies the audited site; fixes ride the normal
assign -> PR -> merge flow (repair dispatch + Review pane).

Site registry (single source of truth; the jobs router and the
console read it through ``sites``/``groups``):
  arii.github.io -> arii/portfolio,   start https://arii.github.io/
  boomtick.blog  -> arii/tech-dancer, start https://boomtick.blog/

Reuses, never replicates: ``impact.rank_findings`` + the shared
finding shape {severity,title,route,viewport,evidence} (+ category
and heuristic keys), ``s5tools.fetch_url`` for fetches, and
``deep_chat.build_model`` for the optional model pass. The model
prompt loads the local impeccable skill text as its rubric when the
checkout carries it, with an embedded compact rubric as fallback.

Stdlib-only at import (same contract as impact.py/s5tools): crawl,
checks, scoring, grouping, and issue rendering import under system
python; only the --model synthesis needs the venv (langchain via
deep_chat), and tests inject a stub model_factory instead.

CLI (cwd: repo root, or deep-agent/ on the path):
  ux_audit.py sites [--json]
  ux_audit.py audit <site> [--max-pages N] [--model] [--file]
                       [--state-dir D] [--out FILE]
  ux_audit.py groups <site> [--state-dir D]

audit prints progress lines plus machine-readable finals:
  AUDIT_GROUP <i>: <title> (<n> findings)
  AUDIT_DONE <repo>#<num> <url>          (only with --file)
"""

import argparse
import html.parser
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

import cli

cli.bootstrap()

try:
    from impact import rank_findings
except ImportError:  # pragma: no cover - hermetic fallback
    def rank_findings(findings):
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3,
                 "info": 4}

        def _key(f):
            return (order.get(f.get("severity"), 99),
                    f.get("route", ""), f.get("title", ""))

        return sorted(findings, key=_key)

try:
    from s5tools import fetch_url
except ImportError:  # pragma: no cover - hermetic fallback
    def fetch_url(url, timeout=20, max_bytes=262144):
        return "FETCH_ERROR: s5tools unavailable"

# --- Site registry --------------------------------------------------

SITES = {
    "arii.github.io": {
        "start_url": "https://arii.github.io/",
        "repo": "arii/portfolio",
        "scope_host": "arii.github.io",
        "scope_prefix": "/",
    },
    "boomtick.blog": {
        "start_url": "https://boomtick.blog/",
        "repo": "arii/tech-dancer",
        "scope_host": "boomtick.blog",
        "scope_prefix": "/",
    },
}

DEFAULT_MAX_PAGES = 25
MAX_PAGES_CAP = 200
FETCH_MAX_BYTES = 262144

# Report state: the jobs router serves finding groups from the
# latest report per site. Env-overridable so tests point at tmp.
DEFAULT_STATE_DIR = "/var/lib/buttons/ux-audit"


def state_dir(explicit=None):
    """Report state dir: explicit arg, env, or the volume default."""
    if explicit:
        return explicit
    return os.environ.get("BUTTONS_UX_AUDIT_DIR", DEFAULT_STATE_DIR)


def site_config(site):
    """Registry entry for a site name. Raises ValueError when unknown."""
    if not isinstance(site, str) or site not in SITES:
        raise ValueError(
            f"unknown site: {site!r} (want one of {sorted(SITES)})")
    return SITES[site]


# --- Crawl ----------------------------------------------------------

class _LinkExtractor(html.parser.HTMLParser):
    """Anchor-href collector. Never raises on hostile markup.
    <link> hrefs (stylesheets, icons, preconnects) are excluded:
    they are never pages. Asset srcs are excluded too: the crawl
    follows pages, not binaries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for key, val in attrs:
            if key == "href" and isinstance(val, str) and val:
                self.links.append(val)


# Asset extensions never crawled as pages (nor probed by the
# dead-link sweep, which would fetch binaries for no verdict).
ASSET_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp",
                    ".svg", ".ico", ".css", ".js", ".map", ".pdf",
                    ".zip", ".woff", ".woff2", ".ttf", ".otf",
                    ".mp4", ".webm", ".mp3", ".wav")

# Machine-readable non-pages: sitemaps, feeds, manifests. Linked
# from homepages but not UX surfaces; auditing them as HTML would
# file false positives (no title/viewport/lang on purpose).
NON_PAGE_EXTENSIONS = (".xml", ".rss", ".atom", ".txt", ".json",
                       ".webmanifest")


def extract_links(html, base_url):
    """Absolute in-crawl http(s) URLs from markup. Never raises."""
    try:
        parser = _LinkExtractor()
        parser.feed(html or "")
    except Exception:
        return []
    out = []
    for raw in parser.links:
        try:
            abs_url = urllib.parse.urljoin(base_url, raw.strip())
        except Exception:
            continue
        parts = urllib.parse.urlsplit(abs_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        lowered = parts.path.lower()
        if lowered.endswith(ASSET_EXTENSIONS + NON_PAGE_EXTENSIONS):
            continue
        out.append(urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path or "/",
             parts.query, "")))
    return out


def _norm_path(path):
    return urllib.parse.unquote(path or "/") or "/"


def in_scope(url, cfg):
    """True when url shares the site's host and scope prefix."""
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if (parts.hostname or "").lower() != cfg["scope_host"].lower():
        return False
    return _norm_path(parts.path).startswith(cfg["scope_prefix"])


def _route_of(url):
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url
    route = _norm_path(parts.path)
    return route + (f"?{parts.query}" if parts.query else "")


def sitemap_urls(xml_body, base_url):
    """Page URLs from a sitemap XML body. Never raises.

    Parses <loc> entries (urlset or sitemapindex — nested sitemap
    URLs are returned too and filtered by scope/extension like any
    link); asset/non-page extensions excluded, same as anchors.
    """
    if not isinstance(xml_body, str) or "<loc" not in xml_body:
        return []
    try:
        locs = re.findall(r"<loc>\s*([^<>\s]+)\s*</loc>", xml_body)
    except Exception:
        return []
    out = []
    for raw in locs[:500]:
        try:
            abs_url = urllib.parse.urljoin(base_url, raw.strip())
        except Exception:
            continue
        parts = urllib.parse.urlsplit(abs_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        lowered = (parts.path or "").lower()
        if lowered.endswith(ASSET_EXTENSIONS + NON_PAGE_EXTENSIONS):
            continue
        out.append(urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path or "/",
             parts.query, "")))
    return out


def crawl(site, max_pages=DEFAULT_MAX_PAGES, fetch=None):
    """Breadth-first crawl within the site's scope.

    Seeds from /sitemap.xml (when served) plus the start URL,
    then follows in-scope anchors. Returns {"site":..., "pages":
    [{url, route, html, error}], "dead": [{url, from_route}]}.
    fetch(url) is the injectable fetch seam (default:
    s5tools.fetch_url); FETCH_ERROR bodies become page errors /
    dead-link rows, never exceptions. The start page failing
    still yields a one-page report so the audit files honestly
    instead of silently auditing nothing. Raises ValueError on
    an unknown site or bad max_pages.
    """
    cfg = site_config(site)
    if (isinstance(max_pages, bool) or not isinstance(max_pages, int)
            or not 1 <= max_pages <= MAX_PAGES_CAP):
        raise ValueError(f"bad max_pages: {max_pages!r}")
    do_fetch = fetch or fetch_url
    seen = set()
    queue = [cfg["start_url"]]
    # Sitemap seeding (best-effort): thinly-linked roots (JS link
    # lists, lone homepages) still audit multi-page. Seeds pass
    # the same scope/extension filters as anchors; a missing or
    # hostile sitemap degrades to anchor-following only.
    try:
        origin = urllib.parse.urlsplit(cfg["start_url"])
        sm_url = f"{origin.scheme}://{origin.netloc}/sitemap.xml"
        try:
            sm_body = do_fetch(sm_url, timeout=20,
                               max_bytes=FETCH_MAX_BYTES)
        except TypeError:
            sm_body = do_fetch(sm_url)
        if isinstance(sm_body, str) and not sm_body.startswith(
                "FETCH_ERROR"):
            for seed in sitemap_urls(sm_body, sm_url):
                if seed not in queue and in_scope(seed, cfg):
                    queue.append(seed)
    except Exception:
        pass
    pages = []
    dead = []
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            body = do_fetch(url, timeout=20, max_bytes=FETCH_MAX_BYTES)
        except TypeError:
            body = do_fetch(url)
        except Exception as exc:
            body = f"FETCH_ERROR: {exc}"
        route = _route_of(url)
        if not isinstance(body, str) or body.startswith("FETCH_ERROR"):
            reason = (body if isinstance(body, str) else "empty body")
            pages.append({"url": url, "route": route, "html": "",
                          "error": reason[:200]})
            dead.append({"url": url, "from_route": "(crawl)",
                         "reason": reason[:200]})
            continue
        if body.lstrip()[:5].lower() == "<?xml":
            pages.append({"url": url, "route": route, "html": "",
                          "error": "", "skipped": "non-HTML (XML)"})
            continue
        pages.append({"url": url, "route": route, "html": body,
                      "error": ""})
        try:
            for link in extract_links(body, url):
                if link in seen or link in queue:
                    continue
                if in_scope(link, cfg):
                    queue.append(link)
        except Exception:
            continue
    # Dead-link sweep: re-check in-scope links that never resolved
    # to a fetched page. Probes are capped at max_pages so a
    # link-heavy site cannot turn the sweep into a second crawl.
    fetched = {p["url"] for p in pages if not p["error"]}
    already_dead = {d["url"] for d in dead}
    link_sources = {}
    for page in pages:
        if page["error"]:
            continue
        try:
            for link in extract_links(page["html"], page["url"]):
                if in_scope(link, cfg) and link not in fetched \
                        and link not in already_dead \
                        and link not in link_sources:
                    link_sources[link] = page["route"]
        except Exception:
            continue
    probes = 0
    for link, source in sorted(link_sources.items()):
        if probes >= max_pages:
            break
        probes += 1
        try:
            probe = do_fetch(link, timeout=20, max_bytes=4096)
        except TypeError:
            try:
                probe = do_fetch(link)
            except Exception as exc:
                probe = f"FETCH_ERROR: {exc}"
        except Exception as exc:
            probe = f"FETCH_ERROR: {exc}"
        if not isinstance(probe, str) or probe.startswith("FETCH_ERROR"):
            dead.append({"url": link, "from_route": source,
                         "reason": probe[:200]
                         if isinstance(probe, str) else "empty"})
    return {"site": site, "repo": cfg["repo"], "pages": pages,
            "dead": dead}


# --- Deterministic per-page impeccable pass -------------------------

# Severity ladder shared with impact.py findings; the P-label in
# each title keeps the impeccable severity visible.
def _finding(severity, title, route, evidence, category,
             heuristic=""):
    item = {"severity": severity, "title": title, "route": route,
            "viewport": "static", "evidence": evidence,
            "category": category}
    if heuristic:
        item["heuristic"] = heuristic
    return item


class _PageFacts(html.parser.HTMLParser):
    """Single-pass structural facts for the deterministic checks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.has_viewport = False
        self.has_description = False
        self.has_lang = False
        self.has_main = False
        self.has_nav = False
        self.has_search = False
        self.has_skip_link = False
        self.headings = []
        self.imgs_missing_alt = []
        self.inputs = []
        self.labels_for = set()
        self.wrapped_inputs = 0
        self._in_label = 0
        self._in_title = False
        self._title_chunks = []
        self.empty_links = 0
        self.empty_buttons = 0
        self._link_depth = 0
        self._link_text = []
        self._button_depth = 0
        self._button_text = []
        self.inline_styles = 0
        self.scripts = 0
        self.scripts_defer = 0
        self.stylesheets = 0
        self.ids = {}
        self.placeholder_links = 0
        self.images = 0
        self.nav_hrefs = None
        self._in_nav = 0
        self._nav_hrefs = []
        self.required_inputs = 0

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v if isinstance(v, str) else "")
                 for k, v in attrs}
        if attrs.get("style", "").strip():
            self.inline_styles += 1
        if tag == "html" and attrs.get("lang", "").strip():
            self.has_lang = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attrs.get("name", "") + " "
                    + attrs.get("property", "")).lower()
            if attrs.get("name", "").lower() == "viewport":
                self.has_viewport = True
            if "description" in name:
                self.has_description = True
        elif tag == "main":
            self.has_main = True
        elif tag == "nav":
            self.has_nav = True
            if self._in_nav == 0:
                self._nav_hrefs = []
            self._in_nav += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings.append(int(tag[1]))
        elif tag == "img":
            self.images += 1
            alt = attrs.get("alt")
            if alt is None or not alt.strip():
                self.imgs_missing_alt.append(
                    attrs.get("src", "")[:120])
        elif tag in ("input", "textarea", "select"):
            if attrs.get("type", "").lower() == "hidden":
                return
            if attrs.get("required") is not None or \
                    attrs.get("aria-required") == "true":
                self.required_inputs += 1
            if self._in_label:
                self.wrapped_inputs += 1
            elif tag == "input" and attrs.get("type", "").lower() in (
                    "submit", "button", "reset", "image"):
                self.wrapped_inputs += 1
            else:
                self.inputs.append(attrs.get("id", ""))
            if attrs.get("type", "").lower() == "search" or \
                    attrs.get("role", "") == "searchbox":
                self.has_search = True
        elif tag == "label":
            self._in_label += 1
            if attrs.get("for"):
                self.labels_for.add(attrs["for"])
        elif tag == "a":
            href = attrs.get("href", "")
            if href.strip() in ("#", ""):
                self.placeholder_links += 1
            if "skip" in (attrs.get("class", "") + " "
                          + attrs.get("id", "")).lower() \
                    or href.startswith("#main") \
                    or href.startswith("#content"):
                self.has_skip_link = True
            self._link_depth += 1
            if self._link_depth == 1:
                self._link_text = []
            if self._in_nav and self.nav_hrefs is None:
                self._nav_hrefs.append(href.split("?")[0][:120])
        elif tag == "button":
            self._button_depth += 1
            if self._button_depth == 1:
                self._button_text = []
        elif tag == "script":
            if attrs.get("src"):
                self.scripts += 1
                if "defer" in attrs or "async" in attrs:
                    self.scripts_defer += 1
        elif tag == "link":
            rel = attrs.get("rel", "").lower()
            if "stylesheet" in rel:
                self.stylesheets += 1
        if attrs.get("id"):
            key = attrs["id"][:80]
            self.ids[key] = self.ids.get(key, 0) + 1
        if attrs.get("role", "") == "search":
            self.has_search = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            self.title = "".join(self._title_chunks).strip()[:200]
        elif tag == "label":
            self._in_label = max(0, self._in_label - 1)
        elif tag == "a":
            if self._link_depth == 1 and not "".join(
                    self._link_text).strip():
                self.empty_links += 1
            self._link_depth = max(0, self._link_depth - 1)
        elif tag == "button":
            if self._button_depth == 1 and not "".join(
                    self._button_text).strip():
                self.empty_buttons += 1
            self._button_depth = max(0, self._button_depth - 1)
        elif tag == "nav":
            self._in_nav = max(0, self._in_nav - 1)
            if self._in_nav == 0 and self.nav_hrefs is None:
                self.nav_hrefs = self._nav_hrefs

    def handle_data(self, data):
        if self._in_title:
            self._title_chunks.append(data)
        if self._link_depth:
            self._link_text.append(data)
        if self._button_depth:
            self._button_text.append(data)


def page_facts(html):
    """Structural facts for markup. Never raises."""
    facts = _PageFacts()
    try:
        facts.feed(html or "")
    except Exception:
        pass
    return facts


def audit_page(route, html):
    """Deterministic impeccable checks for one page.

    Returns (findings, facts): findings use the shared impact
    shape plus category/heuristic keys; facts feed the scorers.
    Never raises on hostile markup.
    """
    try:
        facts = page_facts(html)
    except Exception:
        return ([_finding("info", "[P3] page unparseable", route,
                           "HTMLParser failed on this page",
                           "Implementation Integrity")],
                None)
    out = []
    if facts.title is None or not facts.title:
        out.append(_finding(
            "high", "[P1] missing page title", route,
            "no <title> element (tab/bookmark/SEO identity)", "Implementation Integrity",
            heuristic="H6"))
    if not facts.has_lang:
        out.append(_finding(
            "medium", "[P2] <html> has no lang attribute", route,
            "screen readers guess pronunciation; WCAG 3.1.1",
            "Accessibility", heuristic="H2"))
    if not facts.has_viewport:
        out.append(_finding(
            "high", "[P1] missing viewport meta tag", route,
            "mobile browsers render at desktop width; responsive "
            "breaks without it", "Responsive Design",
            heuristic="H4"))
    for src in facts.imgs_missing_alt[:5]:
        out.append(_finding(
            "high", "[P1] image without alt text", route,
            f"src={src or '(empty)'}; WCAG 1.1.1", "Accessibility",
            heuristic="H2"))
    if len(facts.imgs_missing_alt) > 5:
        out.append(_finding(
            "high", "[P1] more images without alt text", route,
            f"{len(facts.imgs_missing_alt) - 5} further "
            "instances on this page", "Accessibility",
            heuristic="H2"))
    unlabeled = [i for i in facts.inputs if i not in facts.labels_for]
    if unlabeled:
        out.append(_finding(
            "high", "[P1] form inputs without labels", route,
            f"{len(unlabeled)} input(s): "
            + ", ".join(f"id={i!r}" if i else "(no id)"
                        for i in unlabeled[:3]),
            "Accessibility", heuristic="H5"))
    if not facts.headings:
        out.append(_finding(
            "medium", "[P2] no headings on page", route,
            "no h1-h6 structure for screen-reader / SEO outline",
            "Accessibility", heuristic="H8"))
    else:
        if 1 not in facts.headings:
            out.append(_finding(
                "medium", "[P2] no h1 on page", route,
                f"heading levels present: {sorted(set(facts.headings))}",
                "Accessibility", heuristic="H8"))
        prev = 0
        for level in facts.headings:
            if prev and level > prev + 1:
                out.append(_finding(
                    "low", "[P3] skipped heading level", route,
                    f"h{prev} followed by h{level}",
                    "Accessibility", heuristic="H4"))
                break
            prev = level
    if not facts.has_main:
        out.append(_finding(
            "medium", "[P2] no <main> landmark", route,
            "assistive tech cannot jump to page content",
            "Accessibility", heuristic="H7"))
    if facts.empty_links:
        out.append(_finding(
            "medium", "[P2] links with no accessible name", route,
            f"{facts.empty_links} <a> element(s) with no text; "
            "screen readers announce nothing", "Accessibility",
            heuristic="H2"))
    if facts.empty_buttons:
        out.append(_finding(
            "medium", "[P2] buttons with no accessible name", route,
            f"{facts.empty_buttons} <button> element(s) with no text",
            "Accessibility", heuristic="H2"))
    if facts.placeholder_links:
        out.append(_finding(
            "medium", "[P2] placeholder links (href='#')", route,
            f"{facts.placeholder_links} link(s) going nowhere; "
            "dead UI or unfinished wiring", "Implementation Integrity",
            heuristic="H5"))
    dupes = sorted(k for k, n in facts.ids.items() if n > 1)
    if dupes:
        out.append(_finding(
            "medium", "[P2] duplicate element ids", route,
            "ids: " + ", ".join(dupes[:5])
            + ("…" if len(dupes) > 5 else ""),
            "Implementation Integrity", heuristic="H5"))
    if not facts.has_description:
        out.append(_finding(
            "low", "[P3] missing meta description", route,
            "search/social previews fall back to body text",
            "Implementation Integrity"))
    if facts.inline_styles >= 10:
        out.append(_finding(
            "low", "[P3] heavy inline styles", route,
            f"{facts.inline_styles} style attributes; drift from "
            "the token/stylesheet system", "Theming",
            heuristic="H4"))
    blocking = facts.scripts - facts.scripts_defer
    if blocking >= 5:
        out.append(_finding(
            "low", "[P3] render-blocking scripts", route,
            f"{blocking} external scripts without async/defer",
            "Performance", heuristic="H1"))
    if facts.stylesheets >= 6:
        out.append(_finding(
            "low", "[P3] many stylesheets", route,
            f"{facts.stylesheets} <link rel=stylesheet>; "
            "each blocks first paint", "Performance",
            heuristic="H1"))
    return out, facts


# --- Scoring (impeccable rubric, static proxies) --------------------

HEURISTICS = (
    "Visibility of System Status",
    "Match Between System and Real World",
    "User Control and Freedom",
    "Consistency and Standards",
    "Error Prevention",
    "Recognition Rather Than Recall",
    "Flexibility and Efficiency",
    "Aesthetic and Minimalist Design",
    "Error Recovery",
    "Help and Documentation",
)

AUDIT_DIMS = (
    "Accessibility",
    "Performance",
    "Theming",
    "Responsive Design",
    "Implementation Integrity",
)

HEURISTIC_BANDS = ((32, "Excellent"), (24, "Good"), (16, "Acceptable"),
                   (8, "Poor"), (0, "Critical"))
AUDIT_BANDS = ((18, "Excellent"), (14, "Good"), (10, "Acceptable"),
               (6, "Poor"), (0, "Critical"))


def _band(score, bands):
    for floor, name in bands:
        if score >= floor:
            return name
    return bands[-1][1]


def score_heuristics(page_entries, cross):
    """Nielsen 0-4 scores from static signals.

    page_entries: [(route, facts)]. cross: {"nav_variants": n,
    "dead": n, "placeholder_routes": n, ...}. Every score carries
    a basis string naming the signals, so a static proxy never
    masquerades as a browser verdict. Dynamic-only aspects (H1
    feedback, H8 aesthetics, H9 recovery copy) score None (n/a)
    unless the static signals already force a low score.
    Returns {n: {"name", "score"|None, "basis"}}.
    """
    routes = len(page_entries) or 1
    unlabeled = sum(
        len([i for i in f.inputs if i not in f.labels_for])
        for _, f in page_entries if f is not None)
    empty_names = sum(
        (f.empty_links + f.empty_buttons)
        for _, f in page_entries if f is not None)
    no_main = sum(1 for _, f in page_entries
                  if f is not None and not f.has_main)
    no_h1 = sum(1 for _, f in page_entries
                if f is not None and 1 not in f.headings)
    has_search = any(f is not None and f.has_search
                     for _, f in page_entries)
    has_skip = any(f is not None and f.has_skip_link
                   for _, f in page_entries)
    has_nav = sum(1 for _, f in page_entries
                  if f is not None and f.has_nav)
    nav_variants = cross.get("nav_variants", 0)
    dead = cross.get("dead", 0)
    placeholders = cross.get("placeholder_routes", 0)
    scores = {}

    def _put(n, score, basis):
        scores[n] = {"name": HEURISTICS[n - 1], "score": score,
                     "basis": basis}

    _put(1, None, "n/a statically: loading/progress/validation "
                  "feedback needs a running page (browser pass)")
    h2 = 4
    if unlabeled:
        h2 = min(h2, 2)
    if empty_names:
        h2 = min(h2, 2)
    _put(2, h2, f"{unlabeled} unlabeled input(s), {empty_names} "
                f"nameless link/button(s) across {routes} page(s)")
    h3 = 3
    if not has_skip:
        h3 = 2
    _put(3, h3, "skip link "
                f"{'present' if has_skip else 'absent'}; undo/cancel "
                "flows need a browser pass")
    h4 = 4
    if nav_variants > 1:
        h4 = min(h4, 2)
    if placeholders:
        h4 = min(h4, 2)
    _put(4, h4, f"{nav_variants} distinct nav structure(s); "
                f"{placeholders} page(s) with placeholder links")
    h5 = 4
    if unlabeled:
        h5 = min(h5, 1)
    if dead:
        h5 = min(h5, 2)
    _put(5, h5, f"{unlabeled} unlabeled input(s); {dead} dead "
                "in-scope link(s)")
    h6 = 4
    if no_main:
        h6 = min(h6, 2)
    if no_h1:
        h6 = min(h6, 3)
    _put(6, h6, f"{no_main}/{routes} page(s) without <main>; "
                f"{no_h1}/{routes} without h1")
    h7 = 3 if has_search else 2
    _put(7, h7, f"site search {'found' if has_search else 'not found'}; "
                f"nav present on {has_nav}/{routes} page(s); shortcuts "
                "need a browser pass")
    _put(8, None, "n/a statically: visual density/typography "
                  "needs rendered inspection (browser pass)")
    _put(9, None, "n/a statically: error copy and recovery "
                  "flows need interaction (browser pass)")
    help_routes = sum(
        1 for r, _ in page_entries
        if re.search(r"help|faq|docs|support|contact", r, re.I))
    h10 = 4 if help_routes else 2
    _put(10, h10, f"{help_routes} help/faq/docs/support/contact "
                 "route(s) in the crawl")
    return scores


def score_audit_dims(page_entries, cross):
    """Audit-dimension 0-4 scores from static signals.

    Caps (documented in each basis): theming caps at 2 without a
    browser (token usage is unverifiable statically), responsive
    caps at 3 (touch targets need layout), performance is a
    resource-count proxy. Returns {dim: {"score", "basis"}}.
    """
    routes = len(page_entries) or 1
    facts_list = [f for _, f in page_entries if f is not None]
    a11y_types = set()
    for f in facts_list:
        if f.imgs_missing_alt:
            a11y_types.add("missing-alt")
        if [i for i in f.inputs if i not in f.labels_for]:
            a11y_types.add("unlabeled-input")
        if not f.has_lang:
            a11y_types.add("no-lang")
        if 1 not in f.headings:
            a11y_types.add("heading-gap")
        if not f.has_main:
            a11y_types.add("no-main")
        if f.empty_links or f.empty_buttons:
            a11y_types.add("empty-names")
    a11y = max(0, 4 - len(a11y_types))
    scripts = sum(f.scripts for f in facts_list)
    blocking = sum(f.scripts - f.scripts_defer for f in facts_list)
    sheets = sum(f.stylesheets for f in facts_list)
    images = sum(f.images for f in facts_list)
    perf_deductions = (1 if blocking >= 5 else 0) \
        + (1 if sheets >= 6 * routes else 0) \
        + (1 if scripts >= 10 * routes else 0) \
        + (1 if images >= 30 * routes else 0)
    perf = max(0, 4 - perf_deductions)
    inline = sum(f.inline_styles for f in facts_list)
    theming = 2 if inline < 10 * routes else 1
    noview = sum(1 for f in facts_list if not f.has_viewport)
    responsive = 1 if noview else 3
    integ_deductions = 0
    if any(f.title is None or not f.title for f in facts_list):
        integ_deductions += 1
    if any(f.placeholder_links for f in facts_list):
        integ_deductions += 1
    if any(any(n > 1 for n in f.ids.values()) for f in facts_list):
        integ_deductions += 1
    if cross.get("dead"):
        integ_deductions += 1
    integ = max(0, 4 - integ_deductions)
    return {
        "Accessibility": {
            "score": a11y,
            "basis": f"{len(a11y_types)} violation type(s): "
                     + (", ".join(sorted(a11y_types)) or "none")},
        "Performance": {
            "score": perf,
            "basis": f"resource proxy: {scripts} scripts "
                     f"({blocking} blocking), {sheets} stylesheets, "
                     f"{images} images across {routes} page(s)"},
        "Theming": {
            "score": theming,
            "basis": f"capped at 2 statically (token usage needs "
                     f"a browser); {inline} inline style attribute(s)"},
        "Responsive Design": {
            "score": responsive,
            "basis": ("no viewport meta on "
                      f"{noview}/{routes} page(s)" if noview else
                      "viewport meta present; capped at 3 statically "
                      "(touch targets need layout)")},
        "Implementation Integrity": {
            "score": integ,
            "basis": f"title/meta/ids/placeholder/dead-link "
                     f"deductions: {integ_deductions}"},
    }


def heuristic_total(heuristics):
    """(total, maximum, band) over scored heuristics only."""
    scored = [h["score"] for h in heuristics.values()
              if h["score"] is not None]
    total = sum(scored)
    maximum = 4 * len(scored)
    band = _band(round(total / maximum * 40) if maximum else 0,
                 HEURISTIC_BANDS) if maximum else "n/a"
    return total, maximum, band


def audit_total(dims):
    """(total, band) over the five audit dimensions."""
    total = sum(d["score"] for d in dims.values())
    return total, _band(total, AUDIT_BANDS)


# --- Cross-page synthesis -------------------------------------------

def synthesize_cross_page(page_entries, dead):
    """Cross-page findings: nav consistency, dead links, repeats.

    page_entries: [(route, facts)]. dead: crawl dead-link rows.
    Returns (cross_findings, cross_stats).
    """
    out = []
    nav_sets = {}
    for route, facts in page_entries:
        if facts is None or not facts.has_nav:
            continue
        key = tuple(facts.nav_hrefs or ())
        nav_sets.setdefault(key, []).append(route)
    variants = len(nav_sets)
    if variants > 1:
        biggest = max(len(v) for v in nav_sets.values())
        odd = sorted(r for v in nav_sets.values() if len(v) < biggest
                     for r in v)
        out.append(_finding(
            "medium", "[P2] inconsistent navigation across pages",
            odd[0] if odd else "(site)",
            f"{variants} distinct nav structures; minority routes: "
            + (", ".join(odd[:5]) or "none"),
            "Implementation Integrity", heuristic="H4"))
    for row in dead[:20]:
        out.append(_finding(
            "high", "[P1] dead in-scope link", row.get("from_route",
                                                      "(crawl)"),
            f"{row.get('url', '')[:200]} — "
            f"{row.get('reason', 'fetch failed')[:120]}",
            "Implementation Integrity", heuristic="H5"))
    if len(dead) > 20:
        out.append(_finding(
            "high", "[P1] further dead in-scope links", "(site)",
            f"{len(dead) - 20} more dead link(s)",
            "Implementation Integrity", heuristic="H5"))
    placeholder_routes = sum(
        1 for _, f in page_entries
        if f is not None and f.placeholder_links)
    missing_viewport = sum(
        1 for _, f in page_entries
        if f is not None and not f.has_viewport)
    stats = {"nav_variants": variants, "dead": len(dead),
             "placeholder_routes": placeholder_routes,
             "missing_viewport": missing_viewport,
             "pages": len(page_entries)}
    return out, stats


# --- Grouping + task-spec rendering ---------------------------------

# Fixed group order: severity-ranked categories, stable across runs.
GROUP_ORDER = ("Accessibility", "Responsive Design",
               "Implementation Integrity", "Performance", "Theming")

# Verify command carried by filed issues and group specs. Static
# site repos ship no pytest/vitest suite, so the verify is a
# tree-smoke placeholder (unittest with zero tests exits 0); the
# allowlist admits it, which keeps findings assignable through the
# normal repair-dispatch gate. Runbook documents the tradeoff.
VERIFY_COMMAND = "python3 -m unittest"

TARGET_GUESS_NOTE = ("URL-derived guesses — confirm paths in the "
                     "checkout before editing")


def guess_target_file(route):
    """Likely repo-relative source file for a crawled route.

    Static-site mapping: / -> index.html, /about/ -> about/index.html,
    /x -> x.html. Never raises; always returns a checkout-relative
    path (safe for the task-spec Target Files section).
    """
    try:
        path = urllib.parse.urlsplit(route or "/").path or "/"
    except Exception:
        path = "/"
    path = re.sub(r"[^A-Za-z0-9._/-]", "", path).strip("/") or ""
    if not path:
        return "index.html"
    if path.endswith("/"):
        path = path.rstrip("/")
    if "." in path.rsplit("/", 1)[-1]:
        return path
    if (route or "").endswith("/"):
        return f"{path}/index.html"
    return f"{path}.html"


def group_findings(findings):
    """Findings grouped by category in GROUP_ORDER (+ unknowns).

    Returns [{"category", "findings" (severity-ranked), "title"}].
    Empty groups are omitted; an empty input yields one empty
    "No findings" group so the issue still renders assignably.
    """
    buckets = {}
    for item in findings:
        buckets.setdefault(item.get("category",
                                    "Implementation Integrity"),
                           []).append(item)
    groups = []
    for category in list(GROUP_ORDER) + sorted(
            c for c in buckets if c not in GROUP_ORDER):
        items = buckets.get(category)
        if not items:
            continue
        ranked = rank_findings(items)
        top = ranked[0].get("severity", "info")
        groups.append({"category": category, "findings": ranked,
                       "title": f"{category} ({len(ranked)} "
                                f"finding(s), worst: {top})"})
    if not groups:
        groups.append({"category": "No findings", "findings": [],
                       "title": "No findings — site reads clean"})
    return groups


def group_spec_body(site, repo, issue_ref, index, group):
    """Task-spec markdown for one finding group (assign payload).

    Parses under backend.parse_task_spec (Target Files +
    Verification Command) so repair dispatch accepts it as a
    --body focus override; the worker then fixes this group only.
    """
    routes = []
    for item in group["findings"]:
        route = item.get("route", "")
        if route and route not in routes:
            routes.append(route)
    targets = [guess_target_file(r) for r in routes[:10]] or \
        ["index.html"]
    lines = [f"# UX audit follow-up: {group['category']} ({site})",
             "",
             f"Source: {issue_ref} group {index} "
             f"({group['title']}). Fix this group only.",
             "",
             "## Target Files",
             f"<!-- {TARGET_GUESS_NOTE} -->"]
    lines += [f"- `{t}`" for t in targets]
    lines += ["", "## Verification Command", "",
              f"```\n{VERIFY_COMMAND}\n```", "",
              "## Required Changes"]
    for item in group["findings"][:30]:
        where = item.get("route", "")
        lines.append(f"- [{item.get('severity', 'info')}] "
                     f"{item.get('title', '')} ({where}): "
                     f"{item.get('evidence', '')[:200]}")
    if len(group["findings"]) > 30:
        lines.append(f"- …and {len(group['findings']) - 30} more "
                     "finding(s) in this group (see the source issue)")
    lines += ["", "## Expected Output",
              "Group findings resolved; verification passes; no "
              "unrelated pages changed."]
    return "\n".join(lines) + "\n"


# --- Issue rendering + filing ---------------------------------------

def render_issue(site, report):
    """ONE comprehensive findings-issue body for a site report.

    report: build_report() output. The body carries the heuristic
    + audit score tables, severity-ranked findings grouped for
    one-tap assign, and the Target Files / Verification Command
    task-spec sections so the issue dispatches through the normal
    repair gate. Returns (title, body).
    """
    cfg = site_config(site)
    repo = cfg["repo"]
    heuristics = report["heuristics"]
    dims = report["dims"]
    h_total, h_max, h_band = heuristic_total(
        {int(k): v for k, v in heuristics.items()})
    a_total, a_band = audit_total(dims)
    groups = report["groups"]
    findings = report["findings"]
    pages = report["page_count"]
    routes = report.get("routes", [])
    counts = {}
    for item in findings:
        counts[item.get("severity", "info")] = \
            counts.get(item.get("severity", "info"), 0) + 1
    title = (f"[ux-audit] {site}: {len(findings)} findings "
             f"across {pages} pages ({a_total}/20 {a_band})")
    lines = [f"# UX audit: {site}", "",
             f"Automated multi-page audit (impeccable method, static "
             f"pass). Audited {pages} page(s); crawl rooted at "
             f"{cfg['start_url']}.",
             "",
             "## Scores",
             f"Heuristics: **{h_total}/{h_max}** ({h_band}); "
             f"audit dimensions: **{a_total}/20** ({a_band}).",
             "",
             "| # | Heuristic | Score | Basis |",
             "|---|-----------|-------|-------|"]
    for num in sorted(heuristics, key=int):
        h = heuristics[num]
        score = h["score"] if h["score"] is not None else "n/a"
        lines.append(f"| {num} | {h['name']} | {score} | "
                     f"{h['basis'][:120]} |")
    lines += ["", "| Dimension | Score | Basis |",
              "|-----------|-------|-------|"]
    for dim in AUDIT_DIMS:
        d = dims.get(dim, {"score": 0, "basis": ""})
        lines.append(f"| {dim} | {d['score']} | {d['basis'][:120]} |")
    lines += ["",
              "## Severity counts",
              " ".join(f"{sev}: {counts.get(sev, 0)}"
                       for sev in ("critical", "high", "medium",
                                   "low", "info")),
              "",
              "## Findings by group",
              "Assign one group per agent session from the console "
              "(UX audit section); each group is a task spec."]
    for idx, group in enumerate(groups, 1):
        lines += ["", f"### Group {idx}: {group['title']}"]
        for item in group["findings"][:15]:
            heur = f" {item['heuristic']}" if item.get("heuristic") \
                else ""
            lines.append(
                f"- **{item.get('severity', 'info')}**{heur} "
                f"`{item.get('route', '')}` — {item.get('title', '')}")
            if item.get("evidence"):
                lines.append(f"  - Evidence: {item['evidence'][:200]}")
        if len(group["findings"]) > 15:
            lines.append(f"- …and {len(group['findings']) - 15} "
                         "more in this group")
        if not group["findings"]:
            lines.append("- No findings in this group.")
    lines += ["", "## Pages crawled"]
    lines += [f"- `{r}`" for r in routes[:40]]
    if len(routes) > 40:
        lines.append(f"- …and {len(routes) - 40} more")
    targets = []
    for item in findings:
        guess = guess_target_file(item.get("route", ""))
        if guess not in targets:
            targets.append(guess)
    targets = targets[:15] or ["index.html"]
    lines += ["", "## Target Files",
              f"<!-- {TARGET_GUESS_NOTE} -->"]
    lines += [f"- `{t}`" for t in targets]
    lines += ["", "## Verification Command", "",
              f"```\n{VERIFY_COMMAND}\n```", "",
              "## Required Changes",
              "Resolve the findings group by group (see above); "
              "keep each fix to its group's pages.",
              "",
              "## Expected Output",
              "Follow-up PRs reference this issue; a re-audit "
              "shows the scores above improving.",
              "",
              f"_Filed by the dispatched UX audit agent for {site} "
              f"(repo {repo}); audit-only, no site content changed._"]
    return title, "\n".join(lines) + "\n"


def file_issue(site, title, body):
    """File the findings issue via gh. Returns {number, url, repo}.

    Runs the real `gh issue create` and parses the created-issue
    URL from stdout, deriving the number from the URL. No --json:
    the subcommand has no such flag (#876 — gh exits non-zero
    printing usage help). Raises RuntimeError with the full gh
    output on failure. No run seam: tests cover this through a
    stub gh binary on PATH instead of mocks.
    """
    cfg = site_config(site)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="ux-audit-",
        delete=False, encoding="utf-8")
    try:
        tmp.write(body)
        tmp.close()
        proc = subprocess.run(
            ["gh", "issue", "create", "--repo", cfg["repo"],
             "--title", title, "--body-file", tmp.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, text=True)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if not detail:
            detail = f"exit {proc.returncode}"
        if len(detail) > 4000:
            detail = detail[:4000] + "…[truncated]"
        raise RuntimeError(f"gh issue create failed: {detail}")
    words = (proc.stdout or "").strip().split()
    url = words[-1] if words else ""
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        raise RuntimeError(
            "gh issue create returned no issue URL: "
            f"{(proc.stdout or '').strip()[:200]}")
    return {"number": int(match.group(1)), "url": url,
            "repo": cfg["repo"]}


# --- Report build + state -------------------------------------------

def build_report(site, crawled):
    """Full deterministic report (no model, no filing).

    Returns {"site","repo","at","page_count","routes","findings"
    (ranked),"groups","heuristics","dims","cross"}.
    """
    entries = []
    findings = []
    routes = []
    for page in crawled.get("pages", []):
        routes.append(page.get("route", ""))
        if page.get("skipped"):
            continue
        if page.get("error"):
            findings.append(_finding(
                "high", "[P1] page failed to fetch",
                page.get("route", ""),
                page.get("error", "")[:200],
                "Implementation Integrity", heuristic="H5"))
            entries.append((page.get("route", ""), None))
            continue
        page_findings, facts = audit_page(page.get("route", ""),
                                          page.get("html", ""))
        findings.extend(page_findings)
        entries.append((page.get("route", ""), facts))
    entries = [(r, f) for r, f in entries if f is not None]
    cross_findings, cross = synthesize_cross_page(
        entries, crawled.get("dead", []))
    findings.extend(cross_findings)
    ranked = rank_findings(findings)
    heuristics = {str(n): v for n, v in
                  score_heuristics(entries, cross).items()}
    dims = score_audit_dims(entries, cross)
    groups = group_findings(ranked)
    audited = [p for p in crawled.get("pages", [])
               if not p.get("skipped")]
    return {"site": site, "repo": crawled.get("repo", ""),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime()),
            "page_count": len(audited),
            "routes": routes, "findings": ranked, "groups": groups,
            "heuristics": heuristics, "dims": dims, "cross": cross,
            "model": None}


def save_report(report, state=None):
    """Persist the latest report for a site. Returns the path."""
    directory = state_dir(state)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{report['site']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f)
    os.replace(tmp, path)
    return path


def load_report(site, state=None):
    """Latest report for a site, or None when no run exists yet."""
    site_config(site)
    path = os.path.join(state_dir(state), f"{site}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def report_groups(site, state=None):
    """Assign payloads for a site's latest report.

    Returns None when no report exists, else
    [{"index","category","title","count","worst","body"}] with
    group_spec_body() bodies (issue ref resolved when the report
    carries its filing record).
    """
    report = load_report(site, state)
    if report is None:
        return None
    filing = report.get("filing") or {}
    if filing.get("number"):
        ref = f"{filing.get('repo', report['repo'])}#{filing['number']}"
    else:
        ref = f"{report['repo']} (unfiled run {report.get('at', '')})"
    out = []
    for idx, group in enumerate(report.get("groups", []), 1):
        ranked = group.get("findings", [])
        worst = ranked[0].get("severity", "info") if ranked else "info"
        out.append({
            "index": idx, "category": group.get("category", ""),
            "title": group.get("title", ""),
            "count": len(ranked), "worst": worst,
            "body": group_spec_body(report["site"], report["repo"],
                                    ref, idx, group)})
    return out


# --- Model refinement (optional, venv-only at call time) -------------

# Compact embedded rubric: the impeccable heuristic + dimension
# names the model refines against when the local skill text is
# unavailable (hermetic tests, foreign checkouts).
EMBEDDED_RUBRIC = """Nielsen heuristics (score 0-4 each; n/a only when \
genuinely inapplicable): H1 Visibility of System Status, H2 Match \
System/Real World, H3 User Control and Freedom, H4 Consistency and \
Standards, H5 Error Prevention, H6 Recognition Rather Than Recall, \
H7 Flexibility and Efficiency, H8 Aesthetic and Minimalist Design, \
H9 Error Recovery, H10 Help and Documentation.
Audit dimensions (score 0-4 each): Accessibility (WCAG AA: contrast, \
ARIA, keyboard, semantics, alt, labels), Performance (blocking \
resources, unoptimized assets), Theming (token usage, dark mode), \
Responsive Design (breakpoints, touch targets, overflow), \
Implementation Integrity (coherent system, no drift/placeholders).
Finding severities: P0 blocking, P1 major/WCAG-AA violation, \
P2 minor, P3 polish."""

RUBRIC_MAX_CHARS = 12000


def load_skill_rubric():
    """Impeccable rubric text for the model prompt.

    Prefers the local skill's reference/critique.md (heuristics
    guide) + reference/audit.md (diagnostic scan): first relative
    to this install's checkout root (cli.repo_root, S5_CHECKOUT
    override), then UX_AUDIT_SKILL_ROOT when set (the skill is
    git-ignored, so checkouts built from git point this at a live
    checkout carrying it). Falls back to EMBEDDED_RUBRIC when the
    skill is absent everywhere. Never raises; always text.
    """
    try:
        roots = [cli.repo_root()]
        extra = os.environ.get("UX_AUDIT_SKILL_ROOT", "")
        if extra and extra not in roots:
            roots.append(extra)
        ref = None
        for root in roots:
            cand = os.path.join(root, ".agents", "skills",
                                "impeccable", "reference")
            if os.path.isdir(cand):
                ref = cand
                break
        if ref is None:
            return EMBEDDED_RUBRIC
        chunks = []
        for name in ("critique.md", "audit.md"):
            path = os.path.join(ref, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    chunks.append(f"# {name}\n" + f.read(6000))
        if chunks:
            return "\n\n".join(chunks)[:RUBRIC_MAX_CHARS]
    except Exception:
        pass
    return EMBEDDED_RUBRIC


def _default_model_factory(prefer):
    """(model, spec) via deep_chat.build_model. Venv-only at call."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import deep_chat

    return deep_chat.build_model(prefer=prefer)


def _extract_json(text):
    """First parseable {...}/[...] span in model text, else None."""
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",
                      text, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    return None


_MODEL_SEV = {"P0": "critical", "P1": "high", "P2": "medium",
              "P3": "low"}


def synthesize_with_model(report, pages_digest, model_factory=None):
    """Refine a deterministic report with the local model.

    Ollama first, Nebius fallback (same ladder as the #845
    reviewer). The model may add findings and re-score the n/a
    heuristics; deterministic findings and scores are never
    removed, only supplemented. Returns the updated report
    (report["model"] records the label). model_factory(prefer)
    is the hermetic seam. Raises RuntimeError when no model is
    available or the reply parses to nothing usable.
    """
    factory = model_factory or _default_model_factory
    model, spec, last_exc, fell_back = None, None, None, False
    for name in ("ollama", "nebius"):
        try:
            model, spec = factory(name)
            break
        except Exception as exc:
            last_exc = exc
            fell_back = True
    if model is None:
        raise RuntimeError(f"no model available (ollama/nebius): "
                           f"{last_exc}")
    label = f"{spec.get('provider', '?')}:{spec.get('model', '?')}"
    if fell_back:
        label += " (fallback)"
    rubric = load_skill_rubric()
    det = [{"severity": f["severity"], "title": f["title"],
            "route": f["route"]} for f in report["findings"][:60]]
    prompt = (
        f"You are refining a static-HTML UX audit of "
        f"{report['site']} ({report['page_count']} pages crawled). "
        f"Apply this rubric:\n\n{rubric}\n\n"
        f"Deterministic findings already recorded (do not repeat "
        f"these):\n{json.dumps(det)}\n\n"
        f"Per-page digests (route + structural facts):\n"
        f"{json.dumps(pages_digest)[:8000]}\n\n"
        f"Reply with JSON ONLY: {{\"findings\": [{{\"severity\": "
        f"\"P0|P1|P2|P3\", \"title\": ..., \"route\": ..., "
        f"\"evidence\": ..., \"category\": ...}}], "
        f"\"heuristic_scores\": {{\"1\": 0-4|\"n/a\", ...}}, "
        f"\"summary\": \"2-3 sentences\"}}. Rules: findings only "
        f"for issues the deterministic pass provably missed "
        f"(cross-page UX, copy tone, IA); heuristic_scores only "
        f"for heuristics worth overriding (else omit); empty "
        f"findings [] when there is nothing concrete.")
    content = getattr(model.invoke(prompt), "content", "")
    raw = _extract_json(content if isinstance(content, str)
                        else str(content))
    if not isinstance(raw, dict):
        raise RuntimeError(f"model reply unparseable ({label})")
    added = []
    items = raw.get("findings")
    if isinstance(items, list):
        for entry in items[:20]:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            plabel = str(entry.get("severity", "")).strip().upper()
            if plabel not in _MODEL_SEV:
                plabel = "P3"
            sev = _MODEL_SEV[plabel]
            route = entry.get("route")
            route = route if isinstance(route, str) and route \
                else "(site)"
            category = entry.get("category")
            category = category \
                if isinstance(category, str) and category else \
                "Implementation Integrity"
            added.append(_finding(
                sev, f"[{plabel}] {title.strip()[:200]} (model)",
                route, str(entry.get("evidence", ""))[:300],
                category))
    overrides = raw.get("heuristic_scores")
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            num = str(key).strip()
            if num not in report["heuristics"]:
                continue
            if value == "n/a" or value is None:
                report["heuristics"][num] = {
                    "name": report["heuristics"][num]["name"],
                    "score": None,
                    "basis": f"model override ({label}): n/a"}
            elif isinstance(value, int) and not isinstance(
                    value, bool) and 0 <= value <= 4:
                report["heuristics"][num] = {
                    "name": report["heuristics"][num]["name"],
                    "score": value,
                    "basis": f"model override ({label})"}
    if added:
        merged = rank_findings(report["findings"] + added)
        report["findings"] = merged
        report["groups"] = group_findings(merged)
    summary = raw.get("summary")
    report["model"] = {
        "label": label,
        "added_findings": len(added),
        "summary": summary.strip()[:500]
        if isinstance(summary, str) and summary.strip() else ""}
    return report


def pages_digest(crawled):
    """Compact per-page structural digests for the model prompt."""
    out = []
    for page in crawled.get("pages", [])[:40]:
        if page.get("error"):
            out.append({"route": page.get("route", ""),
                        "error": page.get("error", "")[:120]})
            continue
        facts = page_facts(page.get("html", ""))
        out.append({
            "route": page.get("route", ""),
            "title": facts.title,
            "headings": facts.headings[:12],
            "images": facts.images,
            "missing_alt": len(facts.imgs_missing_alt),
            "inputs": len(facts.inputs),
            "unlabeled": len([i for i in facts.inputs
                              if i not in facts.labels_for]),
            "has_nav": facts.has_nav,
            "has_main": facts.has_main,
            "has_search": facts.has_search,
            "viewport": facts.has_viewport,
            "lang": facts.has_lang,
            "inline_styles": facts.inline_styles,
            "scripts": facts.scripts,
            "placeholders": facts.placeholder_links})
    return out


# --- CLI ------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="ux_audit.py",
        description="Dispatchable multi-page UX audit agent "
                    "(impeccable method). Audit-only: crawls and "
                    "files one findings issue; never changes sites.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_sites = sub.add_parser("sites", help="Print the site registry.")
    p_sites.add_argument("--json", action="store_true",
                         help="Print the registry as JSON.")
    p_audit = sub.add_parser("audit", help="Crawl + audit a site.")
    p_audit.add_argument("site", help="Site name from the registry.")
    p_audit.add_argument("--max-pages", type=int,
                         default=DEFAULT_MAX_PAGES,
                         help=f"Page cap 1-{MAX_PAGES_CAP} "
                              f"(default {DEFAULT_MAX_PAGES}).")
    p_audit.add_argument("--model", action="store_true",
                         help="Refine with the local model "
                              "(Ollama, Nebius fallback; venv-only).")
    p_audit.add_argument("--file", action="store_true",
                         help="File ONE findings issue to the "
                              "site's repo and print AUDIT_DONE.")
    p_audit.add_argument("--state-dir", default="",
                         help="Report state dir (default: env "
                              "BUTTONS_UX_AUDIT_DIR or the volume).")
    p_audit.add_argument("--out", default="",
                         help="Also write the report JSON here.")
    p_groups = sub.add_parser("groups", help="Assign payloads from "
                             "the latest report.")
    p_groups.add_argument("site", help="Site name from the registry.")
    p_groups.add_argument("--state-dir", default="",
                          help="Report state dir override.")
    p_groups.add_argument("--json", action="store_true",
                          help="Print groups as JSON (default).")
    return parser


def main(argv=None):
    args = build_parser().parse_args(
        sys.argv[1:] if argv is None else argv)
    if args.command == "sites":
        if args.json:
            print(json.dumps(SITES, indent=1, sort_keys=True))
        else:
            for name in sorted(SITES):
                cfg = SITES[name]
                print(f"{name} -> {cfg['repo']} ({cfg['start_url']})")
        return 0
    if args.command == "groups":
        try:
            groups = report_groups(
                args.site, args.state_dir or None)
        except ValueError as exc:
            print(f"ux_audit: {exc}", file=sys.stderr)
            return 2
        if groups is None:
            print(f"ux_audit: no report yet for {args.site} "
                  "(run audit first)", file=sys.stderr)
            return 1
        print(json.dumps(groups, indent=1))
        return 0
    # audit
    try:
        cfg = site_config(args.site)
    except ValueError as exc:
        print(f"ux_audit: {exc}", file=sys.stderr)
        return 2
    if not 1 <= args.max_pages <= MAX_PAGES_CAP:
        print(f"ux_audit: bad --max-pages {args.max_pages}: want "
              f"1-{MAX_PAGES_CAP}", file=sys.stderr)
        return 2
    state = args.state_dir or None
    print(f"ux_audit: crawling {args.site} "
          f"(max {args.max_pages} pages)…", flush=True)
    crawled = crawl(args.site, max_pages=args.max_pages)
    ok = sum(1 for p in crawled["pages"] if not p["error"])
    print(f"ux_audit: fetched {ok}/{len(crawled['pages'])} pages, "
          f"{len(crawled['dead'])} dead link(s)", flush=True)
    if not crawled["pages"]:
        print("ux_audit: nothing crawled; not filing",
              file=sys.stderr)
        return 1
    report = build_report(args.site, crawled)
    if args.model:
        try:
            report = synthesize_with_model(
                report, pages_digest(crawled))
            added = report["model"]["added_findings"]
            print(f"ux_audit: model {report['model']['label']} "
                  f"added {added} finding(s)", flush=True)
        except RuntimeError as exc:
            print(f"ux_audit: model pass skipped ({exc})",
                  file=sys.stderr)
    path = save_report(report, state)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=1)
        except OSError as exc:
            print(f"ux_audit: --out write failed ({exc})",
                  file=sys.stderr)
            return 1
    print(f"ux_audit: {len(report['findings'])} findings, "
          f"{len(report['groups'])} group(s); report {path}")
    for idx, group in enumerate(report["groups"], 1):
        print(f"AUDIT_GROUP {idx}: {group['title']}")
    if args.file:
        title, body = render_issue(args.site, report)
        try:
            filing = file_issue(args.site, title, body)
        except RuntimeError as exc:
            print(f"ux_audit: filing failed ({exc})", file=sys.stderr)
            return 1
        report["filing"] = filing
        save_report(report, state)
        print(f"AUDIT_DONE {filing['repo']}#{filing['number']} "
              f"{filing['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

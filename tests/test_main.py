"""Regression tests for scripts/main.py.

Everything runs against the fixture site in tests/fixtures, served from a
throwaway local HTTP server on an ephemeral port, so the suite needs no
internet access and no manual setup:

    python -m unittest discover -s tests

Only the rendering tests need the Playwright browser; they skip themselves
if it has not been downloaded (``python -m playwright install chromium``).
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import main as checker  # noqa: E402  (path must be set up first)

# Pages the generated sitemap advertises; deliberately fewer than the crawl
# can reach, so the two discovery modes are distinguishable.
SITEMAP_PAGES = ("index.html", "about.html", "blog.html")

_server: http.server.ThreadingHTTPServer | None = None
BASE_URL = ""


class _FixtureHandler(http.server.SimpleHTTPRequestHandler):
    """Serves tests/fixtures, plus a sitemap.xml built for the live port."""

    def log_message(self, *args) -> None:
        """Silence the per-request logging so test output stays readable."""

    def do_GET(self) -> None:
        if self.path != "/sitemap.xml":
            super().do_GET()
            return
        base = f"http://{self.headers['Host']}"
        entries = "".join(f"<url><loc>{base}/{page}</loc></url>" for page in SITEMAP_PAGES)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{entries}</urlset>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def setUpModule() -> None:
    """Start the fixture server on a free port for the whole module."""
    global _server, BASE_URL
    handler = functools.partial(_FixtureHandler, directory=str(FIXTURES))
    _server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    BASE_URL = f"http://127.0.0.1:{_server.server_port}"
    threading.Thread(target=_server.serve_forever, daemon=True).start()


def tearDownModule() -> None:
    """Stop the fixture server."""
    if _server is not None:
        _server.shutdown()
        _server.server_close()


def _plain_fetcher(workers: int = 4) -> checker.PageFetcher:
    """A fetcher that never starts a browser."""
    return checker.PageFetcher("never", workers)


class UrlHandlingTests(unittest.TestCase):
    """Pure URL and HTML helpers - no network involved."""

    def test_encode_url_percent_encodes_non_ascii(self):
        self.assertEqual(
            checker.encode_url("https://example.com/über uns?q=é"),
            "https://example.com/%C3%BCber%20uns?q=%C3%A9",
        )

    def test_encode_url_leaves_encoded_urls_alone(self):
        already = "https://example.com/already%20encoded?a=1&b=2"
        self.assertEqual(checker.encode_url(already), already)

    def test_encode_url_punycodes_idn_host(self):
        self.assertEqual(
            checker.encode_url("https://bücher.example/x"),
            "https://xn--bcher-kva.example/x",
        )

    def test_canonical_page_folds_root_spellings(self):
        self.assertEqual(
            checker._canonical_page("https://example.com/"), "https://example.com"
        )
        self.assertEqual(
            checker._canonical_page("https://example.com/blog/"),
            "https://example.com/blog/",
        )

    def test_parse_links_resolves_and_filters(self):
        html = """
            <a href="/root-relative">a</a>
            <a href="../parent">b</a>
            <a href="sibling">c</a>
            <a href="https://ext.example/x?q=1#frag">d</a>
            <a href="//cdn.example/lib.js">e</a>
            <a href="mailto:x@y.z">f</a>
            <a href="tel:+3612345678">g</a>
            <a href="javascript:void(0)">h</a>
            <a href="#section">i</a>
        """
        links = checker.parse_links(html, "https://example.com/blog/post/")
        self.assertEqual(
            links,
            [
                "https://example.com/root-relative",
                "https://example.com/blog/parent",
                "https://example.com/blog/post/sibling",
                "https://ext.example/x?q=1",
                "https://cdn.example/lib.js",
            ],
        )

    def test_parse_links_keeps_hash_routes_only_when_asked(self):
        html = '<a href="#/about">route</a><a href="#section">anchor</a>'
        page = "https://spa.example/"
        self.assertEqual(checker.parse_links(html, page), [])
        self.assertEqual(
            checker.parse_links(html, page, keep_hash_routes=True),
            ["https://spa.example/#/about"],
        )

    def test_looks_like_spa(self):
        self.assertTrue(checker.looks_like_spa('<div id="app"></div><script src="a.js">'))
        self.assertFalse(checker.looks_like_spa(""))
        self.assertFalse(
            checker.looks_like_spa('<script></script><a href="/1">1</a><a href="/2">2</a>'
                                   '<a href="/3">3</a>')
        )


class SitemapDiscoveryTests(unittest.TestCase):
    """Default discovery mode: read sitemap.xml."""

    def test_discovers_exactly_the_sitemap_pages(self):
        pages = checker.discover_pages(BASE_URL, 50, _plain_fetcher())
        self.assertEqual(
            sorted(pages), sorted(f"{BASE_URL}/{page}" for page in SITEMAP_PAGES)
        )
        self.assertTrue(all(status == 200 for status, _ in pages.values()))

    def test_max_pages_caps_discovery(self):
        pages = checker.discover_pages(BASE_URL, 2, _plain_fetcher())
        self.assertEqual(len(pages), 2)


class CrawlTests(unittest.TestCase):
    """Recursive discovery mode: follow the site's own links."""

    def setUp(self):
        self.pages = checker.crawl_pages(BASE_URL, 50, _plain_fetcher(), False)

    def test_reaches_pages_the_sitemap_omits(self):
        for page in ("team.html", "posts/first.html"):
            self.assertIn(f"{BASE_URL}/{page}", self.pages, page)

    def test_never_crawls_downloads_or_assets(self):
        self.assertNotIn(f"{BASE_URL}/report.pdf", self.pages)
        self.assertNotIn(f"{BASE_URL}/app.js", self.pages)

    def test_folds_the_two_root_spellings(self):
        self.assertIn(BASE_URL, self.pages)
        self.assertNotIn(f"{BASE_URL}/", self.pages)

    def test_max_pages_caps_the_crawl(self):
        capped = checker.crawl_pages(BASE_URL, 3, _plain_fetcher(), False)
        self.assertEqual(len(capped), 3)

    def test_finds_both_dead_links(self):
        links, _ = checker.extract_links(BASE_URL, self.pages, False, [], "(page)")
        statuses = {url: checker.check_link(url) for url in links}
        broken = sorted(url for url, status in statuses.items() if not checker._ok(status))
        self.assertEqual(
            broken,
            [f"{BASE_URL}/also-gone.html", f"{BASE_URL}/gone.html"],
        )
        self.assertEqual(statuses[f"{BASE_URL}/gone.html"], 404)


class ExtractionTests(unittest.TestCase):
    """Filtering and counting once pages are loaded."""

    def setUp(self):
        self.pages = checker.crawl_pages(BASE_URL, 50, _plain_fetcher(), False)

    def test_external_links_can_be_excluded(self):
        internal, _ = checker.extract_links(BASE_URL, self.pages, False, [], "(page)")
        external, _ = checker.extract_links(BASE_URL, self.pages, True, [], "(page)")
        self.assertIn("https://example.com", external)
        self.assertNotIn("https://example.com", internal)

    def test_skip_domains_wins_over_external_checking(self):
        links, _ = checker.extract_links(
            BASE_URL, self.pages, True, ["example.com"], "(page)"
        )
        self.assertNotIn("https://example.com", links)

    def test_total_counts_occurrences_not_unique_urls(self):
        links, total = checker.extract_links(BASE_URL, self.pages, True, [], "(page)")
        self.assertGreater(total, len(links))

    def test_unreachable_label_drops_once_a_real_source_is_known(self):
        links, _ = checker.extract_links(BASE_URL, self.pages, False, [], "(page)")
        sources = links[f"{BASE_URL}/gone.html"]
        self.assertNotIn("(page)", sources)
        self.assertIn(BASE_URL, sources)


class RenderTests(unittest.TestCase):
    """The headless-browser mode, skipped when no browser is installed."""

    @classmethod
    def setUpClass(cls):
        cls.renderer = checker.PageRenderer(workers=1)
        try:
            cls.renderer.fetch_many([f"{BASE_URL}/index.html"])
        except Exception as error:  # browser binary missing
            cls.renderer.close()
            raise unittest.SkipTest(f"no usable browser: {type(error).__name__}")

    @classmethod
    def tearDownClass(cls):
        cls.renderer.close()

    def test_plain_http_sees_no_links_on_the_spa_shell(self):
        status, html = checker.fetch(f"{BASE_URL}/spa.html")
        self.assertEqual(status, 200)
        self.assertEqual(checker.parse_links(html, f"{BASE_URL}/spa.html"), [])

    def test_rendering_reveals_the_javascript_built_links(self):
        page = f"{BASE_URL}/spa.html"
        status, html = self.renderer.fetch_many([page])[0]
        self.assertEqual(status, 200)
        links = checker.parse_links(html, page)
        self.assertIn(f"{BASE_URL}/js-gone.html", links)
        self.assertIn("https://example.com", links)
        self.assertEqual(len(links), 4)

    def test_auto_mode_renders_only_the_shell(self):
        fetcher = checker.PageFetcher("auto", 2)
        try:
            urls = [f"{BASE_URL}/index.html", f"{BASE_URL}/spa.html"]
            responses = fetcher.fetch_many(urls)
        finally:
            fetcher.close()
        static_links = checker.parse_links(responses[0][1], urls[0])
        spa_links = checker.parse_links(responses[1][1], urls[1])
        self.assertTrue(static_links)  # untouched, still parsed fine
        self.assertEqual(len(spa_links), 4)  # rendered because it looked empty


class ArgumentTests(unittest.TestCase):
    """Command-line validation."""

    def test_sites_are_normalised_and_deduplicated(self):
        args = checker.parse_args(["https://a.example/", "https://a.example"])
        self.assertEqual(args.sites, ["https://a.example"])

    def test_skip_domains_are_split_and_lowercased(self):
        args = checker.parse_args(["https://a.example", "--skip-domains", "X.com, Y.COM"])
        self.assertEqual(args.skip_domains, ["x.com", "y.com"])

    def test_bare_render_flag_means_auto(self):
        self.assertEqual(checker.parse_args(["https://a.example", "--render"]).render, "auto")
        self.assertEqual(checker.parse_args(["https://a.example"]).render, "never")

    def test_rejects_bad_input(self):
        for argv in (
            ["not-a-url"],
            ["https://a.example", "--workers", "0"],
            ["https://a.example", "--max-pages", "0"],
        ):
            # argparse prints its usage to stderr before exiting; swallow it
            # so a passing suite stays quiet.
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    checker.parse_args(argv)


if __name__ == "__main__":
    unittest.main()

"""Link Health Checker.

Usage examples:

    python scripts/main.py https://example.com https://example.org
    python scripts/main.py https://example.com --internal-only --max-pages 20
    python scripts/main.py https://example.com --crawl
    python scripts/main.py https://spa.example --crawl --render

Exits with code 1 if any broken link is found, 0 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.client
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path

from playwright.async_api import async_playwright
from rich.align import Align
from rich.console import Console
from rich.padding import Padding
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

VERSION = "1.0.0"
REPO_URL = "github.com/zsoltfrks/link-health-checker"
BANNER_FILE = Path(__file__).with_name("ascii.txt")
BANNER_FROM = (253, 166, 172)  # light tint of the accent, top of the logo
BANNER_TO = (150, 43, 50)  # dark shade of the accent, bottom of the logo
ACCENT = "#f9727c"

# The report goes to stdout so `main.py ... > report.txt` captures exactly
# that; the banner and progress bars are chrome and belong on stderr.
console = Console()
status_console = Console(stderr=True)

USER_AGENT = "Mozilla/5.0 (compatible; link-health-checker/1.0)"
MAX_BODY_BYTES = 2_000_000  # plenty for HTML pages and sitemaps
HEAD_TIMEOUT = 10
GET_TIMEOUT = 15

# Browsers are far heavier than plain sockets, so rendering runs with its
# own (smaller) concurrency cap and its own timeouts.
MAX_RENDER_WORKERS = 4
RENDER_TIMEOUT_MS = 20_000
RENDER_IDLE_MS = 3_000

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"<a\s[^>]*href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
XML_RE = re.compile(r"\.xml(\?|$)", re.IGNORECASE)
SKIPPED_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")

# RFC 3986 reserved/unreserved characters that must survive percent-encoding.
# '%' is included so already-encoded URLs are not double-encoded.
URL_SAFE = "!#$%&'()*+,/:;=?@[]~-._"

# The crawler must not follow these: they are never HTML pages, and some of
# them (archives, video) are expensive to download.
NON_PAGE_SUFFIXES = (
    ".css", ".js", ".json", ".xml", ".rss", ".atom", ".txt", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif",
    ".pdf", ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dmg",
    ".mp3", ".mp4", ".webm", ".avi", ".mov", ".woff", ".woff2", ".ttf",
)

# A document that runs scripts but exposes almost no links is very likely a
# client-side rendered shell whose real content only appears after JS runs.
SPA_ANCHOR_THRESHOLD = 3


def encode_url(url: str) -> str:
    """Make a URL safe for urllib by percent-encoding it where needed.

    urllib.request refuses raw non-ASCII input, so hrefs scraped from real
    pages (``/über-uns``, spaces, IDN hosts) would otherwise raise instead
    of being checked. Already-encoded URLs pass through unchanged.

    Args:
        url: An absolute http(s) URL, possibly containing raw non-ASCII
            characters or spaces.

    Returns:
        The URL with its host IDNA-encoded (best effort) and its path and
        query percent-encoded.
    """
    parsed_url = urllib.parse.urlsplit(url)
    netloc = parsed_url.netloc
    if not netloc.isascii():
        host, sep, port = netloc.partition(":")
        try:
            netloc = host.encode("idna").decode("ascii") + sep + port
        except UnicodeError:
            pass  # leave as-is; the fetch will fail and report status 0
    return urllib.parse.urlunsplit(
        parsed_url._replace(
            netloc=netloc,
            path=urllib.parse.quote(parsed_url.path, safe=URL_SAFE),
            query=urllib.parse.quote(parsed_url.query, safe=URL_SAFE),
        )
    )


def fetch(
    url: str,
    method: str = "GET",
    timeout: int = GET_TIMEOUT,
    read_body: bool = True,
) -> tuple[int, str]:
    """Perform an HTTP request and normalise every outcome to (status, body).

    Redirects are followed automatically, so the returned status is the
    final one. Response bodies are capped at MAX_BODY_BYTES and decoded
    as UTF-8 with replacement, which keeps link extraction working even
    for mislabelled or truncated documents.

    Args:
        url: The URL to request; encoded via :func:`encode_url` first.
        method: HTTP method, ``"GET"`` or ``"HEAD"``.
        timeout: Socket timeout in seconds.
        read_body: When False, only the status code is read — the body is
            never downloaded (used for status-only link checks).

    Returns:
        A ``(status, body)`` tuple. ``status`` is the HTTP status code, or
        ``0`` for any network-level failure (DNS error, timeout, TLS error,
        unparseable URL). ``body`` is ``""`` unless a body was requested
        and received.
    """
    http_req = urllib.request.Request(
        encode_url(url), method=method, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(http_req, timeout=timeout) as response:
            if not read_body or method == "HEAD":
                return response.status, ""
            body = response.read(MAX_BODY_BYTES).decode("utf-8", "replace")
            return response.status, body
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, ""
    except (OSError, http.client.HTTPException, ValueError):
        return 0, ""


def _ok(status: int) -> bool:
    """Return True when a final HTTP status counts as a healthy link."""
    return 200 <= status < 400


def _locs(sitemap_xml: str) -> list[str]:
    """Extract and entity-decode all ``<loc>`` values from sitemap XML.

    Args:
        sitemap_xml: Raw sitemap or sitemap-index XML.

    Returns:
        The decoded URLs, in document order.
    """
    return [unescape(loc) for loc in LOC_RE.findall(sitemap_xml)]


def looks_like_spa(html: str) -> bool:
    """Guess whether a document needs JavaScript to reveal its links.

    Used by the ``auto`` render mode to spend a browser only where plain
    HTTP came back with an empty shell.

    Args:
        html: The document as served over plain HTTP.

    Returns:
        True when the page runs scripts but exposes almost no links.
    """
    if not html:
        return False
    return "<script" in html.lower() and len(HREF_RE.findall(html)) < SPA_ANCHOR_THRESHOLD


def parse_links(html: str, page_url: str, keep_hash_routes: bool = False) -> list[str]:
    """Pull every checkable link out of one page's HTML.

    Relative hrefs are resolved against the page, non-http(s) schemes are
    dropped, and fragments are stripped — ``/docs#install`` is the same
    resource as ``/docs`` as far as a server is concerned.

    Args:
        html: The page's HTML.
        page_url: Absolute URL the HTML was loaded from.
        keep_hash_routes: When True, a ``#/route`` style fragment is kept,
            because hash-routed single-page apps serve genuinely different
            content per fragment. Plain ``#anchor`` links are still folded
            into their page.

    Returns:
        Absolute http(s) URLs, in document order, duplicates included.
    """
    links = []
    for href in HREF_RE.findall(html):
        href = unescape(href).strip()
        if not href or href.lower().startswith(SKIPPED_SCHEMES):
            continue
        is_route = keep_hash_routes and href.startswith("#/")
        if href.startswith("#") and not is_route:
            continue
        absolute = urllib.parse.urljoin(page_url, href)
        parsed_url = urllib.parse.urlsplit(absolute)
        if parsed_url.scheme not in ("http", "https"):
            continue
        if not (keep_hash_routes and parsed_url.fragment.startswith("/")):
            absolute = urllib.parse.urldefrag(absolute).url
        links.append(absolute)
    return links


async def _block_heavy_assets(route) -> None:
    """Abort requests for bytes that cannot influence the page's links.

    Images, fonts and media are pure payload, so skipping them speeds up
    rendering. Stylesheets deliberately stay allowed: blocking them was
    measured to break hydration on real sites (half the links went
    missing) while saving under 100 ms.
    """
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


class PageRenderer:
    """Loads pages in a headless browser so JavaScript-built links appear.

    One browser instance is launched on the first page and reused for every
    page after it, and pages render concurrently on a single asyncio loop.
    """

    def __init__(self, workers: int, timeout_ms: int = RENDER_TIMEOUT_MS) -> None:
        """Prepare a renderer; the browser starts on the first fetch.

        Args:
            workers: Maximum pages rendered concurrently.
            timeout_ms: Navigation timeout per page, in milliseconds.
        """
        self._workers = max(1, min(workers, MAX_RENDER_WORKERS))
        self._timeout_ms = timeout_ms
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playwright = None
        self._browser = None
        self._context = None

    def _start(self) -> None:
        """Launch the browser and its shared context, once."""
        if self._browser is not None:
            return
        self._loop = asyncio.new_event_loop()
        run = self._loop.run_until_complete
        self._playwright = run(async_playwright().start())
        self._browser = run(self._playwright.chromium.launch())
        self._context = run(self._browser.new_context(user_agent=USER_AGENT))
        run(self._context.route("**/*", _block_heavy_assets))

    def fetch_many(self, urls: list[str]) -> list[tuple[int, str]]:
        """Render several pages and return their (status, html) pairs.

        Args:
            urls: Page URLs to render.

        Returns:
            One ``(status, html)`` tuple per URL, in the same order.
        """
        if not urls:
            return []
        self._start()
        return self._loop.run_until_complete(self._render_all(urls))

    async def _render_all(self, urls: list[str]) -> list[tuple[int, str]]:
        """Render every URL, at most ``workers`` of them at a time."""
        limit = asyncio.Semaphore(self._workers)

        async def bounded(url: str) -> tuple[int, str]:
            async with limit:
                return await self._render_one(url)

        return list(await asyncio.gather(*(bounded(url) for url in urls)))

    async def _render_one(self, url: str) -> tuple[int, str]:
        """Render a single page, tolerating slow or chatty sites.

        The status is read as soon as the response commits, then the page
        gets a short extra budget to settle. A page that never goes idle
        (polling, animations) still yields whatever it has rendered by
        then instead of failing the whole run.
        """
        page = await self._context.new_page()
        status, html = 0, ""
        try:
            response = await page.goto(
                url, wait_until="commit", timeout=self._timeout_ms
            )
            if response is not None:
                status = response.status
            try:
                await page.wait_for_load_state("networkidle", timeout=RENDER_IDLE_MS)
            except Exception:
                pass  # still busy: take the DOM as it stands
            html = await page.content()
        except Exception:
            status, html = status or 0, html
        finally:
            await page.close()
        return status, html

    def close(self) -> None:
        """Shut the browser down and dispose of the event loop."""
        if self._loop is None:
            return
        run = self._loop.run_until_complete
        try:
            if self._context is not None:
                run(self._context.close())
            if self._browser is not None:
                run(self._browser.close())
            if self._playwright is not None:
                run(self._playwright.stop())
        except Exception:
            pass  # best effort: the process is exiting anyway
        finally:
            self._loop.close()
            self._loop = None


class PageFetcher:
    """Loads page HTML, over plain HTTP or through a headless browser.

    This is the only place that knows how a page is obtained, so link
    extraction, crawling and checking stay identical in every mode.
    """

    def __init__(self, render_mode: str, workers: int) -> None:
        """Configure the fetcher.

        Args:
            render_mode: ``"never"`` (plain HTTP only), ``"auto"`` (render
                just the pages that look like empty SPA shells) or
                ``"always"`` (render every page).
            workers: Concurrency for plain HTTP fetches; rendering uses its
                own, lower cap.
        """
        self.render_mode = render_mode
        self.workers = workers
        self._renderer: PageRenderer | None = None

    @property
    def renderer(self) -> PageRenderer:
        """The lazily created browser-backed renderer."""
        if self._renderer is None:
            self._renderer = PageRenderer(self.workers)
        return self._renderer

    def fetch_many(self, urls: list[str]) -> list[tuple[int, str]]:
        """Load several pages and return their (status, html) pairs.

        Args:
            urls: Page URLs to load.

        Returns:
            One ``(status, html)`` tuple per URL, in the same order.
        """
        if not urls:
            return []
        if self.render_mode == "always":
            return self.renderer.fetch_many(urls)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            responses = list(pool.map(fetch, urls))
        if self.render_mode != "auto":
            return responses

        shells = [
            i for i, (status, html) in enumerate(responses)
            if _ok(status) and looks_like_spa(html)
        ]
        if shells:
            rendered = self.renderer.fetch_many([urls[i] for i in shells])
            for index, (status, html) in zip(shells, rendered):
                if _ok(status) and html:
                    responses[index] = (status, html)
        return responses

    def close(self) -> None:
        """Release the browser, if one was ever started."""
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None


def discover_pages(
    site: str,
    max_pages: int,
    fetcher: PageFetcher,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, tuple[int, str]]:
    """Collect the site's pages from its sitemap.

    Fetches ``<site>/sitemap.xml``; entries that are themselves sitemaps
    (a sitemap index) are followed one level deep. When no sitemap is
    available at all, the homepage alone is scanned so the site still
    gets checked.

    Args:
        site: Site root URL without a trailing slash.
        max_pages: Maximum number of pages to load.
        fetcher: Loader used for the pages themselves (sitemaps are always
            plain XML, so they are read over plain HTTP).
        on_progress: Called with the number of pages loaded so far.

    Returns:
        Mapping of page URL to its ``(status, html)`` response.
    """
    sitemap_status, sitemap_xml = fetch(site + "/sitemap.xml")
    locs = _locs(sitemap_xml) if sitemap_status == 200 else []

    pages = [loc for loc in locs if not XML_RE.search(loc)]
    for child_sitemap in (loc for loc in locs if XML_RE.search(loc)):
        sub_status, sub_xml = fetch(child_sitemap)
        if sub_status == 200:
            pages += [loc for loc in _locs(sub_xml) if not XML_RE.search(loc)]

    pages = pages or [site]  # no sitemap -> at least check the homepage
    unique_pages = list(dict.fromkeys(page.split("#")[0] for page in pages))[:max_pages]
    responses = fetcher.fetch_many(unique_pages)
    if on_progress is not None:
        on_progress(len(unique_pages))
    return dict(zip(unique_pages, responses))


def _canonical_page(url: str) -> str:
    """Fold the two spellings of a site root into one.

    Crawls start at ``https://example.com`` while the site's own navigation
    links to ``https://example.com/``; without this they would be visited
    as two separate pages.
    """
    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.path == "/" and not parsed_url.query:
        return urllib.parse.urlunsplit(parsed_url._replace(path=""))
    return url


def _is_page_candidate(url: str, site_host: str) -> bool:
    """Return True when a link is worth crawling as another page.

    Only same-host http(s) URLs qualify, and anything whose path ends in a
    known asset or download extension is rejected before it is fetched.
    """
    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.netloc.lower() != site_host:
        return False
    return not parsed_url.path.lower().endswith(NON_PAGE_SUFFIXES)


def crawl_pages(
    site: str,
    max_pages: int,
    fetcher: PageFetcher,
    keep_hash_routes: bool,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, tuple[int, str]]:
    """Walk a site breadth-first, following its own internal links.

    Starts at the site root and keeps visiting newly discovered same-host
    pages until ``max_pages`` is reached, so no sitemap is required. Pages
    are loaded in batches to keep the existing concurrency behaviour.

    Args:
        site: Site root URL without a trailing slash.
        max_pages: Hard cap on how many pages are loaded.
        fetcher: Loader used for every page.
        keep_hash_routes: Treat ``#/route`` fragments as separate pages,
            which is what hash-routed single-page apps need.
        on_progress: Called after every batch with the number of pages
            loaded so far, since the total is unknown until the end.

    Returns:
        Mapping of page URL to its ``(status, html)`` response.
    """
    site_host = urllib.parse.urlsplit(site).netloc.lower()
    queue = deque([site])
    seen = {site}
    pages: dict[str, tuple[int, str]] = {}

    while queue and len(pages) < max_pages:
        batch = []
        while queue and len(batch) < fetcher.workers and len(pages) + len(batch) < max_pages:
            batch.append(queue.popleft())

        for page_url, (status, html) in zip(batch, fetcher.fetch_many(batch)):
            pages[page_url] = (status, html)
            if not _ok(status) or not html:
                continue
            for link in parse_links(html, page_url, keep_hash_routes):
                link = _canonical_page(link)
                if link in seen or not _is_page_candidate(link, site_host):
                    continue
                seen.add(link)
                queue.append(link)

        if on_progress is not None:
            on_progress(len(pages))

    return pages


def extract_links(
    site: str,
    pages: dict[str, tuple[int, str]],
    check_external: bool,
    skip_domains: list[str],
    unreachable_label: str,
) -> tuple[dict[str, set[str]], int]:
    """Turn scanned pages into the set of links that need checking.

    A page that failed to load is itself queued as a link, so it shows up
    in the final report instead of vanishing silently.

    Args:
        site: Site root URL the pages belong to (defines "internal").
        pages: Mapping of page URL to its ``(status, html)`` response.
        check_external: When False, links whose host differs from the
            site's host are skipped.
        skip_domains: Lowercase domains to skip (exact or subdomain match).
        unreachable_label: Stand-in "found on" note for pages that failed
            to load and therefore have no source page of their own.

    Returns:
        A ``(links, found_total)`` tuple: a mapping of absolute link URL to
        the set of pages it was found on, and the grand total of link
        occurrences discovered on the scanned pages — counted before
        deduplication and before the external/skip-domain filters, so it
        reflects how many links the site actually contains.
    """
    site_host = urllib.parse.urlsplit(site).netloc.lower()
    links: dict[str, set[str]] = {}
    found_total = 0

    for page, (status, html) in pages.items():
        if not _ok(status) or not html:
            links.setdefault(page, set()).add(unreachable_label)
            continue

        for link in parse_links(html, page):
            found_total += 1
            host = urllib.parse.urlsplit(link).netloc.lower()
            if not check_external and host != site_host:
                continue
            if any(
                host == domain or host.endswith("." + domain)
                for domain in skip_domains
            ):
                continue
            links.setdefault(link, set()).add(page)

    # A page that failed to load is usually also linked from a page that did;
    # once a real source is known the stand-in label is just noise.
    for sources in links.values():
        if len(sources) > 1:
            sources.discard(unreachable_label)

    return links, found_total


def check_link(url: str) -> int:
    """Determine the final HTTP status of a link.

    Tries a cheap HEAD request first. Unless that answer is conclusive
    (healthy, or a definite 404/410), the link is retried with a GET —
    many servers reject HEAD (403/405/999) even though the page exists.
    The retry never downloads the response body.

    Args:
        url: Absolute link URL to verify.

    Returns:
        The final status code, or ``0`` for a network-level failure.
    """
    status, _ = fetch(url, method="HEAD", timeout=HEAD_TIMEOUT)
    if _ok(status) or status in (404, 410):
        return status
    status, _ = fetch(url, read_body=False)
    return status


def _gradient(text: str, start: tuple[int, int, int], end: tuple[int, int, int]) -> Text:
    """Colour each line of a block of text along an RGB gradient.

    Args:
        text: Multi-line text, typically the ASCII logo.
        start: RGB colour of the first line.
        end: RGB colour of the last line.

    Returns:
        Rich text with one colour per line.
    """
    lines = text.rstrip("\n").split("\n")
    steps = max(len(lines) - 1, 1)
    rendered = Text()
    for index, line in enumerate(lines):
        ratio = index / steps
        red, green, blue = (
            int(begin + (finish - begin) * ratio) for begin, finish in zip(start, end)
        )
        rendered.append(line + "\n", style=f"#{red:02x}{green:02x}{blue:02x}")
    return rendered


def render_banner(args: argparse.Namespace) -> None:
    """Print the logo and the settings this run will use.

    Args:
        args: The parsed command line, used for the settings line.
    """
    width = min(status_console.width, 60)
    centre = functools.partial(Align.center, width=width)

    title = Text("Link Health Checker  ", style="bold white")
    title.append(f"v{VERSION}", style=ACCENT)

    settings = Text()
    settings.append("mode ", style="white")
    settings.append("crawl" if args.crawl else "sitemap", style=ACCENT)
    settings.append("     render ", style="white")
    settings.append(args.render, style=ACCENT)
    settings.append("     ", style="white")
    settings.append(str(args.workers), style=ACCENT)
    settings.append(" workers     max ", style="white")
    settings.append(str(args.max_pages), style=ACCENT)
    settings.append(" pages", style="white")

    status_console.print()
    if BANNER_FILE.is_file():
        logo = _gradient(
            BANNER_FILE.read_text(encoding="utf-8"), BANNER_FROM, BANNER_TO
        )
        status_console.print(centre(logo))
    status_console.print(centre(title))
    status_console.print(centre(Text(REPO_URL, style="white")))
    status_console.print()
    status_console.print(centre(settings))
    status_console.print()


def _set_progress(progress: Progress, task: int, completed: int) -> None:
    """Progress callback shape the discovery functions expect."""
    progress.update(task, completed=completed)


def make_progress() -> Progress:
    """Build the progress display used for both phases of a run."""
    return Progress(
        SpinnerColumn(style=ACCENT),
        TextColumn("[bold]{task.description}"),
        BarColumn(complete_style=ACCENT, finished_style="green"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=status_console,
    )


def render_report(
    sites: list[str],
    all_links: dict[str, dict[str, set[str]]],
    broken: dict[str, int],
    total_found: int,
    elapsed_ms: int,
    pages_scanned: int,
) -> None:
    """Print the run summary and every broken link, grouped per site.

    Args:
        sites: The site root URLs that were checked.
        all_links: Mapping of link URL to ``{"pages": ..., "sites": ...}``
            describing where each link was found.
        broken: Mapping of broken link URL to its final status code.
        total_found: Grand total of link occurrences discovered on the
            scanned pages, before deduplication and skip filters.
        elapsed_ms: Wall-clock duration of the whole run in milliseconds.
        pages_scanned: How many pages were loaded across all sites.
    """
    console.print(Text("Summary", style="bold"))
    console.print()

    summary = Table.grid(padding=(0, 3))
    summary.add_column(style="dim", justify="right")
    summary.add_column(style="bold")
    summary.add_row("pages scanned", str(pages_scanned))
    summary.add_row("links found", str(total_found))
    summary.add_row("unique links", str(len(all_links)))
    summary.add_row(
        "broken",
        Text(str(len(broken)), style="bold red" if broken else "bold green"),
    )
    summary.add_row("duration", f"{elapsed_ms / 1000:.1f} s")
    console.print(Padding(summary, (0, 0, 0, 2)))
    console.print()

    for site in sorted(sites):
        site_urls = [url for url, entry in all_links.items() if site in entry["sites"]]
        site_broken = sorted(url for url in site_urls if url in broken)

        # A titled rule would truncate long URLs, so the site gets its own
        # folding line under a plain separator.
        console.rule(style="dim")
        console.print(Padding(Text(site, style="bold", overflow="fold"), (0, 0, 0, 2)))

        counts = Text("  ")
        counts.append(f"{len(site_urls)} checked", style="dim")
        counts.append("  ·  ", style="dim")
        counts.append(
            f"{len(site_broken)} broken", style="red" if site_broken else "green"
        )
        console.print(counts)
        console.print()

        if not site_broken:
            console.print(Text("  all links healthy", style="green"))
            console.print()
            continue

        # URLs are the point of the report, so they get the full width: a
        # narrow status column, then the link with its sources underneath.
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold red", justify="right", no_wrap=True)
        table.add_column(overflow="fold")
        for url in site_broken:
            status = broken[url]
            entry = Text(url, style="bold")
            entry.append("\nfound on: ", style="dim")
            entry.append(", ".join(sorted(all_links[url]["pages"])), style="dim")
            table.add_row("dead" if status == 0 else str(status), entry)
        console.print(Padding(table, (0, 0, 0, 2)))
        console.print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate command-line arguments.

    Args:
        argv: Argument list, or None to use ``sys.argv``.

    Returns:
        The validated namespace (sites deduplicated and slash-stripped,
        skip domains lowercased and split).
    """
    parser = argparse.ArgumentParser(
        description="Check one or more websites for dead links.",
        epilog=(
            "Example: python scripts/main.py https://example.com"
            " --skip-domains linkedin.com,x.com"
        ),
    )
    parser.add_argument(
        "sites", nargs="+", help="site URLs to check, e.g. https://example.com"
    )
    parser.add_argument(
        "--max-pages", type=int, default=50,
        help="pages crawled per site (default: 50)",
    )
    parser.add_argument(
        "--internal-only", action="store_true",
        help="skip links pointing to other domains",
    )
    parser.add_argument(
        "--skip-domains", default="",
        help="comma-separated domains to skip, e.g. linkedin.com,x.com",
    )
    parser.add_argument(
        "--workers", type=int, default=10,
        help="concurrent requests (default: 10)",
    )
    parser.add_argument(
        "--crawl", action="store_true",
        help="follow internal links instead of reading sitemap.xml",
    )
    parser.add_argument(
        "--render", nargs="?", choices=("never", "auto", "always"),
        const="auto", default="never",
        help=(
            "render pages in a headless browser so JavaScript-built links"
            " are found; bare --render means auto"
        ),
    )
    args = parser.parse_args(argv)

    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    args.sites = list(dict.fromkeys(site.strip().rstrip("/") for site in args.sites))
    for site in args.sites:
        if not re.match(r"^https?://", site, re.IGNORECASE):
            parser.error(f"site URLs must start with http:// or https:// (got: {site})")
    args.skip_domains = [
        d.strip().lower() for d in args.skip_domains.split(",") if d.strip()
    ]
    return args


def main() -> int:
    """Run the checker end to end and print the report.

    Returns:
        Process exit code: ``1`` when broken links were found, else ``0``.
    """
    start_time = time.perf_counter()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = parse_args()
    render_banner(args)
    fetcher = PageFetcher(args.render, args.workers)

    # url -> {"pages": set(), "sites": set()}
    all_links: dict[str, dict[str, set[str]]] = {}
    total_found = 0
    pages_scanned = 0
    statuses: dict[str, int] = {}

    with make_progress() as progress:
        try:
            for site in args.sites:
                host = urllib.parse.urlsplit(site).netloc
                # The page count is unknown up front in both modes, so the
                # task starts open-ended and is closed off once it is known.
                task = progress.add_task(f"scanning {host}", total=None)
                advance = functools.partial(_set_progress, progress, task)

                if args.crawl:
                    pages = crawl_pages(
                        site, args.max_pages, fetcher, args.render != "never", advance
                    )
                    label = "(page reached by crawl)"
                else:
                    pages = discover_pages(site, args.max_pages, fetcher, advance)
                    label = "(page listed in sitemap)"
                progress.update(task, total=len(pages), completed=len(pages))
                pages_scanned += len(pages)

                site_links, found_on_site = extract_links(
                    site, pages, not args.internal_only, args.skip_domains, label
                )
                total_found += found_on_site
                for url, found_on in site_links.items():
                    link_entry = all_links.setdefault(
                        url, {"pages": set(), "sites": set()}
                    )
                    link_entry["pages"] |= found_on
                    link_entry["sites"].add(site)
        finally:
            fetcher.close()

        check_task = progress.add_task("checking links", total=len(all_links))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(check_link, url): url for url in all_links}
            for future in as_completed(pending):
                statuses[pending[future]] = future.result()
                progress.advance(check_task)

    broken = {url: status for url, status in statuses.items() if not _ok(status)}
    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    console.print()
    render_report(
        args.sites, all_links, broken, total_found, elapsed_ms, pages_scanned
    )
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())

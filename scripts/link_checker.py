"""Link Health Checker.

Usage examples:

    python scripts/link_checker.py https://example.com https://example.org
    python scripts/link_checker.py https://example.com --internal-only --max-pages 20

Exits with code 1 if any broken link is found, 0 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape

USER_AGENT = "Mozilla/5.0 (compatible; dead-link-checker/1.0)"
MAX_BODY_BYTES = 2_000_000  # plenty for HTML pages and sitemaps
HEAD_TIMEOUT = 10
GET_TIMEOUT = 15

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"<a\s[^>]*href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
XML_RE = re.compile(r"\.xml(\?|$)", re.IGNORECASE)
SKIPPED_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")

# RFC 3986 reserved/unreserved characters that must survive percent-encoding.
# '%' is included so already-encoded URLs are not double-encoded.
URL_SAFE = "!#$%&'()*+,/:;=?@[]~-._"


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
    except urllib.error.URLError:
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


def discover_pages(site: str, max_pages: int) -> list[str]:
    """Collect the site's page URLs from its sitemap.

    Fetches ``<site>/sitemap.xml``; entries that are themselves sitemaps
    (a sitemap index) are followed one level deep. When no sitemap is
    available at all, the homepage alone is returned so the site still
    gets checked.

    Args:
        site: Site root URL without a trailing slash.
        max_pages: Maximum number of page URLs to return.

    Returns:
        Deduplicated, fragment-stripped page URLs, capped at ``max_pages``.
    """
    sitemap_status, sitemap_xml = fetch(site + "/sitemap.xml")
    locs = _locs(sitemap_xml) if sitemap_status == 200 else []

    pages = [loc for loc in locs if not XML_RE.search(loc)]
    for child_sitemap in (loc for loc in locs if XML_RE.search(loc)):
        sub_status, sub_xml = fetch(child_sitemap)
        if sub_status == 200:
            pages += [loc for loc in _locs(sub_xml) if not XML_RE.search(loc)]

    pages = pages or [site]  # no sitemap -> at least check the homepage
    unique_pages = list(dict.fromkeys(page.split("#")[0] for page in pages))
    return unique_pages[:max_pages]


def extract_links(
    site: str,
    pages: list[str],
    check_external: bool,
    skip_domains: list[str],
    workers: int,
) -> dict[str, set[str]]:
    """Fetch the given pages and collect every checkable link on them.
    Relative hrefs are resolved against their page, and fragments are
    stripped, and non-http(s) schemes are ignored. A page that fails to
    load is itself queued as a link, so it shows up in the final report.

    Args:
        site: Site root URL the pages belong to (defines "internal").
        pages: Page URLs to scan.
        check_external: When False, links whose host differs from the
            site's host is skipped.
        skip_domains: Lowercase domains to skip (exact or subdomain match).
        workers: Number of concurrent page fetches.

    Returns:
        Mapping of absolute link URL to the set of pages it was found on.
    """
    site_host = urllib.parse.urlsplit(site).netloc.lower()
    links: dict[str, set[str]] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        page_responses = list(pool.map(fetch, pages))

    for page, (status, html) in zip(pages, page_responses):
        if not _ok(status) or not html:
            # The page itself did not load - queue it so it shows up in the report
            links.setdefault(page, set()).add("(page listed in sitemap)")
            continue

        for href in HREF_RE.findall(html):
            href = unescape(href).strip()
            if (not href or href.startswith("#")
                    or href.lower().startswith(SKIPPED_SCHEMES)):
                continue
            absolute = urllib.parse.urldefrag(urllib.parse.urljoin(page, href)).url
            parsed_url = urllib.parse.urlsplit(absolute)
            if parsed_url.scheme not in ("http", "https"):
                continue
            host = parsed_url.netloc.lower()
            if not check_external and host != site_host:
                continue
            if any(
                host == domain or host.endswith("." + domain)
                for domain in skip_domains
            ):
                continue
            links.setdefault(absolute, set()).add(page)

    return links


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


def build_report(
    sites: list[str],
    all_links: dict[str, dict[str, set[str]]],
    broken: dict[str, int],
) -> str:
    """Format the check results as the per-site plain-text report.

    Args:
        sites: The site root URLs that were checked.
        all_links: Mapping of link URL to ``{"pages": ..., "sites": ...}``
            describing where each link was found.
        broken: Mapping of broken link URL to its final status code.

    Returns:
        The complete report, ready to print.
    """
    report_lines = [
        f"Link check for {len(sites)} site(s)",
        f"Checked {len(all_links)} unique links, found {len(broken)} broken.",
        "",
    ]
    for site in sorted(sites):
        site_urls = [url for url, entry in all_links.items() if site in entry["sites"]]
        site_broken = sorted(url for url in site_urls if url in broken)
        report_lines.append(
            f"{site} - checked {len(site_urls)} links, {len(site_broken)} broken"
        )
        for url in site_broken:
            if broken[url] == 0:
                reason = "unreachable (DNS error or timeout)"
            else:
                reason = f"HTTP {broken[url]}"
            pages_str = ", ".join(sorted(all_links[url]["pages"]))
            report_lines.append(f"- {url}")
            report_lines.append(f"  {reason} | found on: {pages_str}")
        report_lines.append("")
    return "\n".join(report_lines).strip()


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
            "Example: python link_checker.py https://example.com"
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
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = parse_args()

    # url -> {"pages": set(), "sites": set()}
    all_links: dict[str, dict[str, set[str]]] = {}
    for site in args.sites:
        pages = discover_pages(site, args.max_pages)
        print(f"{site}: crawling {len(pages)} page(s)...", file=sys.stderr)
        site_links = extract_links(
            site, pages, not args.internal_only, args.skip_domains, args.workers
        )
        for url, found_on in site_links.items():
            link_entry = all_links.setdefault(url, {"pages": set(), "sites": set()})
            link_entry["pages"] |= found_on
            link_entry["sites"].add(site)

    print(f"checking {len(all_links)} unique link(s)...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        statuses = dict(zip(all_links, pool.map(check_link, all_links)))
    broken = {url: status for url, status in statuses.items() if not _ok(status)}

    print(build_report(args.sites, all_links, broken))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())

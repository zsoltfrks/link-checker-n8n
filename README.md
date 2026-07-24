<div align="center">

# Link Health Checker

<a href="https://n8n.io"><img src="https://img.shields.io/badge/n8n-1.x-2b2b2b?logo=n8n&logoColor=white" alt="n8n"></a>
<a href="https://www.python.org"><img src="https://img.shields.io/badge/python-3.9%2B-2b2b2b?logo=python&logoColor=white" alt="Python 3.9+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-2b2b2b.svg?" alt="License"></a>
<img src="https://img.shields.io/badge/Maintained%3F-Yes!-2b2b2b.svg?" alt="Maintained">
<img src="https://img.shields.io/badge/PRs%3F-Welcome!-2b2b2b.svg?" alt="PRs Welcome">

</div>

<p align="center">
  <img src="docs/demo.gif" alt="Link Health Checker crawling a site and printing its report" width="800">
</p>

Point it at one or more sites and it crawls their sitemaps, checks every unique link it finds, and reports the broken ones grouped per site and including which pages they appear on, so you know exactly what to fix.

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Run n8n with Docker (optional)](#run-n8n-with-docker-optional)
  - [Import and configure the workflow](#import-and-configure-the-workflow)
  - [Get notified](#get-notified)
- [Standalone Python script](#standalone-python-script)
  - [Crawling without a sitemap](#crawling-without-a-sitemap)
  - [Rendering JavaScript pages](#rendering-javascript-pages)
  - [Running the tests](#running-the-tests)
- [Repository layout](#repository-layout)
- [Limitations](#limitations)

## Features

- **Multi-site** — check any number of websites in one run, results grouped per site
- **Sitemap-driven crawling** — follows sitemap indexes (WordPress and friends) one level deep, and falls back to the homepage when no sitemap exists
- **Recursive crawl** — or skip sitemaps entirely and let the Python script walk the site from a single URL (`--crawl`)
- **JavaScript rendering** — headless-browser mode finds links that only exist after JS runs (`--render`)
- **Few false positives** — fast `HEAD` checks first, with an automatic `GET` retry for servers that reject `HEAD` (403/405/999)
- **Actionable reports** — every broken link with its HTTP status and the pages it was found on
- **No-fuss workflow** — plain n8n 1.x nodes, no community packages, no credentials required; works self-hosted and on n8n Cloud
- **Ready to schedule** — comes with a daily trigger, just activate the workflow

## How it works

```
sitemap.xml ──► page URLs ──► fetch pages ──► extract <a href> links
                                                      │
report ◄── GET retry (for servers that block HEAD) ◄── HEAD check
```

The workflow discovers pages through `sitemap.xml`, fetches each one, extracts and deduplicates all `<a href>` links, then verifies them. Links that fail a `HEAD` check with anything other than a definite 404/410 are rechecked with a real `GET` before being reported, which keeps false positives low.

A run produces a report like this (see the **Build Report** node output):

```
Link check for 2 site(s)
Checked 142 unique links, found 2 broken.

https://example.com - checked 97 links, 1 broken
- https://example.com/old-blog-post
  HTTP 404 | found on: https://example.com/blog, https://example.com/archive

https://example.org - checked 45 links, 1 broken
- https://gone-domain.example
  unreachable (DNS error or timeout) | found on: https://example.org/links
```

## Getting started

### Prerequisites

For the n8n workflow:

- An [n8n](https://n8n.io) **1.x** instance, self-hosted or on n8n Cloud
- Or [Docker](https://www.docker.com), to spin one up locally (see below)

For the standalone Python script:

- **Python 3.9+**, then install the dependencies and the headless browser:

  ```bash
  python -m pip install -r requirements.txt
  python -m playwright install chromium
  ```

> [!NOTE]
> Use the `python -m` form, with the same launcher you run the script with. On Windows `py` and `python` are often two different installations, so a bare `pip install` can easily land in the one that is not running the checker.

### Run n8n with Docker (optional)

If you don't have an n8n instance yet, [`docker-compose.yml`](docker-compose.yml) starts one:

```bash
docker compose up -d
```

Open <http://localhost:5678>, create the owner account, and import the workflow straight from the CLI:

```bash
docker compose exec n8n n8n import:workflow --input=/workflows/link-health-checker.json
```

All data is persisted in the `n8n_data` volume, and the timezone in the compose file controls when the daily schedule fires.

> [!NOTE]
> If you open n8n from another machine over plain HTTP (not `localhost`), the login cookie is rejected — uncomment `N8N_SECURE_COOKIE=false` in the compose file.

### Import and configure the workflow

1. In n8n, go to **Workflows** → **Create Workflow** → **⋯** → **Import from File** and select [`n8n/link-health-checker.json`](n8n/link-health-checker.json) (skip if you imported via the CLI above).
2. Open the **Config** node and set your values:

   | Field                | Default                                    | Description                                                     |
   | -------------------- | ------------------------------------------ | --------------------------------------------------------------- |
   | `siteUrls`           | `https://example.com, https://example.org` | One or more websites to check, separated by commas or new lines |
   | `checkExternalLinks` | `true`                                     | Also check links pointing to other domains                      |
   | `maxPages`           | `50`                                       | Cap on how many pages are crawled per run                       |
   | `skipDomains`        | _(empty)_                                  | Comma-separated domains to skip, e.g. `linkedin.com,x.com`      |

3. Click **Execute workflow** and inspect the output of the **Build Report** node.
4. **Activate** the workflow to run the daily 7:00 check automatically.

### Get notified

The **Send Email Report** node ships disabled so the workflow runs without credentials. Add SMTP credentials, set the from/to addresses and enable it — or swap it for a Slack, Discord or Telegram node.

> [!TIP]
> The report is available as `{{ $json.report }}` (plain text) and `{{ $json.broken }}` (a structured array with `url`, `status` and `foundOn`), so any notification node can be wired in with two clicks.

## Standalone Python script

Want the same check without n8n — in CI, a cron job or a git hook? [`scripts/main.py`](scripts/main.py) is a standalone port of the workflow (Python 3.9+, see [Prerequisites](#prerequisites) for the install):

```bash
python scripts/main.py https://example.com https://example.org
python scripts/main.py https://example.com --internal-only --max-pages 20 --skip-domains linkedin.com,x.com
```

It prints the same style of per-site report — plus a grand total of every link found and the crawl duration in milliseconds — and exits with code `1` when broken links are found, so it can fail a pipeline — useful as a scheduled GitHub Action.

### Crawling without a sitemap

`--crawl` walks the site breadth-first from the URL you give it, following its own internal links instead of reading `sitemap.xml`. Downloads and assets (`.pdf`, `.zip`, images…) are checked but never crawled, and `--max-pages` still caps the run:

```bash
python scripts/main.py https://example.com --crawl --max-pages 100
```

### Rendering JavaScript pages

`--render` loads pages in a headless browser ([Playwright](https://playwright.dev/python/)) first, so links that only exist after JavaScript runs are found too:

```bash
python scripts/main.py https://spa.example --crawl --render
```

| Mode                          | Behaviour                                                                   |
| ----------------------------- | --------------------------------------------------------------------------- |
| `--render never` _(default)_  | Plain HTTP only — fastest, no browser started                               |
| `--render` or `--render auto` | Plain HTTP first, browser only for pages that come back as empty SPA shells |
| `--render always`             | Every page goes through the browser                                         |

> [!TIP]
> `auto` is usually what you want: static pages keep their millisecond speed and only the JavaScript-driven ones pay for a browser. Images, fonts and media are blocked while rendering (stylesheets are not — blocking those was measured to break hydration and lose half the links).

### Running the tests

The suite covers URL handling, both discovery modes, the filters and the rendering path, against a fixture site served locally — no internet access needed:

```bash
python -m unittest discover -s tests
```

The demo GIF at the top of this README is recorded with [VHS](https://github.com/charmbracelet/vhs) from [`docs/demo.tape`](docs/demo.tape) — the regeneration command is in the tape file's header.

## Repository layout

```
docs/        demo GIF and the VHS tape that regenerates it
n8n/         the importable workflow JSON
scripts/     the standalone Python checker
tests/       unittest suite and the fixture site it runs against
```

## Limitations

- Requests are throttled (batches of 5–10 per second) to be polite to the target site.
- The n8n workflow is plain-HTTP only: client-side rendered pages (JavaScript SPAs) expose just the links present in the served HTML. The Python script solves this with `--render`, which the workflow cannot do without an external rendering service.
- Status `0` in the report means the request failed at the network level (DNS error, timeout or connection refused).

> [!IMPORTANT]
> Some sites (LinkedIn, X/Twitter, certain CDNs) aggressively block automated requests and may show up as broken even though they work in a browser. Add them to `skipDomains` to silence them.

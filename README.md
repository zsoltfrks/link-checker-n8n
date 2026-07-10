<div align="center">

# Link Health Checker N8N

<a href="https://n8n.io"><img src="https://img.shields.io/badge/n8n-1.x-EA4B71?logo=n8n&logoColor=white" alt="n8n"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg?" alt="License"></a>
<img src="https://img.shields.io/badge/Maintained%3F-yes-green.svg?" alt="Maintained">
<img src="https://img.shields.io/badge/PRs-welcome-green.svg?" alt="PRs Welcome">

Point it at one or more sites and it crawls their sitemaps, checks every unique link it finds, and reports the broken ones — grouped per site and including which pages they appear on, so you know exactly what to fix.

## Features

- **Multi-site** — check any number of websites in one run, results grouped per site
- **Sitemap-driven crawling** — follows sitemap indexes (WordPress and friends) one level deep, and falls back to the homepage when no sitemap exists
- **Few false positives** — fast `HEAD` checks first, with an automatic `GET` retry for servers that reject `HEAD` (403/405/999)
- **Actionable reports** — every broken link with its HTTP status and the pages it was found on
- **Zero dependencies** — plain n8n 1.x nodes, no community packages, no credentials required; works self-hosted and on n8n Cloud
- **Ready to schedule** — comes with a daily trigger, just activate the workflow

## Getting started

### Import and configure the workflow

1. In n8n, go to **Workflows** → **Create Workflow** → **⋯** → **Import from File** and select [`dead-link-checker.json`](dead-link-checker.json)
2. Open the **Config** node and set your values:

   | Field                | Default               | Description                                                |
   | -------------------- | --------------------- | ---------------------------------------------------------- |
   | `siteUrl`            | `https://example.com` | The website to check                                       |
   | `checkExternalLinks` | `true`                | Also check links pointing to other domains                 |
   | `maxPages`           | `50`                  | Cap on how many pages are crawled per run                  |
   | `skipDomains`        | _(empty)_             | Comma-separated domains to skip, e.g. `linkedin.com,x.com` |

3. Click **Execute workflow** and inspect the output of the **Build Report** node.
4. **Activate** the workflow to run the daily 7:00 check automatically.

### Get notified

The **Send Email Report** node ships disabled so the workflow runs without credentials. Add SMTP credentials, set the from/to addresses and enable it — or swap it for a Slack, Discord or Telegram node.

> [!TIP]
> The report is available as `{{ $json.report }}` (plain text) and `{{ $json.broken }}` (a structured array with `url`, `status` and `foundOn`), so any notification node can be wired in with two clicks.

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

- An [n8n](https://n8n.io) **1.x** instance, self-hosted or on n8n Cloud
- Or [Docker](https://www.docker.com), to spin one up locally (see below)

### Run n8n with Docker (optional)

If you don't have an n8n instance yet, [`docker-compose.yml`](docker-compose.yml) starts one:

```bash
docker compose up -d
```

Open <http://localhost:5678>, create the owner account, and import the workflow straight from the CLI:

```bash
docker compose exec n8n n8n import:workflow --input=/workflows/dead-link-checker.json
```

All data is persisted in the `n8n_data` volume, and the timezone in the compose file controls when the daily schedule fires.

> [!NOTE]
> If you open n8n from another machine over plain HTTP (not `localhost`), the login cookie is rejected — uncomment `N8N_SECURE_COOKIE=false` in the compose file.

### Import and configure the workflow

1. In n8n, go to **Workflows** → **Create Workflow** → **⋯** → **Import from File** and select [`dead-link-checker.json`](dead-link-checker.json) (skip if you imported via the CLI above).
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

## Limitations

- Requests are throttled (batches of 5–10 per second) to be polite to the target site.
- Pages rendered entirely client-side (JavaScript SPAs) can't be scanned with plain HTTP requests — links that only exist after JS runs won't be found.
- Status `0` in the report means the request failed at the network level (DNS error, timeout or connection refused).

> [!IMPORTANT]
> Some sites (LinkedIn, X/Twitter, certain CDNs) aggressively block automated requests and may show up as broken even though they work in a browser. Add them to `skipDomains` to silence them.
